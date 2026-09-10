"""ServiceWindow value object rules (D28, spec sections 7 and 17)."""

from __future__ import annotations

import unittest
from datetime import time, timezone

from core.model.service_window import (
    DEFAULT_WINDOW_END_POLICY,
    ServiceWindow,
    WindowEndPolicy,
    WindowKind,
)
from core.validation.errors import InvalidServiceWindowError


class ServiceWindowTests(unittest.TestCase):
    def test_fixed_requires_both_local_times(self) -> None:
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow(WindowKind.FIXED, time(8, 0), None)
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow(WindowKind.FIXED, None, time(18, 0))

    def test_fixed_window_is_accepted(self) -> None:
        window = ServiceWindow.fixed(time(8, 0), time(18, 0))
        self.assertTrue(window.is_fixed)
        self.assertTrue(window.is_known)
        self.assertEqual(window.start_local, time(8, 0))
        self.assertEqual(window.end_local, time(18, 0))
        self.assertEqual(window.describe(), "08:00-18:00")

    def test_fixed_window_needs_start_before_end(self) -> None:
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow.fixed(time(18, 0), time(8, 0))
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow.fixed(time(8, 0), time(8, 0))

    def test_overnight_window_is_rejected_instead_of_interpreted(self) -> None:
        # Open item 1 in DECISIONS.md: 22:00-02:00 must not be silently read as next-day close.
        with self.assertRaises(InvalidServiceWindowError) as context:
            ServiceWindow.fixed(time(22, 0), time(2, 0))
        self.assertIn("Overnight", str(context.exception))

    def test_non_fixed_windows_carry_no_times(self) -> None:
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow(WindowKind.UNKNOWN, time(8, 0), time(18, 0))
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow(WindowKind.UNRESTRICTED, time(8, 0), time(18, 0))

    def test_unrestricted_and_unknown_are_distinct_states(self) -> None:
        unrestricted = ServiceWindow.unrestricted()
        unknown = ServiceWindow.unknown()
        self.assertNotEqual(unrestricted, unknown)
        self.assertNotEqual(unrestricted.window_kind, unknown.window_kind)
        # Known to be always accessible vs. missing information.
        self.assertTrue(unrestricted.is_known)
        self.assertFalse(unknown.is_known)
        self.assertFalse(unrestricted.is_fixed)
        self.assertFalse(unknown.is_fixed)
        self.assertEqual(unknown.describe(), "hours unknown")

    def test_kind_is_coerced_from_string(self) -> None:
        window = ServiceWindow("fixed", time(8, 0), time(18, 0))
        self.assertIs(window.window_kind, WindowKind.FIXED)

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow("whenever", None, None)

    def test_wall_clock_times_must_not_carry_tzinfo(self) -> None:
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow.fixed(time(8, 0, tzinfo=timezone.utc), time(18, 0))


class WindowEndPolicyTests(unittest.TestCase):
    """D29: what the end of a window means is explicit, and a stop may override the plan default."""

    def test_default_policy_is_the_conservative_one(self) -> None:
        self.assertIs(
            DEFAULT_WINDOW_END_POLICY, WindowEndPolicy.SERVICE_FINISH_BEFORE_END
        )

    def test_stop_without_an_override_inherits_the_plan_default(self) -> None:
        window = ServiceWindow.fixed(time(8, 0), time(18, 0))
        self.assertIsNone(window.window_end_policy)
        self.assertIs(
            window.effective_end_policy(DEFAULT_WINDOW_END_POLICY), DEFAULT_WINDOW_END_POLICY
        )

    def test_stop_override_wins_over_the_plan_default(self) -> None:
        window = ServiceWindow.fixed(
            time(8, 0),
            time(18, 0),
            window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END,
        )
        self.assertIs(
            window.effective_end_policy(DEFAULT_WINDOW_END_POLICY),
            WindowEndPolicy.SERVICE_START_BEFORE_END,
        )
        self.assertIn("stop override", window.describe_end_policy(DEFAULT_WINDOW_END_POLICY))

    def test_policy_is_coerced_from_a_string(self) -> None:
        window = ServiceWindow.fixed(
            time(8, 0), time(18, 0), window_end_policy="service_start_before_end"
        )
        self.assertIs(window.window_end_policy, WindowEndPolicy.SERVICE_START_BEFORE_END)

    def test_unknown_policy_is_rejected(self) -> None:
        with self.assertRaises(InvalidServiceWindowError):
            ServiceWindow.fixed(time(8, 0), time(18, 0), window_end_policy="whenever")

    def test_windows_without_an_end_must_not_carry_a_policy(self) -> None:
        for kind in (WindowKind.UNKNOWN, WindowKind.UNRESTRICTED):
            with self.assertRaises(InvalidServiceWindowError):
                ServiceWindow(kind, None, None, WindowEndPolicy.SERVICE_START_BEFORE_END)


if __name__ == "__main__":
    unittest.main()
