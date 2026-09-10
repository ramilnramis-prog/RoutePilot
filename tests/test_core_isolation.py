"""Core purity is enforced by a check, not by memory (D1; spec sections 14, 22, 26)."""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from tools.isolation_check import scan_core, scan_directory

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Scratch packages are built inside the repository rather than the OS temp directory: the
#: sandboxed test environment does not reliably create nested directories under %TEMP%.
SCRATCH_ROOT = REPO_ROOT / "_scan_scratch"


class LiveCoreIsolationTests(unittest.TestCase):
    def test_core_has_no_forbidden_imports(self) -> None:
        violations = scan_core(REPO_ROOT)
        self.assertEqual(
            violations,
            (),
            msg="core must stay pure: " + "; ".join(v.describe() for v in violations),
        )


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = SCRATCH_ROOT / self._testMethodName
        self.package = self.workspace / "core"
        self.package.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_detects_network_storage_framework_and_layer_imports(self) -> None:
        (self.package / "sub").mkdir(exist_ok=True)
        (self.package / "bad.py").write_text(
            "import sqlite3\n"
            "import requests\n"
            "from storage import sqlite_repo\n"
            "from fastapi import FastAPI\n",
            encoding="utf-8",
        )
        (self.package / "sub" / "deep.py").write_text(
            "import http.client\nimport socket\n", encoding="utf-8"
        )
        violations = scan_directory(self.package)
        self.assertEqual(
            {violation.module for violation in violations},
            {"sqlite3", "requests", "storage", "fastapi", "http", "socket"},
        )
        self.assertTrue(all(violation.line > 0 for violation in violations))
        self.assertTrue(all(violation.reason for violation in violations))

    def test_layer_import_reason_names_the_dependency_direction(self) -> None:
        (self.package / "layer.py").write_text("import web\n", encoding="utf-8")
        violations = scan_directory(self.package)
        self.assertEqual(len(violations), 1)
        self.assertIn("dependency direction", violations[0].reason)

    def test_stdlib_and_relative_imports_are_allowed(self) -> None:
        (self.package / "good.py").write_text(
            "import json\n"
            "from datetime import datetime, timezone\n"
            "from core.model.ids import StopId\n"
            "from . import sibling\n",
            encoding="utf-8",
        )
        self.assertEqual(scan_directory(self.package), ())

    def test_syntax_errors_do_not_crash_the_scanner(self) -> None:
        (self.package / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        self.assertEqual(scan_directory(self.package), ())

    def test_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(scan_directory(Path("no-such-package")), ())


if __name__ == "__main__":
    unittest.main()
