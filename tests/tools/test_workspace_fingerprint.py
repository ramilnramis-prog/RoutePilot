"""Workspace fingerprint tool (docs/WORKFLOW.md, write-safety refinement).

The fingerprint is the mechanism that proves a Reviewer phase was read-only, so it is tested
against a throwaway repository created inside the ignored scratch root.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

from tools.workspace_fingerprint import GitUnavailableError, compute, main

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH = REPO_ROOT / "_scan_scratch" / "workspace-fingerprint"


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, check=True
    )
    return completed.stdout.decode("utf-8", "replace")


def _force_remove(path: Path) -> None:
    """Remove a throwaway git repository.

    Git object files are read-only, and on Windows ``shutil.rmtree`` then fails, so the write
    bit is restored first. Ignoring the remaining errors keeps a failed test from cascading.
    """
    if not path.exists():
        return
    for root, _directories, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                (Path(root) / name).chmod(stat.S_IWRITE)
    shutil.rmtree(path, ignore_errors=True)


class WorkspaceFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        _force_remove(SCRATCH)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.repo = SCRATCH

        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "pilot@localhost")
        _git(self.repo, "config", "user.name", "Pilot probe")
        _git(self.repo, "config", "core.autocrlf", "false")

        (self.repo / ".gitignore").write_text("*.log\n__pycache__/\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "init")

        self.baseline = compute(self.repo).digest

    def tearDown(self) -> None:
        _force_remove(SCRATCH)

    def test_identical_state_gives_an_identical_digest(self) -> None:
        self.assertEqual(compute(self.repo).digest, self.baseline)
        self.assertEqual(compute(self.repo).digest, self.baseline)

    def test_tracked_change_changes_the_digest_and_revert_restores_it(self) -> None:
        (self.repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
        changed = compute(self.repo).digest
        self.assertNotEqual(changed, self.baseline)

        (self.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        self.assertEqual(compute(self.repo).digest, self.baseline)

    def test_staged_change_changes_the_digest(self) -> None:
        (self.repo / "tracked.txt").write_text("staged modification\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        staged = compute(self.repo)
        self.assertNotEqual(staged.digest, self.baseline)
        self.assertNotEqual(staged.staged_sha256, "0" * 64)

    def test_new_untracked_file_changes_the_digest(self) -> None:
        (self.repo / "new_file.txt").write_text("hello\n", encoding="utf-8")
        self.assertNotEqual(compute(self.repo).digest, self.baseline)

    def test_untracked_content_change_changes_the_digest(self) -> None:
        new_file = self.repo / "new_file.txt"
        new_file.write_text("first\n", encoding="utf-8")
        first = compute(self.repo).digest

        new_file.write_text("second\n", encoding="utf-8")
        second = compute(self.repo).digest

        self.assertNotEqual(first, second)
        self.assertNotEqual(first, self.baseline)

    def test_ignored_files_do_not_change_the_digest(self) -> None:
        # A read-only review may legitimately run tests, which create caches.
        (self.repo / "cache.log").write_text("cache output\n", encoding="utf-8")
        (self.repo / "__pycache__").mkdir(exist_ok=True)
        (self.repo / "__pycache__" / "module.cpython-313.pyc").write_bytes(b"\x00\x01")
        self.assertEqual(compute(self.repo).digest, self.baseline)

    def test_computing_a_fingerprint_does_not_modify_the_workspace(self) -> None:
        before = compute(self.repo)
        compute(self.repo)
        after = compute(self.repo)
        self.assertEqual(before.digest, after.digest)
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")

    def test_untracked_files_are_listed_with_content_hashes(self) -> None:
        (self.repo / "nested").mkdir()
        # Written as bytes: text mode would translate \n to \r\n on Windows and the size would
        # depend on the platform.
        (self.repo / "nested" / "deep.txt").write_bytes(b"deep\n")
        fingerprint = compute(self.repo)
        paths = [entry["path"] for entry in fingerprint.untracked]
        self.assertEqual(paths, ["nested/deep.txt"])
        self.assertEqual(fingerprint.untracked[0]["size"], 5)

    def test_cli_prints_the_digest(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["--repo", str(self.repo)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(buffer.getvalue().strip(), self.baseline)

    def test_cli_json_mode_reports_components(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["--repo", str(self.repo), "--json"])
        self.assertEqual(exit_code, 0)
        self.assertIn('"digest"', buffer.getvalue())
        self.assertIn('"head"', buffer.getvalue())

    def test_a_non_repository_is_reported_not_guessed(self) -> None:
        # A plain subdirectory is NOT a valid negative case: `git -C <dir>` walks up and would
        # find the RoutePilot repository. An invalid .git marker stops that walk and fails.
        broken = SCRATCH / "invalid-repository"
        broken.mkdir(exist_ok=True)
        (broken / ".git").write_text("this is not a git dir\n", encoding="utf-8")

        with self.assertRaises(GitUnavailableError):
            compute(broken)

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(["--repo", str(broken)])
        self.assertEqual(exit_code, 2)
        self.assertIn("workspace fingerprint failed", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
