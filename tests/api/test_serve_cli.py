"""Opt-in smoke test for the ``python -m api.serve`` entry point (U13 deliverable 1).

Skipped unless ``ROUTEPILOT_SLOW_TESTS`` is set, because it starts a **real subprocess** server on
an ephemeral loopback port and then stops it. The default suite still covers the same code paths
in process (``tests/api/test_http_server.py``), plus the argument surface and the database
preparation here, which need no server at all.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from api.serve import build_parser, main
from tests.api.support import cleanup_scratch_root, new_database_file, new_scratch_directory

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

REPO_ROOT = Path(__file__).resolve().parents[2]
SLOW = bool(os.environ.get("ROUTEPILOT_SLOW_TESTS"))


class ServeArgumentTests(unittest.TestCase):
    """The documented command-line surface: host, port, db and static root."""

    def test_defaults_bind_loopback_on_port_8000_with_a_gitignored_database(self) -> None:
        arguments = build_parser().parse_args([])
        self.assertEqual(arguments.host, "127.0.0.1")
        self.assertEqual(arguments.port, 8000)
        self.assertEqual(arguments.db, "var/routepilot.db")
        self.assertTrue(arguments.static_root.endswith("web"))
        self.assertIs(arguments.quiet, False)

    def test_options_are_documented_and_parsed(self) -> None:
        arguments = build_parser().parse_args(
            ["--host", "0.0.0.0", "--port", "0", "--db", "var/other.db", "--quiet"]
        )
        self.assertEqual(arguments.host, "0.0.0.0")
        self.assertEqual(arguments.port, 0)
        self.assertEqual(arguments.db, "var/other.db")
        self.assertIs(arguments.quiet, True)

    def test_an_out_of_range_port_is_refused_before_anything_is_started(self) -> None:
        self.assertEqual(main(["--port", "70000"]), 2)

    def test_preparing_the_database_creates_the_gitignored_parent_directory(self) -> None:
        from api.serve import _prepare_database

        scratch = new_scratch_directory("api-serve")
        self.addCleanup(shutil.rmtree, scratch, True)
        target = Path(new_database_file(scratch)) / "nested" / "routepilot.db"
        _prepare_database(str(target))
        self.assertTrue(target.parent.is_dir())
        self.assertFalse(target.exists())
        # An in-process identifier invents no directory at all.
        _prepare_database(":memory:")
        _prepare_database("file:x?mode=memory&cache=shared")


class ServeSmokeTests(unittest.TestCase):
    """Start the real CLI, call the health endpoint over HTTP, then stop it."""

    @unittest.skipUnless(SLOW, "set ROUTEPILOT_SLOW_TESTS=1 to run the CLI smoke test")
    def test_the_cli_serves_health_and_prints_its_url(self) -> None:
        scratch = new_scratch_directory("api-serve-smoke")
        self.addCleanup(shutil.rmtree, scratch, True)
        database = new_database_file(scratch)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "api.serve",
                "--port",
                "0",
                "--db",
                database,
                "--static-root",
                str(scratch / "web"),
            ],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.wait)
        try:
            url = self._read_serving_url(process)
            with urllib.request.urlopen(f"{url}/api/health", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["data_provenance"], "DEMO_SYNTHETIC")
        finally:
            process.terminate()
            process.wait(timeout=10)

    @staticmethod
    def _read_serving_url(process: subprocess.Popen) -> str:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                continue
            marker = "serving on "
            if marker in line:
                return line.split(marker, 1)[1].strip()
            if "cannot bind" in line:
                raise AssertionError(f"the CLI failed to bind: {line}")
        raise AssertionError("the CLI did not print its URL in time")


if __name__ == "__main__":
    unittest.main()
