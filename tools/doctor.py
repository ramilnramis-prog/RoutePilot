#!/usr/bin/env python
"""RoutePilot environment doctor (decision D12).

Answers one question: *can this machine run RoutePilot correctly right now?* It reports, it does
not silently fix.

    python tools/doctor.py                  # dev mode: missing tzdata is a WARN
    python tools/doctor.py --mode strict    # CI/release: missing tzdata is a FAIL (exit 1)
    python tools/doctor.py --require-zone Europe/Moscow

Checks: Python version, IANA time zone database (source, version, fix command), required zones,
core purity (no HTTP/UI/storage/network imports), and the presence of the governance files.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow `python tools/doctor.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from core.time import tzdata  # noqa: E402 - path bootstrap must run first
from core.validation.errors import TZDATA_INSTALL_COMMAND  # noqa: E402
from tools.isolation_check import scan_core  # noqa: E402

__all__ = [
    "CheckResult",
    "FAIL",
    "INFO",
    "MIN_PYTHON",
    "OK",
    "REQUIRED_ZONES",
    "WARN",
    "format_results",
    "main",
    "run_checks",
]

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

#: RoutePilot targets the modern typing/dataclass stdlib features used across ``core``.
MIN_PYTHON = (3, 11)

#: Zones the product relies on: UTC always, a Moscow-based business, and a DST-observing zone
#: used by the strict-DST test suite.
REQUIRED_ZONES = ("UTC", "Europe/Moscow", "Europe/Berlin")

#: Governance files required by decision D27 / spec section 28.
REQUIRED_FILES = (
    "README.md",
    ".gitignore",
    ".env.example",
    "docs/PRODUCT_SPEC.md",
    "docs/DECISIONS.md",
    "docs/ARCHITECTURE.md",
    "docs/STORAGE_SCHEMA.md",
)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one environment check."""

    name: str
    status: str
    detail: str
    fix: str | None = None

    @property
    def is_failure(self) -> bool:
        return self.status == FAIL


def check_python(version_info: tuple[int, ...] = sys.version_info) -> CheckResult:
    """The interpreter must support the stdlib features ``core`` uses."""
    version = ".".join(str(part) for part in version_info[:3])
    required = ".".join(str(part) for part in MIN_PYTHON)
    if tuple(version_info[:2]) < MIN_PYTHON:
        return CheckResult(
            "python",
            FAIL,
            f"{version} is too old (>= {required} required)",
            fix=f"install Python {required}+ and re-run",
        )
    return CheckResult("python", OK, f"{version} (>= {required} required)")


def check_tzdata(report: tzdata.TZDataReport, *, mode: str = "dev") -> CheckResult:
    """Report the IANA database source; missing data is a WARN in dev and a FAIL in strict mode."""
    if report.status == "package":
        return CheckResult(
            "tzdata",
            OK,
            f"tzdata package, IANA version {report.iana_version or 'unknown'}",
        )
    if report.status == "system":
        detail = (
            f"system TZif tree, IANA version {report.iana_version or 'unknown'} - "
            f"{report.detail}"
        )
        return CheckResult(
            "tzdata",
            WARN,
            detail,
            fix=(
                "install the pure-data package so the IANA version is pinned and reproducible: "
                f"{TZDATA_INSTALL_COMMAND}"
            ),
        )
    detail = report.detail
    if report.fallback_candidate:
        detail += (
            f". Development fallback: set PYTHONTZPATH={report.fallback_candidate} "
            "(not a substitute for the package)"
        )
    return CheckResult(
        "tzdata",
        FAIL if mode == "strict" else WARN,
        detail,
        fix=TZDATA_INSTALL_COMMAND,
    )


def check_required_zones(
    report: tzdata.TZDataReport,
    zones: Sequence[str] = REQUIRED_ZONES,
    *,
    required: bool = False,
) -> CheckResult:
    """Every zone RoutePilot depends on must actually resolve.

    When no database is available at all, the default zone list is reported as ``INFO`` (the
    tzdata check already covers that situation). An *explicitly requested* zone is different:
    the caller needs that zone, so an unresolvable one is a ``FAIL`` (D12).
    """
    if not report.is_available:
        if required:
            return CheckResult(
                "required zones",
                FAIL,
                f"cannot resolve {', '.join(zones)}: no time zone database is available",
                fix=TZDATA_INSTALL_COMMAND,
            )
        return CheckResult(
            "iana zones",
            INFO,
            "skipped: no time zone database available",
        )
    missing: list[str] = []
    for zone in zones:
        try:
            tzdata.load_timezone(zone)
        except Exception as error:  # noqa: BLE001 - doctor reports, it does not raise
            missing.append(f"{zone} ({type(error).__name__})")
    if missing:
        return CheckResult(
            "iana zones",
            FAIL,
            f"cannot resolve: {', '.join(missing)}",
            fix=f"update the IANA database ({TZDATA_INSTALL_COMMAND})",
        )
    return CheckResult("iana zones", OK, f"resolved: {', '.join(zones)}")


def check_core_isolation(repo_root: Path | str = REPO_ROOT) -> CheckResult:
    """``core/`` must not import HTTP, UI, storage, network or outer layers (D1)."""
    violations = scan_core(repo_root)
    if violations:
        listing = "; ".join(violation.describe() for violation in violations[:5])
        more = "" if len(violations) <= 5 else f" (+{len(violations) - 5} more)"
        return CheckResult(
            "core purity",
            FAIL,
            f"{len(violations)} forbidden import(s): {listing}{more}",
            fix="move the offending code out of core/ or invert the dependency",
        )
    return CheckResult("core purity", OK, "no HTTP/UI/storage/network imports inside core/")


def check_governance_files(repo_root: Path | str = REPO_ROOT) -> CheckResult:
    """Documentation and safety files required by D27 / spec section 28."""
    repo_root = Path(repo_root)
    missing = [name for name in REQUIRED_FILES if not (repo_root / name).exists()]
    if missing:
        return CheckResult(
            "governance",
            FAIL,
            f"missing: {', '.join(missing)}",
            fix="restore the required files (spec section 28)",
        )
    return CheckResult("governance", OK, f"{len(REQUIRED_FILES)} required files present")


def run_checks(
    *,
    mode: str = "dev",
    report: tzdata.TZDataReport | None = None,
    python_version: tuple[int, ...] | None = None,
    repo_root: Path | str = REPO_ROOT,
) -> tuple[CheckResult, ...]:
    """Run every check. Injectable inputs keep ``doctor`` testable without a broken machine."""
    report = report if report is not None else tzdata.probe_tzdata()
    python_version = python_version if python_version is not None else tuple(sys.version_info)
    return (
        check_python(python_version),
        check_tzdata(report, mode=mode),
        check_required_zones(report),
        check_core_isolation(repo_root),
        check_governance_files(repo_root),
    )


def format_results(results: Sequence[CheckResult], *, mode: str) -> str:
    """Human-readable report: aligned statuses, then the fixes, then a summary."""
    width = max(len(result.name) for result in results) if results else 0
    lines = [f"RoutePilot doctor - mode: {mode}", ""]
    for result in results:
        lines.append(f"[{result.status:<4}] {result.name:<{width}}  {result.detail}")
    fixes = [result for result in results if result.fix]
    if fixes:
        lines.append("")
        lines.append("Suggested fixes:")
        for result in fixes:
            lines.append(f"  - {result.name}: {result.fix}")
    failures = sum(1 for result in results if result.status == FAIL)
    warnings = sum(1 for result in results if result.status == WARN)
    lines.append("")
    lines.append(
        f"{len(results)} check(s): {failures} failure(s), {warnings} warning(s)"
    )
    if failures:
        lines.append("Result: FAIL")
    elif warnings:
        lines.append("Result: OK with warnings")
    else:
        lines.append("Result: OK")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code (1 when any check failed)."""
    parser = argparse.ArgumentParser(
        prog="doctor",
        description="Check whether this machine can run RoutePilot correctly.",
    )
    parser.add_argument(
        "--mode",
        choices=("dev", "strict"),
        default=os.environ.get("ROUTEPILOT_DOCTOR_MODE", "dev"),
        help="dev: warn about missing tzdata; strict: fail (default: %(default)s)",
    )
    parser.add_argument(
        "--require-zone",
        action="append",
        default=None,
        metavar="ZONE",
        help="additional IANA zone that must resolve (repeatable)",
    )
    arguments = parser.parse_args(argv)

    report = tzdata.probe_tzdata()
    results = list(run_checks(mode=arguments.mode, report=report))
    if arguments.require_zone:
        extra = check_required_zones(
            report, tuple(arguments.require_zone), required=True
        )
        extra = CheckResult("required zones", extra.status, extra.detail, fix=extra.fix)
        results.append(extra)

    print(format_results(results, mode=arguments.mode))
    return 1 if any(result.is_failure for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
