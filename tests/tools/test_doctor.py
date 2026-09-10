"""``doctor`` behaviour (D12, spec sections 20 and 21)."""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from core.time import tzdata
from tools.doctor import (
    FAIL,
    INFO,
    OK,
    WARN,
    check_core_isolation,
    check_governance_files,
    check_python,
    check_required_zones,
    check_tzdata,
    format_results,
    main,
    run_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

MISSING_REPORT = tzdata.TZDataReport(
    status="missing",
    iana_version=None,
    search_path=(),
    fallback_candidate=r"C:\Program Files\Git\mingw64\share\zoneinfo",
    detail="no IANA time zone database is reachable",
)

PACKAGE_REPORT = tzdata.TZDataReport(
    status="package",
    iana_version="2026a",
    search_path=("/usr/share/zoneinfo",),
    fallback_candidate=None,
    detail="tzdata package provides the IANA database (version 2026a)",
)

SYSTEM_REPORT = tzdata.TZDataReport(
    status="system",
    iana_version="2026a",
    search_path=(r"C:\Program Files\Git\mingw64\share\zoneinfo",),
    fallback_candidate=r"C:\Program Files\Git\mingw64\share\zoneinfo",
    detail="IANA database found through the zoneinfo search path (version 2026a)",
)


class CheckTests(unittest.TestCase):
    def test_supported_python_version_passes(self) -> None:
        self.assertEqual(check_python((3, 13, 15)).status, OK)

    def test_old_python_version_fails_with_a_fix(self) -> None:
        result = check_python((3, 9, 0))
        self.assertEqual(result.status, FAIL)
        self.assertIn("3.11", result.detail)
        self.assertIsNotNone(result.fix)

    def test_missing_tzdata_warns_in_dev_mode(self) -> None:
        result = check_tzdata(MISSING_REPORT, mode="dev")
        self.assertEqual(result.status, WARN)
        self.assertIn("python -m pip install tzdata", result.fix or "")

    def test_missing_tzdata_fails_in_strict_mode(self) -> None:
        result = check_tzdata(MISSING_REPORT, mode="strict")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.fix, "python -m pip install tzdata")

    def test_missing_tzdata_reports_the_development_fallback(self) -> None:
        result = check_tzdata(MISSING_REPORT, mode="dev")
        self.assertIn("PYTHONTZPATH", result.detail)
        self.assertIn("not a substitute", result.detail)

    def test_package_source_is_ok_and_names_the_version(self) -> None:
        result = check_tzdata(PACKAGE_REPORT, mode="strict")
        self.assertEqual(result.status, OK)
        self.assertIn("2026a", result.detail)

    def test_system_source_warns_and_recommends_the_package(self) -> None:
        result = check_tzdata(SYSTEM_REPORT, mode="dev")
        self.assertEqual(result.status, WARN)
        self.assertIn("python -m pip install tzdata", result.fix or "")

    def test_required_zones_are_skipped_without_a_database(self) -> None:
        self.assertEqual(check_required_zones(MISSING_REPORT).status, INFO)

    def test_explicitly_required_zone_fails_without_a_database(self) -> None:
        result = check_required_zones(MISSING_REPORT, ("Europe/Moscow",), required=True)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.fix, "python -m pip install tzdata")

    def test_required_zones_check_matches_the_live_environment(self) -> None:
        result = check_required_zones(tzdata.probe_tzdata())
        self.assertIn(result.status, (OK, INFO))

    def test_core_isolation_passes_for_this_repository(self) -> None:
        self.assertEqual(check_core_isolation(REPO_ROOT).status, OK)

    def test_governance_files_are_present(self) -> None:
        result = check_governance_files(REPO_ROOT)
        self.assertEqual(result.status, OK, msg=result.detail)


class ReportTests(unittest.TestCase):
    def test_report_names_the_install_command(self) -> None:
        report = format_results(run_checks(mode="dev", report=MISSING_REPORT), mode="dev")
        self.assertIn("python -m pip install tzdata", report)
        self.assertIn("RoutePilot doctor", report)

    def test_report_summarises_outcome(self) -> None:
        self.assertIn(
            "Result: FAIL",
            format_results(run_checks(mode="strict", report=MISSING_REPORT), mode="strict"),
        )

    def test_healthy_environment_reports_ok(self) -> None:
        report = format_results(
            run_checks(mode="strict", report=PACKAGE_REPORT, python_version=(3, 13, 15)),
            mode="strict",
        )
        self.assertIn("Result:", report)


class CliTests(unittest.TestCase):
    def test_strict_mode_exit_code_reflects_the_environment(self) -> None:
        expected = 0 if tzdata.probe_tzdata().is_available else 1
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--mode", "strict"]), expected)

    def test_dev_mode_never_fails_on_missing_tzdata_alone(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--mode", "dev"]), 0)

    def test_extra_required_zone_is_checked(self) -> None:
        # An explicitly required zone must fail when nothing can resolve it (D12).
        expected = 0 if tzdata.probe_tzdata().is_available else 1
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["--mode", "dev", "--require-zone", "Europe/Moscow"]), expected
            )


if __name__ == "__main__":
    unittest.main()
