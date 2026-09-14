"""The stdlib HTTP transport for the RoutePilot API (Stage 4 U13).

Standard library only: :mod:`http.server`, :mod:`json`, :mod:`urllib`. There is no FastAPI, no
Flask, no WSGI server and no new dependency of any kind. This module owns *only* transport
concerns - routing, request parsing, response writing, static asset serving and the error mapping -
and it holds **no business formula**: every number it returns is produced by
:mod:`api.services` (and therefore by ``core``/``storage``), never by arithmetic invented here.

Routing and behaviour
=====================

* ``GET /api/health`` - the honest capability/environment document (D12/D16/D19);
* ``GET /api/plans`` - the stored plans;
* ``GET /api/plans/{id}`` - one plan with its stops, first-stop state and provenance;
* ``POST /api/plans`` - create (or return) the deterministic DEMO/SYNTHETIC demo plan;
* ``PUT /api/plans/{id}`` - the approved MVP stop controls, ``enabled`` and ``priority`` only;
* ``GET /api/settings/{key}`` / ``PUT /api/settings/{key}`` - the ``app_settings`` store,
  including the approved tile keys of D15;
* everything else: a ``404`` JSON error; an API path that exists with another method: ``405``;
* a declared-but-unimplemented API path (recommendation, selection, route, runs - U14): ``501``
  with the capability explained, which is how this transport stays honest about missing features.

JSON responses carry ``application/json; charset=utf-8``. Static assets are served from ``web/``
with ``text/html``/``text/css``/``application/javascript`` by extension and
``Cache-Control: no-store``. ``GET`` is used for read endpoints and ``PUT``/``POST`` only where a
body is expected; a request body that is not a JSON object is a ``400``.

Static path resolution is a **pure function** (:func:`resolve_static_path`): URL path in, candidate
file under the static root out, with directory traversal (``..``, encoded variants, backslashes)
and absolute paths refused before anything touches the filesystem - so it is unit-testable without
a single file on disk. When ``web/`` does not exist yet (the real UI arrives in U15) a static
request returns a clear ``404`` JSON error rather than crashing; serving the real ``web/`` needs no
code change once it exists.

Error mapping (the explicit table of this transport)
====================================================

==================================================================  ==========  ====================
condition                                                           HTTP        documented code
==================================================================  ==========  ====================
body is not valid JSON / not a JSON object / empty but required      400         ``invalid_body``
unknown path                                                        404         ``unknown_path``
unknown plan id                                                     404         ``unknown_plan``
unknown stop id inside an existing plan                             404         ``unknown_stop``
unknown settings key (nothing stored)                               404         ``unknown_setting``
wrong method for a known path                                       405         ``method_not_allowed``
illegal state transition / a change that carries no meaning         409         ``illegal_state``
input validation and domain shape errors (``ValidationError``)      422         ``invalid_input``
declared-but-unimplemented capability / any route mode but
``SMART_ROUTE`` (``UnsupportedFeatureError``, D16/D19)              501         ``unsupported_capability``
missing or unusable time zone data
(``TimezoneDataMissingError``)                                      503         ``timezone_data_unavailable``
corrupt stored state (any ``storage.StorageError``)                 500         ``storage_error``
anything else                                                       500         ``internal_error``
==================================================================  ==========  ====================

No domain error may ever become a silent ``2xx``: every handler either returns a payload produced
by the service layer or raises, and every raise is mapped through :func:`error_for_exception`.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from api import serialization
from api.services import (
    MAX_REQUEST_BODY_BYTES,
    ApiServices,
    CapabilityNotImplemented,
    Conflict,
    InvalidInput,
    NotFound,
    ServiceError,
    TimezoneDataUnavailable,
    health_payload,
)
from core.validation.errors import (
    ConfigurationError,
    RoutePilotError,
    TimezoneDataMissingError,
    UnsupportedFeatureError,
    ValidationError,
)
from storage import StorageError

__all__ = [
    "API_JSON_CONTENT_TYPE",
    "ERROR_STATUS_MAP",
    "KNOWN_NOT_IMPLEMENTED",
    "NOT_IMPLEMENTED_PATHS",
    "ROUTES",
    "Route",
    "RoutePilotHTTPServer",
    "RouteRequestHandler",
    "STATIC_CONTENT_TYPES",
    "STATIC_EXTENSIONS",
    "StaticPathError",
    "StaticRequest",
    "create_server",
    "error_for_exception",
    "resolve_static_path",
    "serve_forever",
    "static_request_for",
]

API_JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: The repository directory. ``web/`` (the U15 UI) and the default static root both live here.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The directory static assets are served from. It does not exist yet; see the module docstring.
STATIC_ROOT = REPO_ROOT / "web"

#: Extensions this transport will serve, with their content type. A file whose extension is not
#: listed is not served at all (a ``404``), so the static surface stays an explicit allow-list
#: rather than "whatever happens to be in the directory".
STATIC_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
}

#: The same allow-list as a tuple, for documentation and tests.
STATIC_EXTENSIONS: tuple[str, ...] = tuple(sorted(STATIC_CONTENT_TYPES))


# --------------------------------------------------------------------------- #
# static path resolution (pure)
# --------------------------------------------------------------------------- #
class StaticPathError(ValueError):
    """A requested static URL path cannot name a file under the static root.

    Raised for an absolute path, an empty path, a control character, a backslash, a URL that
    encodes one, a dot-segment (``.``/``..``) and an extension outside the allow-list. It is a
    *refusal*, not a filesystem result: nothing is read from disk before this is raised.
    """


@dataclass(frozen=True)
class StaticRequest:
    """A resolved static request, before any file is opened."""

    url_path: str
    relative_path: str
    candidate: Path
    content_type: str

    def read_bytes(self) -> bytes:
        """The file's bytes, or :class:`FileNotFoundError` when it is not there."""
        return self.candidate.read_bytes()


def static_request_for(url_path: str, static_root: Path | str = STATIC_ROOT) -> StaticRequest:
    """Alias of :func:`resolve_static_path`, named for the object it returns."""
    return resolve_static_path(url_path, static_root)


def resolve_static_path(url_path: str, static_root: Path | str = STATIC_ROOT) -> StaticRequest:
    """URL path -> the candidate file under ``static_root`` (PURE: no filesystem access).

    Accepted: ``/app.js``, ``/assets/app.css`` (query strings are ignored). Refused, with
    :class:`StaticPathError`, before anything is opened:

    * anything that is not a string path beginning with ``/``;
    * an absolute filesystem path (``//etc/passwd``, ``C:/Windows/...``, a drive-qualified path);
    * a backslash, a NUL or another control character;
    * **any** dot-segment - ``..`` or ``.``, in any position and in raw or percent-encoded form
      (``/a/%2e%2e/b``, ``/a/%2E%2E/b``) - because percent-decoding happens once, before the
      segments are inspected, so an encoded traversal is the same refusal as a literal one;
    * a drive colon in a segment (``/C:/x``) on any platform;
    * an extension outside :data:`STATIC_CONTENT_TYPES`.

    The result is the join of the root with the relative path; the caller decides what to do when
    that file does not exist (this transport answers a ``404`` JSON error). The path is never
    resolved against the root's parent and ``..`` can never survive, so the resolver cannot escape
    the static root even if a future transport forgot the check.
    """
    if not isinstance(url_path, str) or not url_path.startswith("/"):
        raise StaticPathError(
            f"a static URL path must be an absolute URL path starting with '/', got {url_path!r}"
        )
    if url_path.startswith("//"):
        raise StaticPathError(
            f"static URL path {url_path!r} starts with '//', which is an absolute (UNC or "
            "network-share) path; it is refused"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in url_path):
        raise StaticPathError(f"static URL path {url_path!r} contains a control character")

    split = urlsplit(url_path)
    if split.netloc or split.scheme:
        raise StaticPathError(
            f"static URL path {url_path!r} carries a scheme or network location "
            f"({split.scheme!r}//{split.netloc!r}); only a path under the static root is served"
        )
    path_only = split.path
    decoded = unquote(path_only)
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise StaticPathError(f"static URL path {url_path!r} decodes to a control character")
    if "\\" in decoded:
        raise StaticPathError(
            f"static URL path {url_path!r} contains a backslash; only '/' separates path segments"
        )
    if decoded.startswith("//"):
        raise StaticPathError(
            f"static URL path {url_path!r} looks like an absolute (UNC) path, which is refused"
        )

    segments = [segment for segment in decoded.split("/") if segment != ""]
    for segment in segments:
        if segment in (".", ".."):
            raise StaticPathError(
                f"static URL path {url_path!r} contains the dot-segment {segment!r}: directory "
                "traversal is refused"
            )
        if ":" in segment:
            raise StaticPathError(
                f"static URL path {url_path!r} contains a drive-qualified segment {segment!r}"
            )

    relative = Path(*segments) if segments else Path()
    root = Path(static_root)
    if not segments:
        raise StaticPathError(
            f"static URL path {url_path!r} names a directory, not a file; this transport serves "
            "explicit asset paths only (the demo UI arrives in U15)"
        )
    candidate = root / relative
    suffix = candidate.suffix.lower()
    content_type = STATIC_CONTENT_TYPES.get(suffix)
    if content_type is None:
        raise StaticPathError(
            f"static asset {decoded!r} has extension {suffix or '(none)'!r}, which this transport "
            f"does not serve; allowed: {', '.join(STATIC_EXTENSIONS)} (the demo UI arrives in U15)"
        )

    if not _is_within(root, candidate):
        raise StaticPathError(
            f"static URL path {url_path!r} resolves outside the static root {root}"
        )
    return StaticRequest(
        url_path=path_only,
        relative_path=str(relative),
        candidate=candidate,
        content_type=content_type,
    )


def _is_within(root: Path, candidate: Path) -> bool:
    """Whether ``candidate`` is ``root`` itself or below it, without touching the disk."""
    root_parts = [part for part in root.parts]
    candidate_parts = [part for part in candidate.parts]
    if len(candidate_parts) < len(root_parts):
        return False
    return candidate_parts[: len(root_parts)] == root_parts


# --------------------------------------------------------------------------- #
# error mapping (the table of this transport)
# --------------------------------------------------------------------------- #
#: ``exception class -> (HTTP status, documented code)``. Looked up by walking the class's MRO, so
#: a subclass of a mapped error inherits its mapping unless it is listed itself.
ERROR_STATUS_MAP: dict[type[BaseException], tuple[int, str]] = {
    InvalidInput: (422, "invalid_input"),
    NotFound: (404, "unknown_plan"),
    Conflict: (409, "illegal_state"),
    CapabilityNotImplemented: (501, "unsupported_capability"),
    TimezoneDataUnavailable: (503, "timezone_data_unavailable"),
    TimezoneDataMissingError: (503, "timezone_data_unavailable"),
    UnsupportedFeatureError: (501, "unsupported_capability"),
    ValidationError: (422, "invalid_input"),
    ConfigurationError: (503, "timezone_data_unavailable"),
    StorageError: (500, "storage_error"),
    ServiceError: (500, "internal_error"),
    RoutePilotError: (500, "internal_error"),
}

#: Refinement used by the service layer: a plain :class:`NotFound` names *which* thing is missing
#: through its message, and the transport picks the documented code from this table.
_NOT_FOUND_CODES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsettings key\b"), "unknown_setting"),
    (re.compile(r"\bstop\b"), "unknown_stop"),
    (re.compile(r"\bplan\b"), "unknown_plan"),
)


def _not_found_code(error: NotFound) -> str:
    message = str(error)
    for pattern, code in _NOT_FOUND_CODES:
        if pattern.search(message):
            return code
    return "unknown_path"


def error_for_exception(error: BaseException) -> tuple[int, str, str, str]:
    """``(status, code, type_name, message)`` for one exception - the whole mapping in one place.

    Every exception the transport can meet is classified here; an unclassified one is an
    ``internal_error`` (500) rather than a leaked traceback, and no error becomes a silent ``2xx``.
    """
    for klass in type(error).__mro__:
        mapped = ERROR_STATUS_MAP.get(klass)
        if mapped is not None:
            status, code = mapped
            if isinstance(error, NotFound):
                code = _not_found_code(error)
            return status, code, type(error).__name__, str(error)
    return 500, "internal_error", type(error).__name__, str(error)


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Route:
    """One route-table entry: a compiled pattern, the methods it accepts and its handler name."""

    pattern: re.Pattern[str]
    methods: tuple[str, ...]
    handler: str


#: The route table of this unit: the read/config surface plus the two approved write endpoints.
#: ``{id}``/``{key}`` are single URL path segments (``[^/]+``), so a plan id or settings key can
#: never span a path separator.
ROUTES: tuple[Route, ...] = (
    Route(re.compile(r"^/api/health$"), ("GET",), "health"),
    Route(re.compile(r"^/api/plans$"), ("GET", "POST"), "plans"),
    Route(re.compile(r"^/api/plans/(?P<plan_id>[^/]+)$"), ("GET", "PUT"), "plan_by_id"),
    Route(
        re.compile(r"^/api/settings/(?P<key>[^/]+)$"),
        ("GET", "PUT"),
        "setting_by_key",
    ),
)

#: API paths this unit deliberately does **not** implement (U14): the recommendation, the driver's
#: selection, the computed route and the run history. They are declared here so the transport
#: answers ``501`` with the capability explained instead of pretending the endpoint does not exist.
#: ``(methods, path pattern, what arrives later)``.
KNOWN_NOT_IMPLEMENTED: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("GET",),
        r"^/api/plans/[^/]+/recommendation$",
        "the first-stop recommendation (ranked complete routes; U14)",
    ),
    (
        ("POST", "DELETE"),
        r"^/api/plans/[^/]+/selection$",
        "choosing, accepting or cancelling the driver's first stop (U14)",
    ),
    (
        ("GET", "POST"),
        r"^/api/plans/[^/]+/route$",
        "the computed route with its timeline and metrics (U14)",
    ),
    (
        ("GET",),
        r"^/api/plans/[^/]+/runs$",
        "the immutable optimization-run history (U14)",
    ),
    (
        ("POST",),
        r"^/api/plans/[^/]+/reoptimize$",
        "reoptimization after a served stop (a later stage, not U14)",
    ),
)

#: :data:`KNOWN_NOT_IMPLEMENTED` with its patterns compiled.
NOT_IMPLEMENTED_PATHS: tuple[tuple[tuple[str, ...], re.Pattern[str], str], ...] = tuple(
    (methods, re.compile(pattern), description)
    for methods, pattern, description in KNOWN_NOT_IMPLEMENTED
)


@dataclass(frozen=True)
class _Matched:
    route: Route
    match: re.Match[str]


@dataclass(frozen=True)
class _NotImplemented:
    """A declared API path of a later unit: answered ``501``, never faked or silently 404-ed."""

    description: str
    path: str


_MISS = object()


class RoutePilotHTTPServer(ThreadingHTTPServer):
    """The threading HTTP server: one request per thread, one database connection per request.

    ``ThreadingHTTPServer`` (not ``HTTPServer``) is used because the transport must not serialise
    unrelated readers behind one request; the database side of that is handled by
    :class:`api.services.DatabaseState`, which opens a connection per request because a ``sqlite3``
    connection is not safe to share between threads.
    """

    daemon_threads = True

    #: How often ``serve_forever`` checks for a shutdown request (the stdlib default is 0.5 s).
    #: 0.05 s keeps a stopped server from making a test or a Ctrl+C wait half a second for nothing.
    poll_interval = 0.05

    def __init__(
        self,
        server_address: tuple[str, int],
        services: ApiServices,
        *,
        static_root: Path | str = STATIC_ROOT,
        quiet: bool = True,
    ) -> None:
        self.services = services
        self.static_root = Path(static_root)
        self.quiet = quiet
        super().__init__(server_address, RouteRequestHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Report a handler crash without killing the server or leaking a traceback to a client."""
        if not self.quiet:
            print(f"[api] unhandled error for {client_address}", file=sys.stderr)
        super().handle_error(request, client_address)


class RouteRequestHandler(BaseHTTPRequestHandler):
    """One HTTP request: parse, route, serialise, respond. No business logic lives here."""

    #: ``HTTP/1.0`` on purpose: every response closes its connection, so no handler thread can
    #: stay parked on a keep-alive socket. This is a local demo surface and the simplicity (and a
    #: clean shutdown) is worth more than connection reuse.
    protocol_version = "HTTP/1.0"
    server_version = "RoutePilot/0.1"
    sys_version = ""

    # -- HTTP verbs ------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self._respond("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._respond("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._respond("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._respond("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._respond("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._respond("OPTIONS")

    # -- logging --------------------------------------------------------- #
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Compact one-line access log; silent in ``--quiet`` mode (tests, embedded use)."""
        if self.server.quiet:
            return
        message = format % args
        print(f"[api] {self.address_string()} {message}", file=sys.stderr)

    # -- dispatch -------------------------------------------------------- #
    def _respond(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BaseException as error:  # noqa: BLE001 - the transport must answer, never crash
            self._write_error(error)

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        if path == "":
            path = "/"

        if not path.startswith("/api/"):
            self._serve_static(method, split.path)
            return

        matched, allowed = self._match_api(method, path)
        if isinstance(matched, _NotImplemented):
            self._write_error(
                CapabilityNotImplemented(
                    f"{method} {path} is declared but not implemented in this unit (U13 ships the "
                    f"read/config surface only): it would provide {matched.description}"
                )
            )
            return
        if matched is _MISS:
            if allowed:
                self._write_error(
                    _MethodNotAllowed(
                        f"{method} is not allowed on {path}; allowed method(s): "
                        f"{', '.join(allowed)}"
                    )
                )
                return
            self._write_error(self._not_an_api_path(method, path))
            return

        body = self._read_json_body(method)
        handler = getattr(self, f"_handle_{matched.route.handler}")
        handler(matched.match, body)

    def _match_api(
        self, method: str, path: str
    ) -> tuple[_Matched | _NotImplemented | object, tuple[str, ...]]:
        """Find the route for ``path``.

        Returns the match, or :data:`_MISS` when nothing matched; the second element is the set of
        methods that *would* be allowed, which is what turns "wrong method" into a ``405``.
        """
        allowed: tuple[str, ...] = ()
        for methods, pattern, description in NOT_IMPLEMENTED_PATHS:
            if pattern.match(path):
                if method in methods:
                    return _NotImplemented(description, path), methods
                return _MISS, methods
        for route in ROUTES:
            match = route.pattern.match(path)
            if match is None:
                continue
            if method in route.methods:
                return _Matched(route, match), route.methods
            allowed = route.methods
            break
        return _MISS, allowed

    @staticmethod
    def _not_an_api_path(method: str, path: str) -> ServiceError:
        return NotFound(
            f"{method} {path} is not part of this API; U13 serves GET /api/health, "
            "GET|POST /api/plans, GET|PUT /api/plans/{id}, GET|PUT /api/settings/{key} and the "
            "static assets under web/"
        )

    def _read_json_body(self, method: str) -> Any:
        """The parsed JSON body, or ``None`` when the request carries none.

        Only ``PUT``/``POST`` are expected to carry a body (a body on a ``GET`` is a client error,
        which is exactly the "GET only for read endpoints" rule of the brief). A body that is not
        a JSON *object* is a ``400``: this API accepts objects, and refusing anything else is how
        "not valid JSON" and "not an object" stay distinguishable from a domain rejection (422).
        """
        if method in ("GET", "HEAD", "DELETE"):
            return None
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            raise _BadRequest(f"Content-Length {raw_length!r} is not an integer") from None
        if length <= 0:
            return None
        if length > MAX_REQUEST_BODY_BYTES:
            raise _BadRequest(
                f"request body of {length} bytes exceeds the {MAX_REQUEST_BODY_BYTES}-byte limit"
            )
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise _BadRequest(f"request body is not valid UTF-8: {error}") from None
        except json.JSONDecodeError as error:
            raise _BadRequest(
                f"request body is not valid JSON: {error.msg} (line {error.lineno}, "
                f"column {error.colno})"
            ) from None
        if not isinstance(parsed, dict):
            raise _BadRequest(
                f"request body must be a JSON object, got {type(parsed).__name__}"
            )
        return parsed

    # -- handlers -------------------------------------------------------- #
    def _handle_health(self, match: re.Match[str], body: Any) -> None:
        self._write_json(200, health_payload(self.server.services.state))

    def _handle_plans(self, match: re.Match[str], body: Any) -> None:
        method = self.command
        if method == "GET":
            records = self.server.services.plans.list_plans()
            self._write_json(
                200,
                {
                    "type": "RoutePlanList",
                    "api_version": serialization.API_VERSION,
                    "count": len(records),
                    "data": [
                        serialization.plan_summary_payload(
                            record.plan, record.data_provenance
                        )
                        for record in records
                    ],
                },
            )
            return
        record = self.server.services.plans.create_demo_plan(body)
        self._write_json(201, serialization.plan_document(record.plan, record.data_provenance))

    def _handle_plan_by_id(self, match: re.Match[str], body: Any) -> None:
        plan_id = match.group("plan_id")
        if self.command == "GET":
            record = self.server.services.plans.get_plan(plan_id)
            self._write_json(
                200, serialization.plan_document(record.plan, record.data_provenance)
            )
            return
        record = self.server.services.plans.update_plan_controls(plan_id, body)
        self._write_json(200, serialization.plan_document(record.plan, record.data_provenance))

    def _handle_setting_by_key(self, match: re.Match[str], body: Any) -> None:
        key = match.group("key")
        if self.command == "GET":
            setting = self.server.services.settings.get_setting(key)
            self._write_json(
                200,
                serialization.settings_document(
                    setting.key, setting.value, configured=setting.configured
                ),
            )
            return
        setting = self.server.services.settings.set_setting(key, body)
        self._write_json(
            200,
            serialization.settings_document(
                setting.key, setting.value, configured=setting.configured
            ),
        )

    # -- static assets --------------------------------------------------- #
    def _serve_static(self, method: str, url_path: str) -> None:
        if method != "GET":
            self._write_error(
                _MethodNotAllowed(
                    f"{method} is not allowed on a static asset; use GET (the UI is read-only in "
                    "this unit)"
                )
            )
            return
        try:
            request = resolve_static_path(url_path, self.server.static_root)
        except StaticPathError as error:
            self._write_error(
                NotFound(
                    f"{error} (static assets are served from {self.server.static_root})"
                ),
                code="unknown_path",
            )
            return
        try:
            payload = request.read_bytes()
        except OSError as error:
            self._write_error(
                NotFound(
                    f"static asset {request.relative_path!r} is not available under "
                    f"{self.server.static_root}: {error.strerror or error} (the demo UI arrives in "
                    "U15, so web/ may legitimately not exist yet)"
                ),
                code="unknown_path",
            )
            return
        self._write_bytes(
            200,
            payload,
            content_type=request.content_type,
            extra_headers={"Cache-Control": "no-store"},
        )

    # -- response writing ------------------------------------------------ #
    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._write_bytes(status, body, content_type=API_JSON_CONTENT_TYPE)

    def _write_error(self, error: BaseException, *, code: str | None = None) -> None:
        status, mapped_code, type_name, message = error_for_exception(error)
        document = serialization.error_document(code or mapped_code, type_name, message)
        self._write_json(status, document)

    def _write_bytes(
        self,
        status: int,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


class _BadRequest(ServiceError):
    """The request body is not a JSON object (400 in the mapping)."""

    code = "invalid_body"


class _MethodNotAllowed(ServiceError):
    """The path exists but does not accept this method (405 in the mapping)."""

    code = "method_not_allowed"


#: Codes the **router** raises rather than the service layer (400/404/405). They are merged into
#: ``ERROR_STATUS_MAP`` so the transport's mapping is complete in exactly one table.
ERROR_STATUS_MAP[_BadRequest] = (400, "invalid_body")
ERROR_STATUS_MAP[_MethodNotAllowed] = (405, "method_not_allowed")


# --------------------------------------------------------------------------- #
# server construction
# --------------------------------------------------------------------------- #
def create_server(
    services: ApiServices,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    static_root: Path | str = STATIC_ROOT,
    quiet: bool = True,
) -> RoutePilotHTTPServer:
    """Bind and return the server. The caller runs :meth:`serve_forever` and closes it.

    ``host`` defaults to loopback: this transport is a local demo surface, and binding it to a
    public interface is a deliberate decision that has not been taken (no authentication exists).
    """
    return RoutePilotHTTPServer(
        (host, port), services, static_root=static_root, quiet=quiet
    )


def serve_forever(
    services: ApiServices,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    static_root: Path | str = STATIC_ROOT,
    quiet: bool = False,
) -> None:
    """Serve until interrupted, then shut the server and its database state down."""
    server = create_server(
        services, host=host, port=port, static_root=static_root, quiet=quiet
    )
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(f"RoutePilot API serving on http://{bound_host}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("RoutePilot API stopped", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        services.close()
