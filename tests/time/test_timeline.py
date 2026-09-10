"""Timeline arithmetic: ETA, waiting time, service start, lateness, feasibility.

Covers spec section 27 items 11-14 and the motivating scenario of spec section 1: the driver
loads at 04:00, customers open at 08:00, and a far stop that arrives just before opening beats a
near stop that would wait three hours. Travel data is synthetic and labelled ``DEMO_SYNTHETIC``.
"""

from __future__ import annotations

import unittest
from datetime import time, timedelta

from core.model.ids import StopId
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.solution import Feasibility, TimelineFlag, ViolationKind
from core.time.timeline import compute_timeline, resolve_service_duration
from core.validation.errors import (
    InvalidOrderError,
    MissingServiceDurationError,
    StopNotGeocodedError,
)
from tests.support import (
    FixedTravelMatrix,
    WAREHOUSE,
    build_plan,
    degrees_for,
    stop,
    utc,
)

T = 3600
M = 60

OPEN_WINDOW = ServiceWindow.fixed(time(8, 0), time(18, 0))


def stop_after(seconds: int, stop_id: str = "s1", **kwargs):
    """A stop whose synthetic travel time from the warehouse is exactly ``seconds``."""
    return stop(stop_id, WAREHOUSE[0] + degrees_for(seconds), WAREHOUSE[1], **kwargs)


class TimelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = FixedTravelMatrix()

    def timeline_for(self, plan, *order):
        return compute_timeline(
            plan=plan,
            order=[StopId(stop_id) for stop_id in order],
            travel_provider=self.matrix,
        )


class SyntheticMatrixSanityTests(TimelineTestCase):
    def test_fixture_travel_times_are_exact(self) -> None:
        # Guard: the scenario numbers below depend on this matrix being exact.
        for seconds in (45 * M, 3 * T + 55 * M, 4 * T + 30 * M, 5 * T):
            with self.subTest(seconds=seconds):
                plan = build_plan(stop_after(seconds))
                location = plan.stops[0].location
                self.assertIsNotNone(location)
                assert location is not None
                self.assertEqual(
                    self.matrix.travel_time_seconds(plan.departure_point, location), seconds
                )


class ScenarioTests(TimelineTestCase):
    """Departure 04:00 (Moscow), customers open 08:00."""

    def test_far_stop_arrives_just_before_opening(self) -> None:
        plan = build_plan(stop_after(3 * T + 55 * M, "far", window=OPEN_WINDOW))
        result = self.timeline_for(plan, "far")
        timeline = result.timelines[0]

        self.assertEqual(timeline.travel_time, 3 * T + 55 * M)
        self.assertEqual(timeline.estimated_arrival, utc(2026, 9, 11, 4, 55))  # 07:55 Moscow
        self.assertEqual(timeline.service_window_start, utc(2026, 9, 11, 5, 0))  # 08:00 Moscow
        self.assertEqual(timeline.waiting_time, 5 * M)
        self.assertEqual(timeline.service_start, utc(2026, 9, 11, 5, 0))
        self.assertEqual(timeline.estimated_departure, utc(2026, 9, 11, 5, 10))
        self.assertEqual(timeline.lateness, 0)
        self.assertIs(timeline.feasibility, Feasibility.FEASIBLE)
        self.assertFalse(result.has_infeasible_windows)

    def test_near_stop_waits_three_hours_fifteen(self) -> None:
        plan = build_plan(stop_after(45 * M, "near", window=OPEN_WINDOW))
        timeline = self.timeline_for(plan, "near").timelines[0]

        self.assertEqual(timeline.estimated_arrival, utc(2026, 9, 11, 1, 45))  # 04:45 Moscow
        self.assertEqual(timeline.waiting_time, 3 * T + 15 * M)
        self.assertEqual(timeline.service_start, utc(2026, 9, 11, 5, 0))

    def test_service_never_starts_before_opening(self) -> None:
        for seconds in (45 * M, 3 * T + 55 * M):
            with self.subTest(seconds=seconds):
                plan = build_plan(stop_after(seconds, window=OPEN_WINDOW))
                timeline = self.timeline_for(plan, "s1").timelines[0]
                self.assertGreaterEqual(timeline.service_start, timeline.estimated_arrival)
                assert timeline.service_window_start is not None
                self.assertGreaterEqual(timeline.service_start, timeline.service_window_start)
                self.assertEqual(
                    timeline.service_start,
                    timeline.estimated_arrival + timedelta(seconds=timeline.waiting_time),
                )

    def test_eta_calculation_is_deterministic(self) -> None:
        plan = build_plan(
            stop_after(45 * M, "a", window=OPEN_WINDOW),
            stop_after(2 * T + 10 * M, "b", window=OPEN_WINDOW),
        )
        first = self.timeline_for(plan, "a", "b")
        second = self.timeline_for(plan, "a", "b")
        self.assertEqual(first.timelines, second.timelines)
        # a: 45m of driving, then waits until 08:00; b is 1h25m further on from a.
        self.assertEqual(
            [timeline.estimated_arrival for timeline in first.timelines],
            [utc(2026, 9, 11, 1, 45), utc(2026, 9, 11, 6, 35)],
        )

    def test_each_leg_starts_when_the_previous_one_ends(self) -> None:
        plan = build_plan(
            stop_after(45 * M, "a", window=OPEN_WINDOW),
            stop_after(2 * T + 10 * M, "b", window=OPEN_WINDOW),
        )
        timelines = self.timeline_for(plan, "a", "b").timelines
        self.assertEqual(timelines[0].departure_from_previous, plan.departure_time)
        self.assertEqual(timelines[1].departure_from_previous, timelines[0].estimated_departure)


class WindowEndPolicyTests(TimelineTestCase):
    """D29: 'closes at' is an explicit choice, not an implicit assumption."""

    def setUp(self) -> None:
        super().setUp()
        # Arrives 08:50, 20 minutes of service -> finishes 09:10 against a 09:00 closing time.
        self.stop = stop_after(
            4 * T + 50 * M,
            "overtime",
            window=ServiceWindow.fixed(time(8, 0), time(9, 0)),
            service_duration=20 * M,
        )

    def test_finishing_after_closing_is_infeasible_under_the_default_finish_policy(self) -> None:
        plan = build_plan(self.stop)
        self.assertIs(
            plan.window_end_policy, WindowEndPolicy.SERVICE_FINISH_BEFORE_END
        )
        result = self.timeline_for(plan, "overtime")
        timeline = result.timelines[0]
        self.assertEqual(timeline.service_start, utc(2026, 9, 11, 5, 50))  # 08:50
        self.assertEqual(timeline.finish_overtime, 10 * M)
        self.assertEqual(timeline.lateness, 10 * M)  # lateness == finish miss under this policy
        self.assertIs(timeline.feasibility, Feasibility.INFEASIBLE)
        self.assertTrue(result.has_infeasible_windows)

    def test_finishing_after_closing_is_informational_under_the_start_policy(self) -> None:
        plan = build_plan(
            self.stop, window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END
        )
        result = self.timeline_for(plan, "overtime")
        timeline = result.timelines[0]
        self.assertEqual(timeline.lateness, 0)  # starting inside the window is enough
        self.assertEqual(timeline.finish_overtime, 10 * M)  # still recorded as information
        self.assertIs(timeline.feasibility, Feasibility.FEASIBLE)
        self.assertFalse(result.has_infeasible_windows)

    def test_starting_at_the_closing_minute_is_infeasible_under_the_default_policy(self) -> None:
        # Service starts exactly at 08:30 but would finish at 08:40, after closing.
        plan = build_plan(
            stop_after(4 * T + 30 * M, "edge", window=ServiceWindow.fixed(time(8, 0), time(8, 30)))
        )
        timeline = self.timeline_for(plan, "edge").timelines[0]
        self.assertEqual(timeline.start_lateness, 0)
        self.assertEqual(timeline.finish_overtime, 10 * M)
        self.assertIs(timeline.feasibility, Feasibility.INFEASIBLE)

    def test_starting_at_the_closing_minute_is_feasible_under_the_start_policy(self) -> None:
        plan = build_plan(
            stop_after(4 * T + 30 * M, "edge", window=ServiceWindow.fixed(time(8, 0), time(8, 30))),
            window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END,
        )
        timeline = self.timeline_for(plan, "edge").timelines[0]
        self.assertEqual(timeline.lateness, 0)
        self.assertEqual(timeline.finish_overtime, 10 * M)
        self.assertIs(timeline.feasibility, Feasibility.FEASIBLE)

    def test_a_stop_override_beats_the_plan_default(self) -> None:
        window = ServiceWindow.fixed(
            time(8, 0),
            time(9, 0),
            window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END,
        )
        plan = build_plan(
            stop_after(
                4 * T + 50 * M, "override", window=window, service_duration=20 * M
            )
        )
        self.assertIs(plan.window_end_policy, WindowEndPolicy.SERVICE_FINISH_BEFORE_END)
        timeline = self.timeline_for(plan, "override").timelines[0]
        self.assertIs(timeline.window_end_policy, WindowEndPolicy.SERVICE_START_BEFORE_END)
        self.assertIs(timeline.feasibility, Feasibility.FEASIBLE)

    def test_plan_default_applies_when_a_stop_has_no_override(self) -> None:
        plan = build_plan(
            self.stop, window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END
        )
        self.assertIsNone(plan.stops[0].service_window.window_end_policy)
        timeline = self.timeline_for(plan, "overtime").timelines[0]
        self.assertIs(timeline.window_end_policy, WindowEndPolicy.SERVICE_START_BEFORE_END)

    def test_specification_example_17_50_start_with_30_minutes_of_service(self) -> None:
        # window 08:00-18:00, service 30m, service start 17:50 -> finishes 18:20.
        # 13h50m of driving from 04:00 puts the ETA exactly at 17:50.
        def plan_with(policy: WindowEndPolicy):
            return build_plan(
                stop_after(
                    13 * T + 50 * M,
                    "late-day",
                    window=ServiceWindow.fixed(time(8, 0), time(18, 0)),
                    service_duration=30 * M,
                ),
                window_end_policy=policy,
            )

        finish_plan = plan_with(WindowEndPolicy.SERVICE_FINISH_BEFORE_END)
        finish_timeline = self.timeline_for(finish_plan, "late-day").timelines[0]
        self.assertEqual(finish_timeline.service_start, utc(2026, 9, 11, 14, 50))  # 17:50
        self.assertEqual(finish_timeline.estimated_departure, utc(2026, 9, 11, 15, 20))  # 18:20
        self.assertEqual(finish_timeline.lateness, 20 * M)
        self.assertIs(finish_timeline.feasibility, Feasibility.INFEASIBLE)

        start_plan = plan_with(WindowEndPolicy.SERVICE_START_BEFORE_END)
        start_timeline = self.timeline_for(start_plan, "late-day").timelines[0]
        self.assertEqual(start_timeline.lateness, 0)
        self.assertEqual(start_timeline.finish_overtime, 20 * M)
        self.assertIs(start_timeline.feasibility, Feasibility.FEASIBLE)

    def test_free_windows_carry_no_end_policy(self) -> None:
        for window in (ServiceWindow.unrestricted(), ServiceWindow.unknown()):
            plan = build_plan(stop_after(45 * M, "free", window=window))
            timeline = self.timeline_for(plan, "free").timelines[0]
            self.assertIsNone(timeline.window_end_policy)
            self.assertEqual(timeline.lateness, 0)
            self.assertEqual(timeline.finish_overtime, 0)


class InfeasibilityTests(TimelineTestCase):
    """A hard window miss is an explicit violation, never a hidden penalty (D13 amendment)."""

    def test_late_arrival_is_an_explicit_violation(self) -> None:
        # Window 08:00-08:30, five hours of driving, 10 minutes of service:
        # earliest service start is 09:00 and it would finish 09:10, i.e. 40 minutes after closing.
        plan = build_plan(
            stop_after(5 * T, "tight", window=ServiceWindow.fixed(time(8, 0), time(8, 30)))
        )
        result = self.timeline_for(plan, "tight")
        timeline = result.timelines[0]

        self.assertEqual(timeline.estimated_arrival, utc(2026, 9, 11, 6, 0))  # 09:00 Moscow
        self.assertEqual(timeline.finish_overtime, 40 * M)
        self.assertEqual(timeline.lateness, 40 * M)
        self.assertIs(timeline.feasibility, Feasibility.INFEASIBLE)
        self.assertTrue(result.has_infeasible_windows)
        self.assertEqual(result.infeasible_stop_ids, ("tight",))
        self.assertEqual(len(result.violations), 1)
        self.assertIs(result.violations[0].kind, ViolationKind.TIME_WINDOW_INFEASIBLE)
        self.assertIn("cannot be served within the permitted window", result.violations[0].message)

    def test_violation_message_names_the_applied_policy(self) -> None:
        plan = build_plan(
            stop_after(5 * T, "tight", window=ServiceWindow.fixed(time(8, 0), time(8, 30)))
        )
        message = self.timeline_for(plan, "tight").violations[0].message
        self.assertIn(WindowEndPolicy.SERVICE_FINISH_BEFORE_END.value, message)

        reverse_plan = build_plan(
            stop_after(5 * T, "tight", window=ServiceWindow.fixed(time(8, 0), time(8, 30))),
            window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END,
        )
        reverse_message = self.timeline_for(reverse_plan, "tight").violations[0].message
        self.assertIn(WindowEndPolicy.SERVICE_START_BEFORE_END.value, reverse_message)

    def test_explicit_violation_is_never_a_score(self) -> None:
        # The timeline reports infeasibility as a Violation object; there is no penalty field.
        plan = build_plan(
            stop_after(5 * T, "tight", window=ServiceWindow.fixed(time(8, 0), time(8, 30)))
        )
        timeline = self.timeline_for(plan, "tight").timelines[0]
        self.assertFalse(hasattr(timeline, "penalty"))


class WindowKindTests(TimelineTestCase):
    def test_unknown_window_creates_no_waiting_and_is_flagged(self) -> None:
        plan = build_plan(stop_after(45 * M, "s1", window=ServiceWindow.unknown()))
        result = self.timeline_for(plan, "s1")
        timeline = result.timelines[0]
        self.assertEqual(timeline.waiting_time, 0)
        self.assertEqual(timeline.lateness, 0)
        self.assertIn(TimelineFlag.WINDOW_UNKNOWN, timeline.flags)
        self.assertIsNone(timeline.service_window_start)
        self.assertFalse(result.has_infeasible_windows)

    def test_unknown_window_is_never_turned_into_invented_hours(self) -> None:
        unknown = ServiceWindow.unknown()
        self.assertIsNone(unknown.start_local)
        self.assertIsNone(unknown.end_local)
        self.assertEqual(unknown.describe(), "hours unknown")

    def test_unrestricted_window_creates_no_waiting_and_no_flag(self) -> None:
        plan = build_plan(stop_after(45 * M, "s1", window=ServiceWindow.unrestricted()))
        timeline = self.timeline_for(plan, "s1").timelines[0]
        self.assertEqual(timeline.waiting_time, 0)
        self.assertNotIn(TimelineFlag.WINDOW_UNKNOWN, timeline.flags)


class ServiceDurationTests(TimelineTestCase):
    def test_plan_default_is_used_when_the_stop_has_no_duration(self) -> None:
        plan = build_plan(
            stop_after(45 * M, "s1", window=OPEN_WINDOW, service_duration=None),
            default_service_duration=15 * M,
        )
        timeline = self.timeline_for(plan, "s1").timelines[0]
        self.assertEqual(timeline.service_duration, 15 * M)
        self.assertIn(TimelineFlag.SERVICE_DURATION_DEFAULTED, timeline.flags)

    def test_missing_duration_without_a_default_is_an_error(self) -> None:
        plan = build_plan(stop_after(45 * M, "s1", window=OPEN_WINDOW, service_duration=None))
        with self.assertRaises(MissingServiceDurationError):
            self.timeline_for(plan, "s1")

    def test_resolve_service_duration_reports_defaulting(self) -> None:
        plan = build_plan(
            stop_after(45 * M, "s1", service_duration=None), default_service_duration=900
        )
        duration, defaulted = resolve_service_duration(plan.stops[0], plan)
        self.assertEqual(duration, 900)
        self.assertTrue(defaulted)
        explicit, defaulted_explicit = resolve_service_duration(
            stop_after(45 * M, "s2", service_duration=120), plan
        )
        self.assertEqual(explicit, 120)
        self.assertFalse(defaulted_explicit)


class TimelineGuardTests(TimelineTestCase):
    def test_departure_location_is_not_a_service_stop(self) -> None:
        # I1: START never appears in a timeline; the first leg starts at the departure time.
        plan = build_plan(stop_after(45 * M, "a"), stop_after(90 * M, "b"))
        result = self.timeline_for(plan, "a", "b")
        self.assertEqual(len(result.timelines), 2)
        self.assertEqual(
            {timeline.stop_id for timeline in result.timelines}, {"a", "b"}
        )
        self.assertEqual(result.timelines[0].departure_from_previous, plan.departure_time)
        self.assertNotIn(plan.departure.label, {timeline.stop_id for timeline in result.timelines})

    def test_disabled_stop_cannot_be_routed(self) -> None:
        plan = build_plan(
            stop_after(45 * M, "a"),
            stop_after(90 * M, "b", enabled=False),
        )
        with self.assertRaises(InvalidOrderError):
            self.timeline_for(plan, "a", "b")

    def test_stop_without_coordinates_cannot_be_routed(self) -> None:
        plan = build_plan(stop("s1"))
        with self.assertRaises(StopNotGeocodedError):
            self.timeline_for(plan, "s1")

    def test_total_waiting_is_reported(self) -> None:
        plan = build_plan(
            stop_after(45 * M, "a", window=OPEN_WINDOW),
            stop_after(3 * T + 55 * M, "b", window=OPEN_WINDOW),
        )
        result = self.timeline_for(plan, "b", "a")
        self.assertGreater(result.total_waiting_sec, 0)
        self.assertEqual(
            result.total_waiting_sec, sum(t.waiting_time for t in result.timelines)
        )


if __name__ == "__main__":
    unittest.main()
