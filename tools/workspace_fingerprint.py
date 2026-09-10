#!/usr/bin/env python
"""Deterministic workspace fingerprint for the Builder -> Reviewer handoff.

Why this exists
---------------
``git status`` alone cannot prove the Reviewer was read-only: by the time the Reviewer
runs, the Builder has already produced a legitimately dirty tree, so "the tree is dirty"
says nothing about *who* made it dirty. The review loop therefore compares a fingerprint of
the exact working-tree state before and after every review phase (docs/WORKFLOW.md,
write-safety refinement).

What the fingerprint covers
---------------------------
* tracked, unstaged changes      -> ``git diff --binary --no-color``
* staged changes                 -> ``git diff --cached --binary --no-color``
* every untracked file           -> path, size and content SHA-256, sorted by path
* the HEAD commit id

Ignored files are deliberately excluded (``git status`` omits them by default): a legitimate
read-only review may run tests, and tests create caches such as ``__pycache__/`` or
``_scan_scratch/``. Including them would make the fingerprint unstable for correct behaviour.

Contract
--------
* identical tree state -> identical digest, independent of file-system enumeration order;
* any tracked, staged or untracked change -> a different digest;
* running this tool never modifies the workspace.

Usage
-----
    python tools/workspace_fingerprint.py [--repo PATH] [--json]

Prints a 64-character hex digest, or with ``--json`` the components alongside it.
Exit codes: 0 success, 2 the repository could not be read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = ["GitUnavailableError", "WorkspaceFingerprint", "compute", "main"]


class GitUnavailableError(RuntimeError):
    """git could not be run, or refused the repository."""


@dataclass(frozen=True)
class WorkspaceFingerprint:
    """The hashed components plus the resulting digest."""

    digest: str
    head: str | None
    unstaged_sha256: str
    staged_sha256: str
    untracked: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "head": self.head,
            "unstaged_sha256": self.unstaged_sha256,
            "staged_sha256": self.staged_sha256,
            "untracked": list(self.untracked),
        }


def _run_git(repo: Path, *arguments: str, check: bool = True) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            check=False,
        )
    except OSError as error:  # git not installed / not executable
        raise GitUnavailableError(f"could not run git: {error}") from error
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GitUnavailableError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): {detail}"
        )
    return completed.stdout


def _head_commit(repo: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None  # a repository without commits has no HEAD; that is not an error here
    return completed.stdout.decode("utf-8", "replace").strip() or None


def _untracked_paths(repo: Path) -> list[str]:
    """Untracked file paths, parsed from NUL-separated porcelain output.

    ``-uall`` lists every file inside an untracked directory individually, so the fingerprint
    cannot miss a nested new file.
    """
    status = _run_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = status.split(b"\x00")
    untracked: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2].decode("ascii", "replace")
        path = record[3:]
        if code[:1] in {"R", "C"}:
            # A rename/copy entry carries the source path in the following record.
            index += 1
        if code == "??":
            untracked.append(os.fsdecode(path))
    return sorted(untracked)


def _untracked_component(repo: Path, paths: Sequence[str]) -> list[dict[str, object]]:
    component: list[dict[str, object]] = []
    for path in paths:
        entry: dict[str, object] = {"path": path}
        try:
            content = (repo / path).read_bytes()
        except OSError as error:
            entry["size"] = None
            entry["sha256"] = f"unreadable:{type(error).__name__}"
        else:
            entry["size"] = len(content)
            entry["sha256"] = hashlib.sha256(content).hexdigest()
        component.append(entry)
    return component


def compute(repo: Path | str = Path(".")) -> WorkspaceFingerprint:
    """Compute the fingerprint of ``repo``'s working tree.

    Raises:
        GitUnavailableError: git could not be run, or ``repo`` is not a repository.
    """
    repo = Path(repo).resolve()
    head = _head_commit(repo)
    unstaged = _run_git(repo, "diff", "--binary", "--no-color")
    staged = _run_git(repo, "diff", "--cached", "--binary", "--no-color")
    untracked = _untracked_component(repo, _untracked_paths(repo))

    unstaged_sha256 = hashlib.sha256(unstaged).hexdigest()
    staged_sha256 = hashlib.sha256(staged).hexdigest()

    payload = {
        "head": head,
        "staged_sha256": staged_sha256,
        "unstaged_sha256": unstaged_sha256,
        "untracked": untracked,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return WorkspaceFingerprint(
        digest=digest,
        head=head,
        unstaged_sha256=unstaged_sha256,
        staged_sha256=staged_sha256,
        untracked=tuple(untracked),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workspace_fingerprint",
        description="Print a deterministic fingerprint of a git working tree.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repository path (default: the current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the hashed components as well as the digest",
    )
    arguments = parser.parse_args(argv)

    try:
        fingerprint = compute(arguments.repo)
    except GitUnavailableError as error:
        print(f"workspace fingerprint failed: {error}", file=sys.stderr)
        return 2

    if arguments.json:
        print(json.dumps(fingerprint.as_dict(), indent=2, sort_keys=True))
    else:
        print(fingerprint.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
