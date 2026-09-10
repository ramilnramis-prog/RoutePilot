"""Strict DST validation (D3, spec sections 21 and 27.15/27.16).

A local wall-clock time that does not exist (DST gap) or occurs twice (ambiguous) is an explicit
error. It is never shifted forward and ``fold`` is never chosen for the user.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timezone

from core.model.service_window import ServiceWindow
from core.time import tz, tzdata
from core.validation.errors import (
    AmbiguousLocalTimeError,
    NonexistentLocalTimeError,
)

UTC = timezone.utc
BERLIN = "Europe/Berlin"
MOSCOW = "Europe/Moscow"

# Europe/Berlin 2026: DST starts 2026-03-29 (02:00 -> 03:00) and ends 2026-10-25 (03:00 -> 02:00).
GAP_DATE = date(2026, 3, 29)
AMBIGUOUS_DATE = date(2026, 10, 25)


class IanaDatabaseTests(unittest.TestCase):
    def test_an_iana_database_is_available(self) -> None:
        report = tzdata.probe_tzdata()
        self.assertTrue(
            report.is_available,
            msg=(
                "No IANA time zone database is reachable, so DST behaviour cannot be verified. "
                f"{report.detail}. Fix: {report.install_command}"
            ),
        )

    def test_database_source_is_reported(self) -> None:
        report = tzdata.probe_tzdata()
        self.assertIn(report.status, ("package", "system"))
        self.assertTrue(report.detail)
        if report.iana_version:
            self.assertIn(report.iana_version, report.detail)

    def test_required_zones_load(self) -> None:
        for zone in ("UTC", MOSCOW, BERLIN):
            self.assertIsNotNone(tzdata.load_timezone(zone))


class DstGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tzinfo = tzdata.load_timezone(BERLIN)

    def test_nonexistent_local_time_is_an_explicit_error(self) -> None:
        with self.assertRaises(NonexistentLocalTimeError) as context:
            tz.resolve_local_datetime(GAP_DATE, time(2, 30), self.tzinfo)
        self.assertIn("DST gap", str(context.exception))
        self.assertIn(BERLIN, str(context.exception))

    def test_service_window_in_a_gap_is_rejected_not_shifted(self) -> None:
        window = ServiceWindow.fixed(time(2, 30), time(5, 0))
        with self.assertRaises(NonexistentLocalTimeError):
            tz.resolve_service_window(window, GAP_DATE, self.tzinfo)

    def test_closing_time_in_a_gap_is_also_rejected(self) -> None:
        window = ServiceWindow.fixed(time(1, 0), time(2, 30))
        with self.assertRaises(NonexistentLocalTimeError):
            tz.resolve_service_window(window, GAP_DATE, self.tzinfo)


class DstAmbiguityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tzinfo = tzdata.load_timezone(BERLIN)

    def test_ambiguous_local_time_requires_explicit_disambiguation(self) -> None:
        with self.assertRaises(AmbiguousLocalTimeError) as context:
            tz.resolve_local_datetime(AMBIGUOUS_DATE, time(2, 30), self.tzinfo)
        error = context.exception
        self.assertEqual(len(error.candidates), 2)
        self.assertNotEqual(error.candidates[0], error.candidates[1])
        # Both candidates are genuinely the same wall-clock time -> a human must choose.
        for candidate in error.candidates:
            self.assertEqual(
                candidate.astimezone(self.tzinfo).replace(tzinfo=None),
                datetime(2026, 10, 25, 2, 30),
            )

    def test_fold_is_never_chosen_implicitly(self) -> None:
        for minute in (0, 30, 59):
            with self.assertRaises(AmbiguousLocalTimeError):
                tz.resolve_local_datetime(AMBIGUOUS_DATE, time(2, minute), self.tzinfo)

    def test_service_window_in_the_ambiguous_hour_is_rejected(self) -> None:
        window = ServiceWindow.fixed(time(2, 0), time(6, 0))
        with self.assertRaises(AmbiguousLocalTimeError):
            tz.resolve_service_window(window, AMBIGUOUS_DATE, self.tzinfo)


class NormalResolutionTests(unittest.TestCase):
    def test_unambiguous_local_time_resolves_to_utc(self) -> None:
        berlin = tzdata.load_timezone(BERLIN)
        resolved = tz.resolve_local_datetime(date(2026, 9, 11), time(8, 0), berlin)
        self.assertEqual(resolved, datetime(2026, 9, 11, 6, 0, tzinfo=UTC))

    def test_moscow_has_a_stable_offset(self) -> None:
        moscow = tzdata.load_timezone(MOSCOW)
        resolved = tz.resolve_local_datetime(date(2026, 9, 11), time(8, 0), moscow)
        self.assertEqual(resolved, datetime(2026, 9, 11, 5, 0, tzinfo=UTC))

    def test_local_date_uses_the_plan_timezone(self) -> None:
        moscow = tzdata.load_timezone(MOSCOW)
        instant = datetime(2026, 9, 10, 22, 0, tzinfo=UTC)  # 01:00 next day in Moscow
        self.assertEqual(tz.local_date_of(instant, moscow), date(2026, 9, 11))

    def test_windows_without_hours_resolve_to_nothing(self) -> None:
        moscow = tzdata.load_timezone(MOSCOW)
        for window in (ServiceWindow.unrestricted(), ServiceWindow.unknown()):
            self.assertIsNone(tz.resolve_service_window(window, GAP_DATE, moscow))

    def test_resolved_window_ordering_is_enforced(self) -> None:
        moscow = tzdata.load_timezone(MOSCOW)
        window = ServiceWindow.fixed(time(8, 0), time(18, 0))
        resolved = tz.resolve_service_window(window, date(2026, 9, 11), moscow)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertLess(resolved.open_at, resolved.close_at)


if __name__ == "__main__":
    unittest.main()
