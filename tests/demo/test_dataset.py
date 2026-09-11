"""Deterministic demo dataset (spec section 24, v2 section 33)."""

from __future__ import annotations

import unittest

from core.model.first_stop import FirstStopMode, FirstStopState
from core.model.route_stop import GeocodeStatus
from core.time import tzdata, tz
from demo.dataset import (
    DEMO_DEFAULT_SERVICE_DURATION,
    DEMO_DEPARTURE_TIME,
    DEMO_TIMEZONE,
    DEMO_WARNING,
    HEADLINE_STOP_IDS,
    build_demo_plan,
    demo_departure_time_at,
    demo_warning_text,
    intended_travel_minutes,
    work_list_order,
)


class DemoDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = build_demo_plan()

    def test_dataset_has_about_thirty_enabled_stops(self) -> None:
        self.assertEqual(len(self.plan.active_stops()), 31)
        self.assertGreaterEqual(len(self.plan.stops), 30)

    def test_exactly_one_stop_is_disabled_and_excluded(self) -> None:
        self.assertEqual(len(self.plan.disabled_stops()), 1)
        self.assertEqual(self.plan.disabled_stops()[0].id, HEADLINE_STOP_IDS["disabled"])
        self.assertNotIn(
            HEADLINE_STOP_IDS["disabled"],
            [stop.id for stop in self.plan.active_stops()],
        )

    def test_departure_is_four_in_the_morning_local_time(self) -> None:
        self.assertEqual(self.plan.departure_time, DEMO_DEPARTURE_TIME)
        zone = tzdata.load_timezone(DEMO_TIMEZONE)
        local = tz.to_local(self.plan.departure_time, zone)
        self.assertEqual((local.hour, local.minute), (4, 0))

    def test_local_departure_helper_matches_the_dataset(self) -> None:
        self.assertEqual(demo_departure_time_at(4), DEMO_DEPARTURE_TIME)
        self.assertNotEqual(demo_departure_time_at(7), DEMO_DEPARTURE_TIME)

    def test_every_enabled_stop_is_geocoded_and_located(self) -> None:
        for stop in self.plan.active_stops():
            self.assertIs(stop.geocode_status, GeocodeStatus.RESOLVED)
            self.assertIsNotNone(stop.location)

    def test_many_enabled_customers_open_at_eight(self) -> None:
        # The "many-stop 08:00 opening" story the demo scenario is built on, pinned exactly: the
        # report prints this count ("17 opening at 08:00", including the early-closing customer).
        opening_at_eight = [
            stop
            for stop in self.plan.active_stops()
            if stop.service_window.is_fixed
            and stop.service_window.describe().startswith("08:00-")
        ]
        self.assertEqual(len(opening_at_eight), 17)

    def test_the_bottleneck_customer_closes_early_and_the_rest_close_late(self) -> None:
        # The calibration that makes the demo demonstrate v2 section 14 at all: exactly one enabled
        # customer closes early enough to reject a first-stop choice, and every other fixed window
        # closes at 19:00 or 20:00 so that wasting the morning is merely worse, not infeasible.
        bottleneck = self.plan.stop_by_id(HEADLINE_STOP_IDS["early_close"])
        self.assertEqual(bottleneck.service_window.describe(), "08:00-10:00")
        closing_times = {
            stop.service_window.describe().split("-")[1]
            for stop in self.plan.active_stops()
            if stop.service_window.is_fixed
        }
        self.assertIn("10:00", closing_times)
        self.assertEqual(
            closing_times - {"10:00"},
            {"19:00", "20:00"},
        )

    def test_service_durations_are_short_and_one_stop_uses_the_default(self) -> None:
        durations = {
            stop.service_duration
            for stop in self.plan.active_stops()
            if stop.service_duration is not None
        }
        self.assertEqual(durations, {4 * 60, 5 * 60})
        defaulted = [
            stop for stop in self.plan.active_stops() if stop.service_duration is None
        ]
        self.assertEqual(len(defaulted), 1)
        self.assertEqual(HEADLINE_STOP_IDS["unknown_hours"], "S08-UNKNOWN-HOURS")
        self.assertEqual(defaulted[0].id, "S18-NO-DURATION")
        self.assertEqual(self.plan.default_service_duration, 10 * 60)

    def test_dataset_covers_every_window_kind(self) -> None:
        kinds = {stop.service_window.window_kind.value for stop in self.plan.stops}
        self.assertEqual(kinds, {"fixed", "unrestricted", "unknown"})
        # ... and the enabled set exercises all three too, so the candidates do.
        active_kinds = {stop.service_window.window_kind.value for stop in self.plan.active_stops()}
        self.assertEqual(active_kinds, {"fixed", "unrestricted", "unknown"})

    def test_dataset_contains_priorities_and_an_unknown_duration(self) -> None:
        self.assertTrue(any(stop.priority is not None for stop in self.plan.stops))
        self.assertTrue(any(stop.service_duration is None for stop in self.plan.stops))
        self.assertEqual(self.plan.default_service_duration, DEMO_DEFAULT_SERVICE_DURATION)

    def test_headline_stops_exist(self) -> None:
        ids = {stop.id for stop in self.plan.stops}
        for role, stop_id in HEADLINE_STOP_IDS.items():
            with self.subTest(role=role):
                self.assertIn(stop_id, ids)

    def test_build_is_deterministic(self) -> None:
        first = build_demo_plan()
        second = build_demo_plan()
        self.assertEqual(first, second)
        self.assertEqual(first.inputs_fingerprint(), second.inputs_fingerprint())

    def test_departure_time_change_produces_a_different_fingerprint(self) -> None:
        shifted = build_demo_plan(departure_time=demo_departure_time_at(7))
        self.assertNotEqual(
            self.plan.inputs_fingerprint(), shifted.inputs_fingerprint()
        )

    def test_data_is_labelled_as_synthetic(self) -> None:
        self.assertIn("DEMO", DEMO_WARNING)
        self.assertIn("SYNTHETIC", DEMO_WARNING)
        self.assertIn("not real routing", DEMO_WARNING)
        self.assertEqual(demo_warning_text(), DEMO_WARNING)

    def test_start_and_finish_are_not_service_stops(self) -> None:
        stop_ids = {stop.id for stop in self.plan.stops}
        self.assertNotIn(self.plan.departure.label, stop_ids)
        self.assertNotIn(self.plan.finish.label, stop_ids)

    def test_the_demo_plan_waits_for_the_driver_choice(self) -> None:
        # D4/D32: the demo starts in RECOMMEND mode with nothing selected.
        self.assertIs(self.plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertIs(self.plan.first_service_stop.mode, FirstStopMode.RECOMMEND)
        self.assertIsNone(self.plan.first_service_stop.selected_stop_id)

    def test_input_positions_are_unique_and_contiguous(self) -> None:
        positions = [stop.input_position for stop in self.plan.stops]
        self.assertEqual(sorted(positions), list(range(len(self.plan.stops))))
        self.assertEqual(len(set(positions)), len(positions))

    def test_the_work_list_is_a_plausible_nearest_first_list(self) -> None:
        # The plan's input order (the user's work list, v2 section 30) is deterministic and
        # nearest-first, so the USER baseline is a route a driver could really have entered.
        specs = work_list_order()
        travels = [spec.travel_minutes for spec in specs]
        self.assertEqual(travels, sorted(travels))
        self.assertEqual(
            [stop.id for stop in self.plan.stops],
            [spec.stop_id for spec in specs],
        )
        self.assertEqual(intended_travel_minutes(12, -30), 30)

    def test_the_disabled_stop_keeps_its_position_and_is_omitted_from_the_baseline(self) -> None:
        disabled = self.plan.disabled_stops()[0]
        work_list = [spec.stop_id for spec in work_list_order()]
        self.assertEqual(disabled.input_position, work_list.index(disabled.id))
        self.assertNotIn(disabled.id, self.plan.user_baseline_order())
        # Disabling must not renumber the rest: every other position is untouched.
        self.assertEqual(
            [stop.input_position for stop in self.plan.active_stops()],
            [
                position
                for position in range(len(self.plan.stops))
                if position != disabled.input_position
            ],
        )

    def test_user_baseline_is_the_input_order(self) -> None:
        self.assertEqual(
            self.plan.user_baseline_order(),
            tuple(stop.id for stop in self.plan.active_stops()),
        )
        self.assertEqual(
            self.plan.next_input_position(), max(s.input_position for s in self.plan.stops) + 1
        )


if __name__ == "__main__":
    unittest.main()
