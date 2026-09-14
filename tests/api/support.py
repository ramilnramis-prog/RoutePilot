"""Shared test harness for the RoutePilot API suite (Stage 4 U13).

Deterministic and fully offline: the tests drive the transport **in process** on an ephemeral port
bound to ``127.0.0.1`` with :mod:`urllib.request` from the standard library. There is no browser,
no external network and no external service.

Where the database lives
------------------------

Each test gets its own SQLite database, and it is a **file** rather than an in-process
``mode=memory`` URI. That is a deliberate, documented consequence of the storage boundary:
:func:`storage.sqlite.database.connect` passes the identifier straight to
``sqlite3.connect()`` **without** ``uri=True``, so a ``file:...?mode=memory&cache=shared`` string is
treated as a filesystem path (SQLite would literally create a file named ``file:``). This suite must
not change storage semantics, so it exercises the supported case instead: a real database file,
opened from the gitignored ``var/`` directory through the same helper the server uses, and deleted
at the end of the test class. The cleanliness test
(``tests/storage/test_migrations.py::test_no_database_artifacts_in_the_repository``) runs *after*
this module and must find nothing, so every scratch tree is removed in ``tearDownClass``.
"""

from __future__ import annotations

import itertools
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.http_server import create_server
from api.services import ApiServices

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the suite's throwaway databases live: inside the repository (the sandbox does not reliably
#: create directories under ``%TEMP%``) but inside the gitignored ``var/`` directory, and removed
#: again in ``tearDownClass``.
SCRATCH_ROOT = REPO_ROOT / "var"

_DB_COUNTER = itertools.count(1)
_DB_LOCK = threading.Lock()


def new_scratch_directory(prefix: str = "api-tests") -> Path:
    """A fresh scratch directory under the gitignored ``var/`` tree."""
    with _DB_LOCK:
        index = next(_DB_COUNTER)
    directory = SCRATCH_ROOT / f"{prefix}-{index}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def new_database_file(directory: Path) -> str:
    """A fresh (not yet created) database path inside ``directory``."""
    return str(directory / f"routepilot-{uuid.uuid4().hex[:12]}.db")


def cleanup_scratch_root() -> None:
    """Remove the whole scratch tree.

    Every test class removes its own directory again; this is the belt-and-braces sweep that also
    covers a directory left behind by a killed run, so the suite can prove it leaves no artifact.
    Attach it as ``tearDownModule`` in a module to run it after that module finishes.
    """
    shutil.rmtree(SCRATCH_ROOT, ignore_errors=True)


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

    def setUp(self) -> None:
        self.scratch = new_scratch_directory()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.services = ApiServices(new_database_file(self.scratch))
        self.addCleanup(self.services.close)
        self.server = None

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
            with urllib.request.urlopen(request, timeout=10) as response:
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

    # -- convenience ----------------------------------------------------- #
    def create_demo_plan(self) -> dict[str, Any]:
        """``POST /api/plans`` and return the plan payload, asserting that it was created."""
        response = self.post("/api/plans", body={})
        self.assertEqual(response.status, 201, msg=response.text)
        return response.json()["data"]


class ServerBackedTestCase(ApiServerTestCase):
    """A test case that starts its server (on an ephemeral loopback port) for every test."""

    def setUp(self) -> None:
        super().setUp()
        self.base_url = self.start_server()
