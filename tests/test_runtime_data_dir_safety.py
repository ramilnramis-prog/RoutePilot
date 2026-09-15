"""H2 guard: the suite must be structurally incapable of deleting the runtime data directory (D40).

The defect this module locks out: the suite's scratch root used to **be** the application's runtime
data directory ``var/``, and ``tearDownModule`` swept it with ``shutil.rmtree(SCRATCH_ROOT,
ignore_errors=True)``. A full-suite run therefore deleted the whole ``var/`` tree - including a live
``python -m api.serve`` server's ``var/routepilot.db`` with its plans, the driver's selection, and
the append-only run history the product documents as immutable audit (D38). Afterwards every request
answered ``HTTP 500 internal_error`` / ``OperationalError: unable to open database file``.

Every guard here must **fail** if that behaviour is reintroduced. Nothing in this module ever creates
or touches the real ``var/routepilot.db``: the sweeps are pointed at isolated stand-in runtime
directories under the suite-owned scratch root.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from time import monotonic, sleep

from api.services import DEFAULT_DB_PATH
from tests import REPO_ROOT, RUNTIME_DATA_DIR, SUITE_SCRATCH_ROOT
from tests.api import support
from tests.api.support import (
    SCRATCH_ROOT,
    ScratchRootError,
    cleanup_scratch_root,
    new_scratch_directory,
)

#: The marker directory the suite owns directly under a runtime data directory (D40).
MARKER_NAME = "tests"

#: The runtime database path derived from the **product** constant, read-only: the directory that
#: holds it is the application's runtime data directory, whoever spells it out.
RUNTIME_DB_PATH = Path(DEFAULT_DB_PATH)

#: A foreign runtime file, deliberately shaped like the live server's database.
SENTINEL_TEXT = "foreign runtime data"


def _stand_in_runtime_directory(prefix: str = "runtime-data-dir-stand-in") -> Path:
    """An isolated stand-in for the runtime data directory, under the suite's scratch root.

    It has the same shape as ``var/`` (a directory holding a foreign runtime file plus a ``tests``
    marker subtree) without ever being ``var/``.
    """
    stand_in = new_scratch_directory(prefix)
    (stand_in / "routepilot.db").write_text(SENTINEL_TEXT, encoding="utf-8")
    return stand_in


def _best_effort_remove(path: Path, seconds: float = 2.0) -> None:
    """Test-only cleanup: retry while Windows holds a scratch handle, then give up quietly.

    ``ignore_errors=True`` is deliberately **not** used in the code under test; this helper belongs to
    the guard's own tear-down, where a leftover scratch directory is a hygiene issue rather than an
    assertion.
    """
    deadline = monotonic() + seconds
    while True:
        try:
            support.shutil.rmtree(path)
            return
        except OSError:
            if monotonic() >= deadline:
                return
            sleep(0.05)


class RuntimeDirectorySafetyTests(unittest.TestCase):
    """The two behaviour guards: the sweep spares foreign runtime data, and refuses bad targets."""

    def setUp(self) -> None:
        self.runtime_dir = _stand_in_runtime_directory()
        # Belt and braces for a failing test; the sweep itself removes the marker subtree.
        self.addCleanup(cleanup_scratch_root)
        self.sentinel = self.runtime_dir / "routepilot.db"

    def seed_suite_marker_subtree(self) -> Path:
        """The suite-owned ``tests`` marker subtree of the stand-in, with its scratch junk."""
        marker = self.runtime_dir / MARKER_NAME
        junk = marker / "api-tests-1-deadbeef"
        junk.mkdir(parents=True, exist_ok=True)
        (junk / "routepilot-scratch.db").write_text("suite scratch", encoding="utf-8")
        return marker

    def test_the_sweep_removes_only_the_suite_marker_subtree(self) -> None:
        """SENTINEL: the foreign runtime file and the runtime directory itself survive the sweep."""
        marker = self.seed_suite_marker_subtree()
        self.assertTrue(self.sentinel.is_file(), msg="the sentinel runtime database is missing")
        self.assertTrue(marker.is_dir(), msg="the suite marker subtree was not seeded")

        cleanup_scratch_root(self.runtime_dir)

        self.assertFalse(marker.is_dir(), msg="the suite marker subtree was NOT removed")
        self.assertTrue(self.sentinel.is_file(), msg="the sweep DESTROYED a foreign runtime file")
        self.assertEqual(
            self.sentinel.read_text(encoding="utf-8"),
            SENTINEL_TEXT,
            msg="the sweep modified a foreign runtime file",
        )
        self.assertTrue(
            self.runtime_dir.is_dir(), msg="the sweep DESTROYED the runtime data directory"
        )

    def test_the_sweep_refuses_a_target_that_is_not_an_owned_marker_directory(self) -> None:
        """STRUCTURAL: the sweep refuses the runtime root, the repository root, and foreign paths.

        The stand-in's own root, the repository root and a directory outside the runtime data
        directory are all targets the suite does not own. Pointing the sweep anywhere that does not
        resolve to the ``tests`` marker directory directly under the runtime data directory must
        raise :class:`ScratchRootError` and delete **nothing**.
        """
        marker = self.seed_suite_marker_subtree()
        before = sorted(path.name for path in self.runtime_dir.iterdir())
        # The suite's own scratch boundary file: no refusal may touch it either.
        boundary = SCRATCH_ROOT / "boundary-must-survive.db"
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        boundary.write_text("boundary", encoding="utf-8")
        self.addCleanup(boundary.unlink, True)

        # Every one of these is a target the suite does not own. ``REPO_ROOT`` is the deepest
        # available foreign directory - this environment's ``%TEMP%`` cannot create a nested
        # directory - and it is exactly the "outside the runtime data directory" refusal.
        refusals = (
            ("the repository root", REPO_ROOT),
            # Its deletion target would be the repository's own ``tests`` package - not owned.
            ("the repository root's parent", REPO_ROOT.parent),
        )
        for label, target in refusals:
            with self.subTest(target=label):
                with self.assertRaises(ScratchRootError):
                    cleanup_scratch_root(target)

        self.assertTrue(self.sentinel.is_file(), msg="a refused sweep deleted the sentinel file")
        self.assertEqual(
            sorted(path.name for path in self.runtime_dir.iterdir()),
            before,
            msg="a refused sweep deleted entries it does not own",
        )
        self.assertTrue(marker.is_dir(), msg="a refused sweep deleted the marker subtree")
        self.assertTrue(boundary.is_file(), msg="a refused sweep deleted the suite boundary file")
        self.assertTrue((REPO_ROOT / "tests").is_dir(), msg="a refused sweep deleted tests/")

        # And the sweep must never remove a runtime directory root itself, only its marker child:
        # after a successful sweep the stand-in root is still there.
        cleanup_scratch_root(self.runtime_dir)
        self.assertTrue(self.runtime_dir.is_dir())
        self.assertTrue(self.sentinel.is_file())

    def test_the_suite_scratch_root_is_a_descendant_never_the_runtime_directory(self) -> None:
        """STRUCTURAL: the suite root is a proper descendant of the runtime data directory."""
        runtime = RUNTIME_DATA_DIR.resolve()
        suite_root = SCRATCH_ROOT.resolve()

        self.assertNotEqual(SCRATCH_ROOT, runtime, msg="the suite root IS the runtime directory")
        self.assertNotEqual(SCRATCH_ROOT, RUNTIME_DATA_DIR)
        self.assertFalse(
            runtime.is_relative_to(suite_root),
            msg="the suite root is an ANCESTOR of the runtime directory",
        )
        self.assertTrue(
            suite_root.is_relative_to(runtime),
            msg="the suite root is not a descendant of the runtime data directory",
        )
        self.assertEqual(suite_root.parent, runtime, msg="the suite root is not directly under var/")
        self.assertEqual(SUITE_SCRATCH_ROOT.resolve(), suite_root)

    def test_the_runtime_directory_is_the_directory_holding_the_product_database(self) -> None:
        """STRUCTURAL: the runtime directory is derived from the product's own database constant."""
        self.assertFalse(RUNTIME_DB_PATH.is_absolute(), msg="DEFAULT_DB_PATH must stay repo-relative")
        self.assertEqual(RUNTIME_DB_PATH.name, "routepilot.db")

        # The directory holding the frozen runtime database path, derived from the product constant.
        runtime = (REPO_ROOT / RUNTIME_DB_PATH).parent.resolve()

        self.assertEqual(runtime, RUNTIME_DATA_DIR.resolve(), msg="RUNTIME_DATA_DIR is not var/")
        self.assertEqual(runtime.name, "var")
        self.assertNotEqual(SCRATCH_ROOT.resolve(), runtime, msg="the suite root IS var/")
        self.assertFalse(
            runtime.is_relative_to(SCRATCH_ROOT.resolve()),
            msg="the suite root is an ANCESTOR of var/",
        )
        self.assertTrue(
            (REPO_ROOT / RUNTIME_DB_PATH).is_relative_to(runtime),
            msg="the product database path is not inside the runtime directory",
        )
        self.assertTrue(
            SCRATCH_ROOT.resolve().is_relative_to(runtime),
            msg="the suite root is not a proper descendant of the runtime data directory",
        )
        self.assertNotEqual(SCRATCH_ROOT.resolve(), runtime)
        self.assertNotEqual(SUITE_SCRATCH_ROOT, RUNTIME_DATA_DIR)


class ScratchRootErrorTests(unittest.TestCase):
    """The refusal is a real, typed error and nothing is deleted when it is raised."""

    def test_a_sweep_pointed_outside_the_runtime_directory_is_refused(self) -> None:
        """The repository root is not the runtime directory: the sweep refuses it and deletes none."""
        boundary = SCRATCH_ROOT / "outside-refusal-boundary.db"
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        boundary.write_text("boundary", encoding="utf-8")
        self.addCleanup(boundary.unlink, True)

        with self.assertRaises(ScratchRootError) as caught:
            cleanup_scratch_root(REPO_ROOT)

        self.assertIn("refusing", str(caught.exception))
        self.assertTrue(boundary.is_file(), msg="the refused sweep deleted a suite file")
        self.assertTrue((REPO_ROOT / "tests").is_dir(), msg="the refused sweep swept the repo")

    def test_the_refusal_is_a_dedicated_error_type(self) -> None:
        self.assertTrue(issubclass(ScratchRootError, RuntimeError))
        with self.assertRaises(ScratchRootError):
            cleanup_scratch_root(REPO_ROOT)
        self.assertTrue((REPO_ROOT / ".gitignore").is_file())
        self.assertTrue((REPO_ROOT / "tests").is_dir(), msg="the repository was swept")


class TransientLockTests(unittest.TestCase):
    """Dropping ``ignore_errors=True`` must not turn a transient Windows lock into a hidden skip.

    A sweep can run while the last request's database handle is still closing, and Windows then
    refuses to unlink that scratch file (``WinError 32``). Only that transient sharing violation is
    retried, for a bounded time; any other error propagates, so no refusal and no real failure is
    ever swallowed (D40).
    """

    def setUp(self) -> None:
        self.runtime_dir = _stand_in_runtime_directory("runtime-lock-stand-in")
        self.addCleanup(_best_effort_remove, self.runtime_dir)
        self.marker = self.runtime_dir / MARKER_NAME
        (self.marker / "junk").mkdir(parents=True)
        (self.marker / "junk" / "routepilot-scratch.db").write_text("x", encoding="utf-8")
        self.real_rmtree = support.shutil.rmtree

    def _patch_rmtree(self, fake) -> None:
        support.shutil.rmtree = fake
        self.addCleanup(setattr, support.shutil, "rmtree", self.real_rmtree)

    def test_a_transient_sharing_violation_is_retried_until_the_scratch_is_gone(self) -> None:
        calls: list[Path] = []

        def flaky(target, *args, **kwargs):
            calls.append(Path(target))
            if len(calls) == 1:
                raise PermissionError(13, "in use", str(target), 32)
            return self.real_rmtree(target, *args, **kwargs)

        self._patch_rmtree(flaky)
        cleanup_scratch_root(self.runtime_dir)

        self.assertEqual(len(calls), 2, msg="the sharing violation was not retried exactly once")
        self.assertFalse(self.marker.is_dir(), msg="the scratch subtree survived the sweep")
        self.assertTrue(
            (self.runtime_dir / "routepilot.db").is_file(),
            msg="the retry destroyed a foreign runtime file",
        )

    def test_a_non_sharing_error_is_never_swallowed(self) -> None:
        def failing(target, *args, **kwargs):
            raise OSError(2, "no such file", str(target), 2)

        self._patch_rmtree(failing)
        with self.assertRaises(OSError):
            cleanup_scratch_root(self.runtime_dir)
        self.assertTrue(self.marker.is_dir(), msg="the failing sweep deleted the scratch anyway")


if __name__ == "__main__":
    unittest.main()
