"""The stdlib HTTP transport for the RoutePilot API (Stage 4 U13).

Standard library only: :mod:`http.server`, :mod:`json`, :mod:`urllib`. There is no FastAPI, no
Flask, no WSGI server and no new dependency of any kind. This module owns *only* transport
concerns - routing, request parsing, response writing, static asset serving and the error mapping -
and it holds **no business formula**: every number it returns is produced by
:mod:`api.services` (and therefore by ``core``/``storage``), never by arithmetic invented here.

Routing and behaviour
=====================

* ``GET /api/health`` - the honest capability/environment document (D12/D16/D19/D39);
* ``GET /api/plans`` - the stored plans;
* ``GET /api/plans/{id}`` - one plan with its stops, first-stop state and provenance;
* ``POST /api/plans`` - create (or return) the deterministic DEMO/SYNTHETIC demo plan;
* ``PUT /api/plans/{id}`` - the approved MVP stop controls, ``enabled`` and ``priority`` only;
* ``GET /api/settings/{key}`` / ``PUT /api/settings/{key}`` - the ``app_settings`` store,
  including the approved tile keys of D15;
* everything else: a ``404`` JSON error; an API path that exists with another method: ``405``;
* a declared-but-unimplemented API path (reoptimization after a served stop - a later stage):
  ``501`` with the capability explained, which is how this transport stays honest about missing
  features.

Engine-facing endpoints (Stage 4 U14, D39; every one is synchronous and has no job/status sibling)
================================================================================================

===================================================================  ==================================================
endpoint                                                             what it does
===================================================================  ==================================================
``GET /api/plans/{id}/recommendation``                                 **live recompute** of the exhaustive first-stop
                                                                     recommendation. ADVISORY: the payload states that
                                                                     it is a recommendation and NOT an applied decision
                                                                     (``advisory``, ``applied_decision: false``,
                                                                     ``as_plan_state: false``, ``note``). Nothing is
                                                                     written: no plan change, no run row (owner
                                                                     decision 5). A ``no_fully_feasible_route``
                                                                     outcome is a valid ``200`` with its diagnostics
                                                                     and no recommended stop (v2 section 14)
``POST /api/plans/{id}/selection``                                     apply the driver's first stop. Body:
                                                                     ``{"mode": "recommend"|"manual"|"accept",
                                                                     "stop_id": "..."}``. Sets mode, provenance and
                                                                     ``pinned`` through the domain and persists it
                                                                     (D4-D11/D32); it does **not** recompute a route
                                                                     and appends **no** run
``DELETE /api/plans/{id}/selection``                                   cancel / unpin: back to
                                                                     ``awaiting_first_stop_choice`` with a ``null``
                                                                     selected stop and a ``null`` source (D8/D9)
``GET /api/plans/{id}/route``                                          **live recompute** of the committed route for the
                                                                     plan's current selection. Requires a selection:
                                                                     ``409 no_first_stop_selected`` otherwise. Appends
                                                                     no run
``POST /api/plans/{id}/optimize``                                      recalculation that ALSO appends exactly **one**
                                                                     immutable run row (``optimize`` the first time,
                                                                     ``reoptimize`` afterwards). The only endpoint
                                                                     that writes history
``GET /api/plans/{id}/runs``                                           the immutable run history, oldest first, read-only
``GET /api/runs/{run_id}``                                             one stored run by its own id, read-only
===================================================================  ==================================================

Latency contract and single-flight (owner decision 5, D39(e))
=============================================================

* **No GET writes anything.** ``recommendation``, ``route``, ``runs`` and ``runs/{id}`` append no
  history and change no stored state; only ``POST /api/plans/{id}/optimize`` appends a run, and the
  selection endpoints persist the decision that *is* the request (they do not recompute a route).
* **Per-plan single-flight.** Recommendation, route and optimize computations are guarded by one
  lock per plan id (``api.services.PLAN_LOCKS``), so two concurrent requests for the same plan cannot
  run the exhaustive loop twice. The wait is **bounded**
  (``api.services.PLAN_LOCK_TIMEOUT_SECONDS``, reported in ``GET /api/health``) and expiry answers
  ``409 plan_busy`` with an honest message.
* **Honest latency.** The exhaustive recommendation runs the real engine once per enabled stop and
  has **no prefilter** (v2 section 20); at the ~50-enabled-stop portfolio scale the **accepted MVP
  worst case is about 8 seconds** (D36/D37), and that figure is **reported, not hidden**. Every
  computation response carries the measured ``computation_seconds``. There is **no background job
  queue and no async job/status subsystem**: the request either answers synchronously or is refused.
* **Never fabricated.** A timeout, a missing selection or an infeasible plan is answered with its
  documented error or its documented diagnostics - never with a partial route, a placeholder winner
  or a route the driver did not select.

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
condition                                                            HTTP        documented code
==================================================================  ==========  ====================
body is not valid JSON / not a JSON object / empty but required      400         ``invalid_body``
unknown path                                                         404         ``unknown_path``
unknown plan id                                                      404         ``unknown_plan``
unknown stop id inside an existing plan                              404         ``unknown_stop``
unknown optimization-run id                                          404         ``unknown_run``
unknown settings key (nothing stored)                                404         ``unknown_setting``
wrong method for a known path                                        405         ``method_not_allowed``
route asked for while no first stop is selected (D9/I4)              409         ``no_first_stop_selected``
the plan's computation lock did not become free inside the
documented bound (owner decision 5)                                  409         ``plan_busy``
illegal state transition / a change that carries no meaning          409         ``illegal_state``
input validation and domain shape errors (``ValidationError``),
invalid mode/body, a disabled first stop, a mode/source mismatch     422         ``invalid_input``
declared-but-unimplemented capability / any route mode but
``SMART_ROUTE`` (``UnsupportedFeatureError``, D16/D19)               501         ``unsupported_capability``
missing or unusable time zone data
(``TimezoneDataMissingError``)                                       503         ``timezone_data_unavailable``
corrupt stored state (any ``storage.StorageError``)                  500         ``storage_error``
anything else                                                        500         ``internal_error``
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
    MAX_RANKED_RECOMMENDATION_CANDIDATES,
    MAX_REQUEST_BODY_BYTES,
    ApiServices,
    CapabilityNotImplemented,
    Conflict,
    InvalidInput,
    NoFirstStopSelected,
    NotFound,
    PlanBusy,
    ServiceError,
    TimezoneDataUnavailable,
    UnknownPlan,
    UnknownRun,
    UnknownStop,
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
#: a subclass of a mapped error inherits its mapping unless it is listed itself. The four
#: ``NotFound`` subclasses are listed explicitly (U14): each one names *which* thing is missing, so
#: the documented code never depends on the wording of a message.
ERROR_STATUS_MAP: dict[type[BaseException], tuple[int, str]] = {
    InvalidInput: (422, "invalid_input"),
    UnknownPlan: (404, "unknown_plan"),
    UnknownStop: (404, "unknown_stop"),
    UnknownRun: (404, "unknown_run"),
    NotFound: (404, "unknown_plan"),
    NoFirstStopSelected: (409, "no_first_stop_selected"),
    PlanBusy: (409, "plan_busy"),
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

#: Refinement used by the service layer for a **plain** :class:`NotFound` (the settings service): it
#: names *which* thing is missing through its message, and the transport picks the documented code
#: from this table. The plan/stop/run lookups of U14 raise their own subclasses above, so this
#: message-based refinement is only the fallback for a not-found the service did not classify.
_NOT_FOUND_CODES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsettings key\b"), "unknown_setting"),
    (re.compile(r"\bstop\b"), "unknown_stop"),
    (re.compile(r"\brun\b"), "unknown_run"),
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


#: The route table of this unit: the read/config surface, the engine-facing endpoints of U14 and the
#: two approved write endpoints. ``{id}``/``{key}``/``{run_id}`` are single URL path segments
#: (``[^/]+``), so a plan id, a settings key or a run id can never span a path separator.
ROUTES: tuple[Route, ...] = (
    Route(re.compile(r"^/api/health$"), ("GET",), "health"),
    Route(re.compile(r"^/api/plans$"), ("GET", "POST"), "plans"),
    Route(re.compile(r"^/api/plans/(?P<plan_id>[^/]+)$"), ("GET", "PUT"), "plan_by_id"),
    Route(
        re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/recommendation$"),
        ("GET",),
        "plan_recommendation",
    ),
    Route(
        re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/selection$"),
        ("POST", "DELETE"),
        "plan_selection",
    ),
    Route(
        re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/route$"),
        ("GET",),
        "plan_route",
    ),
    Route(
        re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/optimize$"),
        ("POST",),
        "plan_optimize",
    ),
    Route(
        re.compile(r"^/api/plans/(?P<plan_id>[^/]+)/runs$"),
        ("GET",),
        "plan_runs",
    ),
    Route(re.compile(r"^/api/runs/(?P<run_id>[^/]+)$"), ("GET",), "run_by_id"),
    Route(
        re.compile(r"^/api/settings/(?P<key>[^/]+)$"),
        ("GET", "PUT"),
        "setting_by_key",
    ),
)

#: API paths this build deliberately does **not** implement: reoptimization after a served stop,
#: which is a later stage (U16 and the stage after it), not U14. They are declared here so the
#: transport answers ``501`` with the capability explained instead of pretending the endpoint does
#: not exist. ``(methods, path pattern, what arrives later)``.
KNOWN_NOT_IMPLEMENTED: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("POST",),
        r"^/api/plans/[^/]+/reoptimize$",
        "reoptimization after a served stop (a later stage, not U14)",
    ),
    (
        ("GET",),
        r"^/api/plans/[^/]+/preview$",
        "a cheap non-committing route preview (declared, not implemented in this build)",
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
            self._drain_declared_body()
            self._write_error(
                CapabilityNotImplemented(
                    f"{method} {path} is declared but not implemented in this build: the read/config "
                    "surface (health, plans, plan controls, settings) and the engine-facing surface "
                    "(recommendation, the driver's selection, the committed route, the recalculation "
                    "that appends one run row and the run history) are implemented; this endpoint is "
                    f"a later stage, and it would provide {matched.description}"
                )
            )
            return
        if matched is _MISS:
            if allowed:
                self._drain_declared_body()
                self._write_error(
                    _MethodNotAllowed(
                        f"{method} is not allowed on {path}; allowed method(s): "
                        f"{', '.join(allowed)}"
                    )
                )
                return
            self._drain_declared_body()
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
            f"{method} {path} is not part of this API; this surface serves GET /api/health, "
            "GET|POST /api/plans, GET|PUT /api/plans/{id}, "
            "GET /api/plans/{id}/recommendation, POST|DELETE /api/plans/{id}/selection, "
            "GET /api/plans/{id}/route, POST /api/plans/{id}/optimize, GET /api/plans/{id}/runs, "
            "GET /api/runs/{run_id}, GET|PUT /api/settings/{key} and the static assets under web/"
        )

    def _drain_declared_body(self) -> None:
        """Read and discard a declared request body before an early error response is written.

        Every early refusal - the ``501`` of a declared-but-unimplemented path, the ``405`` of a
        wrong method, the ``404`` of an unknown API path and the static-asset ``405`` - is answered
        **before** the body would be read. This transport speaks ``HTTP/1.0`` and therefore closes
        the connection after every response, and a socket closed while the peer's declared body is
        still unread is *reset* (``WSAECONNABORTED`` / ``10053`` on Windows) instead of shut down
        cleanly, so the client can lose a response the server already sent (``D26``: an error is
        answered, never lost). Draining the declared bytes first leaves the socket quiet and the
        close clean, so the refusal survives.

        Exactly ``Content-Length`` bytes are read, bounded by :data:`MAX_REQUEST_BODY_BYTES`: a body
        larger than the limit is never buffered (``_read_json_body`` refuses it on the paths that do
        read one). A header that is missing, unparseable or non-positive declares no body, so
        nothing is read. Nothing is parsed, validated or acted upon - this is transport
        housekeeping, not a semantic change, and every status code, error code and message stays
        exactly as it was.

        The read does wait for bytes the client has declared, which is the point: a client that
        sends its body only after seeing the response (and so would otherwise have that body reset
        under its feet) is answered instead of aborted. ``Content-Length`` is the client's own
        promise of how much it is about to send, and every ordinary client sends it with the
        request rather than after the response.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            length = int(raw_length)
        except ValueError:
            return
        if length <= 0:
            return
        self.rfile.read(min(length, MAX_REQUEST_BODY_BYTES))

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

    # -- U14: recommendation, selection, route and run history ----------- #
    def _handle_plan_recommendation(self, match: re.Match[str], body: Any) -> None:
        """``GET /api/plans/{id}/recommendation`` - live recompute, advisory, writes nothing."""
        self._refuse_body("GET /api/plans/{id}/recommendation", body)
        result = self.server.services.recommendations.recommend(match.group("plan_id"))
        report = result.report
        self._write_json(
            200,
            serialization.recommendation_document(
                report,
                computed_at=(
                    None
                    if report.resolved_at is None
                    else serialization.instant_text(report.resolved_at)
                ),
                computation_seconds=result.computation_seconds,
                ranked_limit=MAX_RANKED_RECOMMENDATION_CANDIDATES,
            ),
        )

    def _handle_plan_selection(self, match: re.Match[str], body: Any) -> None:
        """``POST``/``DELETE /api/plans/{id}/selection`` - the driver's decision (D4-D11/D32).

        Applying a selection persists the driver's decision and **appends no run**: it does not
        recompute a route, so there is nothing to record (owner decision 5). ``GET
        /api/plans/{id}/route`` computes the route for whatever is selected.
        """
        plan_id = match.group("plan_id")
        if self.command == "DELETE":
            result = self.server.services.selections.clear_first_stop(plan_id)
        else:
            request = _require_object_body(body, "POST /api/plans/{id}/selection")
            unknown = sorted(set(request) - self.server.services.selections.ACCEPTED_FIELDS)
            if unknown:
                raise InvalidInput(
                    "POST /api/plans/{id}/selection accepts only 'mode' and 'stop_id'; unexpected "
                    f"field(s): {', '.join(unknown)}"
                )
            result = self.server.services.selections.select_first_stop(
                plan_id, request.get("mode"), request.get("stop_id")
            )
        self._write_json(200, serialization.selection_document(result.plan))

    def _handle_plan_route(self, match: re.Match[str], body: Any) -> None:
        """``GET /api/plans/{id}/route`` - live recompute of the committed route; writes nothing."""
        self._refuse_body("GET /api/plans/{id}/route", body)
        result = self.server.services.routes.committed_route(match.group("plan_id"))
        self._write_json(
            200,
            serialization.route_document(
                result.solution,
                plan_id=result.plan.id,
                route_fingerprint=result.route_fingerprint,
                computation_seconds=result.computation_seconds,
            ),
        )

    def _handle_plan_optimize(self, match: re.Match[str], body: Any) -> None:
        """``POST /api/plans/{id}/optimize`` - recalculate AND append exactly one run row."""
        self._refuse_body("POST /api/plans/{id}/optimize", body)
        result = self.server.services.routes.optimize_and_record(match.group("plan_id"))
        self._write_json(
            201,
            serialization.run_document(
                result.run,
                computation={
                    "computation_seconds": result.computation_seconds,
                    "recommendation_seconds": result.recommendation_seconds,
                    "route_seconds": result.route_seconds,
                    "note": (
                        "measured latency of this recalculation: it recomputed the recommendation, "
                        "computed the committed route and appended exactly one immutable run row. "
                        "No metric was recomputed by the transport."
                    ),
                },
            ),
        )

    def _handle_plan_runs(self, match: re.Match[str], body: Any) -> None:
        """``GET /api/plans/{id}/runs`` - the immutable history, oldest first; writes nothing."""
        self._refuse_body("GET /api/plans/{id}/runs", body)
        plan_id = match.group("plan_id")
        runs = self.server.services.routes.list_runs(plan_id)
        self._write_json(200, serialization.run_list_document(plan_id, runs))

    def _handle_run_by_id(self, match: re.Match[str], body: Any) -> None:
        """``GET /api/runs/{run_id}`` - one stored run by its own id; writes nothing."""
        self._refuse_body("GET /api/runs/{run_id}", body)
        run = self.server.services.routes.get_run(match.group("run_id"))
        self._write_json(200, serialization.run_document(run))

    def _refuse_body(self, endpoint: str, body: Any) -> None:
        """These endpoints take no body: a body would imply a parameter that is not implemented.

        ``DELETE``/``GET`` never carry one through this transport, and ``POST
        /api/plans/{id}/optimize`` accepts nothing; a body is refused rather than ignored, so a
        client cannot believe it sent an option that had no effect (D16).
        """
        if body:
            raise InvalidInput(
                f"{endpoint} accepts no request body, got {sorted(body)}; every parameter of this "
                "endpoint is in its URL and its documented behaviour, and an unimplemented option "
                "is refused rather than silently ignored"
            )

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
            self._drain_declared_body()
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


def _require_object_body(body: Any, endpoint: str) -> dict[str, Any]:
    """The request's body as a JSON object, or a ``422`` naming the endpoint that needs one.

    ``_read_json_body`` already refused anything that is not a JSON *object* (``400``); this helper
    covers the remaining case of a body that was never sent, which for an endpoint that documents a
    body is a request error (``422``) rather than a silent default.
    """
    if body is None:
        raise InvalidInput(f"{endpoint} needs a JSON object body")
    if not isinstance(body, dict):  # pragma: no cover - the reader already guarantees the type
        raise InvalidInput(
            f"{endpoint} needs a JSON object body, got {type(body).__name__}"
        )
    return body


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
