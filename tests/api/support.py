"""Shared test harness for the RoutePilot API suite (Stage 4 U13).

Deterministic and fully offline: the tests drive the transport **in process** on an ephemeral port
bound to ``127.0.0.1`` with :mod:`urllib.request` from the standard library. There is no browser,
no external network and no external service.

Where the database lives
------------------------

Each test gets its own SQLite **file**, opened under the suite-owned scratch root
``var/tests/`` (``tests.SUITE_SCRATCH_ROOT``) through the same helper the server uses and deleted at
the end of the test class, so two tests can never share a database.

That scratch root is a **subdirectory of** the application's runtime data directory ``var/``, never
the runtime directory itself: ``var/`` belongs to the running ``python -m api.serve`` process, which
writes its database and its append-only run history there. The suite may only create and remove
``var/tests/``; :func:`cleanup_scratch_root` is structurally incapable of removing ``var/`` or any
foreign file inside it (D40).

An in-process ``file:...?mode=memory&cache=shared`` URI is supported by
:func:`storage.sqlite.database.connect` (it enables URI semantics for a ``file:`` identifier only),
but it is deliberately **not** used here: a shared-cache in-memory database is keyed by its name
**process-wide**, so two concurrently running test cases that picked the same name would silently
share one database, while a file gives each case its own storage. The cleanliness test
(``tests/storage/test_migrations.py::test_no_database_artifacts_in_the_repository``) runs *after*
this module and must find nothing, so every scratch tree is removed in ``tearDownClass``.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from api.http_server import create_server
from api.services import ApiServices
from tests import RUNTIME_DATA_DIR, SUITE_SCRATCH_ROOT

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the suite's throwaway databases live: the suite-owned ``tests`` marker directory directly
#: under the gitignored runtime data directory ``var/`` (``tests.SUITE_SCRATCH_ROOT``), removed again
#: in ``tearDownClass``. It is deliberately a **descendant** of ``var/`` and never ``var/`` itself,
#: so a sweep can only ever delete suite-owned scratch (D40).
SCRATCH_ROOT = SUITE_SCRATCH_ROOT

#: The name of the suite-owned marker directory directly under the runtime data directory.
_SCRATCH_MARKER_NAME = "tests"

#: Windows' transient "another handle still has this file open" errors: ERROR_SHARING_VIOLATION and
#: ERROR_LOCK_VIOLATION. A scratch sweep retries only these, and only for this long, before failing
#: loudly. A plain access-denied or any other error is never retried and never hidden.
_WINDOWS_SHARING_VIOLATIONS = frozenset({32, 33})
_SCRATCH_RETRY_SECONDS = 5.0

_DB_COUNTER = itertools.count(1)
_DB_LOCK = threading.Lock()


class ScratchRootError(RuntimeError):
    """The scratch sweep was pointed at something the suite does not own.

    Raised instead of deleting anything, so a misconfigured or sabotaged scratch root fails loudly
    rather than destroying the application's runtime data directory (D40).
    """


def new_scratch_directory(prefix: str = "api-tests") -> Path:
    """A fresh scratch directory under the suite-owned ``var/tests/`` tree."""
    with _DB_LOCK:
        index = next(_DB_COUNTER)
    directory = SCRATCH_ROOT / f"{prefix}-{index}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def new_database_file(directory: Path) -> str:
    """A fresh (not yet created) database path inside ``directory``."""
    return str(directory / f"routepilot-{uuid.uuid4().hex[:12]}.db")


def _absolute(path: Path) -> Path:
    """``path`` resolved to an absolute, symlink-free path for structural comparison."""
    return Path(os.path.abspath(path))


def _scratch_removal_target(runtime_dir: Path) -> Path:
    """The single path a scratch sweep may remove: ``<runtime_dir>/tests``, proven to be owned.

    ``runtime_dir`` is injectable so a regression guard can point the sweep at an isolated stand-in.
    The target is **always** the suite marker directory directly under that runtime directory and
    never the runtime directory itself, and the check refuses everything else - including a runtime
    directory that is the repository root, or that resolves outside the runtime data directory -
    before any deletion happens.
    """
    runtime = _absolute(runtime_dir)
    target = _absolute(runtime / _SCRATCH_MARKER_NAME)
    if target.name != _SCRATCH_MARKER_NAME or target.parent != runtime:
        raise ScratchRootError(
            f"refusing to remove {target}: the suite may only remove the "
            f"{_SCRATCH_MARKER_NAME!r} marker directory directly under a runtime data directory"
        )
    if target == runtime:
        raise ScratchRootError(f"refusing to remove the runtime data directory itself: {target}")
    if runtime == REPO_ROOT or runtime == REPO_ROOT.parent:
        raise ScratchRootError(f"refusing to treat {runtime} as the runtime data directory")
    try:
        runtime.relative_to(RUNTIME_DATA_DIR)
    except ValueError as error:
        raise ScratchRootError(
            f"refusing to remove {target}: {runtime} is outside the runtime data directory "
            f"{RUNTIME_DATA_DIR}"
        ) from error
    return target


def _remove_tree(target: Path) -> None:
    """Remove ``target``, retrying briefly while Windows still holds a scratch file open.

    A ``tearDownModule`` sweep can run while the last request's connection is still being closed, and
    Windows then refuses to unlink that scratch database (``WinError 32``) - which is exactly the
    error ``ignore_errors=True`` used to swallow. Only that transient sharing violation is retried,
    and only for a bounded deadline; anything else, and a lock that outlasts the deadline, propagates
    so the suite fails loudly instead of leaving an artifact behind (D40).
    """
    deadline = monotonic() + _SCRATCH_RETRY_SECONDS
    delay = 0.05
    while True:
        try:
            shutil.rmtree(target)
            return
        except OSError as error:
            if not _is_transient_sharing_violation(error) or monotonic() >= deadline:
                raise
            sleep(delay)
            delay = min(delay * 2, 0.5)


def _is_transient_sharing_violation(error: OSError) -> bool:
    """Whether ``error`` is Windows refusing to unlink a file another handle still holds open."""
    return getattr(error, "winerror", None) in _WINDOWS_SHARING_VIOLATIONS


def cleanup_scratch_root(runtime_dir: Path | None = None) -> None:
    """Remove the suite-owned scratch subtree ``<runtime_dir>/tests`` and nothing else.

    Every test class removes its own directory again; this is the belt-and-braces sweep that also
    covers a directory left behind by a killed run, so the suite can prove it leaves no artifact.
    Attach it as ``tearDownModule`` in a module to run it after that module finishes - the
    no-argument call sweeps ``tests.RUNTIME_DATA_DIR``.

    The application's runtime data directory is **never** a deletion target: the sweep removes only
    the ``tests`` marker directory directly under it, and a target that is not provably that
    directory raises :class:`ScratchRootError` instead of deleting anything (D40). ``runtime_dir``
    exists so a regression guard can point the sweep at an isolated stand-in runtime directory.
    """
    target = _scratch_removal_target(RUNTIME_DATA_DIR if runtime_dir is None else runtime_dir)
    if not target.is_dir():
        return
    _remove_tree(target)


# --------------------------------------------------------------------------- #
# U14 fixtures: a DEMO/SYNTHETIC matrix variant and a constructed infeasible plan
# --------------------------------------------------------------------------- #
def build_infeasible_demo_plan(plan_id: str = "demo-unreachable"):
    """A copy of the demo fixture whose stops all close before the driver can arrive.

    Every enabled stop is given the same fixed window, ``01:00`` to ``02:00`` **local** on the plan's
    service date, while the driver departs at ``04:00`` local (``demo.dataset``: 04:00 Europe/Moscow).
    The window is therefore already closed for every candidate of every complete route - the earliest
    possible arrival is the departure itself - so the engine evaluates all candidates and rejects all
    of them on the stops they cannot serve. Nothing about the resulting payload is constructed by
    hand; the engine produces it.

    Returns ``(plan, window_stop_id)``.
    """
    from dataclasses import replace as dataclass_replace

    from core.model.service_window import ServiceWindow, WindowKind
    from demo.dataset import build_demo_plan

    plan = build_demo_plan(plan_id=plan_id)
    window = ServiceWindow(
        window_kind=WindowKind.FIXED, start_local=time(1, 0), end_local=time(2, 0)
    )
    stops = tuple(
        dataclass_replace(stop, service_window=window)
        if stop.enabled
        else stop  # a disabled stop keeps the fixture's own window: it is never routed
        for stop in plan.stops
    )
    plan = dataclass_replace(plan, stops=stops)
    return plan, plan.active_stops()[0].id


@dataclass(frozen=True)
class Response:
    """One HTTP response, parsed for assertions."""

    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        """The decoded JSON body (raises when the body is not JSON - a test failure in itself)."""
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class ApiServerTestCase(unittest.TestCase):
    """Base case: an in-process server on an ephemeral loopback port and a clean database."""

    #: When ``True``, :meth:`setUp` keeps the database the class prepared in ``setUpClass`` instead
    #: of creating a fresh one. A U14 case that prepares an expensive plan state (a selection and a
    #: recorded run) sets this, so the tests can read that state without paying for it again.
    use_prepared_database = False

    def setUp(self) -> None:
        self.scratch = new_scratch_directory()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.services = self.make_services(
            self.class_database if self.use_prepared_database else new_database_file(self.scratch)
        )
        self.addCleanup(self.services.close)
        self.server = None

    def make_services(self, identifier: str) -> ApiServices:
        """The services this case runs. A U14 case may override it to inject a matrix fixture."""
        return ApiServices(identifier)

    def start_server(self, *, static_root: Path | str | None = None) -> str:
        """Start the server on an ephemeral port; returns its base URL (no trailing slash)."""
        if self.server is not None:
            raise AssertionError("start_server() was already called in this test")
        self.server = create_server(
            self.services,
            host="127.0.0.1",
            port=0,
            static_root=static_root if static_root is not None else self.missing_static_root(),
            quiet=True,
        )
        # Cleanups run in reverse registration order: the thread is joined *after* the server was
        # shut down, so `_stop_server` is registered last on purpose.
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="routepilot-api-test"
        )
        self.server_thread.start()
        self.addCleanup(self._join_server_thread)
        self.addCleanup(self._stop_server)
        host, port = self.server.server_address[0], self.server.server_address[1]
        return f"http://{host}:{port}"

    def missing_static_root(self) -> Path:
        """A static root that does not exist (``web/`` does not exist yet in this repository).

        Deliberately a path that is simply never created rather than a temporary directory: the
        resolver is pure, so no directory has to exist for the "not there yet" behaviour to be
        exercised, and the suite leaves no temporary artifact behind.
        """
        return self.scratch / "no-web"

    def _join_server_thread(self) -> None:
        """Wait (briefly) for the serving thread, which :meth:`_stop_server` has already stopped."""
        thread = getattr(self, "server_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(5.0)

    def _stop_server(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    # -- request helpers ------------------------------------------------- #
    def request(
        self,
        method: str,
        path: str,
        *,
        base_url: str | None = None,
        body: Any = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Send one request and return its response, including error statuses.

        ``body`` is JSON-encoded; ``raw_body`` is sent verbatim (for malformed-JSON tests). An HTTP
        error response is returned rather than raised, because the status code is under test.

        The client timeout is generous **on purpose**: the U14 engine-facing endpoints recompute a
        recommendation or a route synchronously and legitimately take seconds (31 complete routes on
        the demo plan; the ~50-stop portfolio worst case is about 8 s, D36/D37). A tight timeout would
        turn a slow-but-correct answer into a spurious failure, while the server's own bounded
        per-plan lock is what keeps a slow request honest.
        """
        url = (base_url or self.base_url) + path
        data: bytes | None
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
        else:
            data = None
        request_headers = dict(headers or {})
        if data is not None and content_type is not None:
            request_headers["Content-Type"] = content_type
        elif data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=data, method=method, headers=request_headers
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return Response(
                    status=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return Response(
                status=error.code,
                headers={k.lower(): v for k, v in error.headers.items()},
                body=error.read(),
            )

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)

    # -- convenience ----------------------------------------------------- #
    def create_demo_plan(self) -> dict[str, Any]:
        """``POST /api/plans`` and return the plan payload, asserting that it was created."""
        response = self.post("/api/plans", body={})
        self.assertEqual(response.status, 201, msg=response.text)
        return response.json()["data"]

    def select_first_stop(self, plan_id: str, mode: str, stop_id: str) -> Response:
        """``POST /api/plans/{id}/selection`` (the body shape of the documented endpoint)."""
        return self.post(
            f"/api/plans/{plan_id}/selection", body={"mode": mode, "stop_id": stop_id}
        )


class ServerBackedTestCase(ApiServerTestCase):
    """A test case that starts its server (on an ephemeral loopback port) for every test."""

    def setUp(self) -> None:
        super().setUp()
        self.base_url = self.start_server()
