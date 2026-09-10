"""RoutePlan structure, order validation and the AUTO recomputation fingerprint.

Covers spec section 27 items 1, 2 (START/FINISH are not service stops, FINISH stays fixed) and
items 3, 4, 17 (every enabled stop exactly once, disabled excluded, no duplication or loss).
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, time

from core.model.first_stop import FirstStopIntent
from core.model.ids import PlanId, StopId
from core.model.order_override import (
    OrderConstraint,
    OrderConstraintKind,
    OrderOverrides,
)
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.service_window import ServiceWindow
from core.model.value_objects import GeoPoint, PlaceRef
from core.time import tzdata
from core.validation.errors import (
    InvalidOrderError,
    InvalidRoutePlanError,
    InvalidTimezoneNameError,
    UnknownTimezoneError,
    UnsupportedConstraintError,
    ValidationError,
)
from tests.support import build_plan, place, stop, utc

ENABLED = "s1"
ENABLED2 = "s2"
DISABLED = "s3"


def sample_plan() -> RoutePlan:
    return build_plan(
        stop(ENABLED, 55.80, 37.70, priority=1),
        stop(ENABLED2, 55.90, 37.80),
        stop(DISABLED, 55.85, 37.75, enabled=False),
    )


class PlanStructureTests(unittest.TestCase):
    def test_start_and_finish_are_place_references_not_stops(self) -> None:
        # I1/I2 are enforced by the type system: a PlaceRef can never appear in an order.
        plan = sample_plan()
        self.assertIsInstance(plan.departure, PlaceRef)
        self.assertIsInstance(plan.finish, PlaceRef)
        self.assertNotIsInstance(plan.departure, RouteStop)
        self.assertNotIsInstance(plan.finish, RouteStop)
        self.assertNotIn(plan.departure.label, [stop.id for stop in plan.stops])

    def test_plan_rejects_a_route_stop_as_departure(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            RoutePlan(
                id=PlanId("p"),
                timezone="Europe/Moscow",
                departure=stop("warehouse", 55.75, 37.62),  # type: ignore[arg-type]
                departure_time=utc(2026, 9, 11, 1, 0),
                finish=place("Depot", 55.70, 37.55),
            )

    def test_duplicate_stop_ids_are_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            build_plan(stop("dup", 55.80, 37.70), stop("dup", 55.90, 37.80))

    def test_naive_departure_time_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RoutePlan(
                id=PlanId("p"),
                timezone="Europe/Moscow",
                departure=place("Warehouse", 55.75, 37.62),
                departure_time=datetime(2026, 9, 11, 4, 0),  # naive
                finish=place("Depot", 55.70, 37.55),
            )

    def test_invalid_timezone_name_is_rejected(self) -> None:
        with self.assertRaises(InvalidTimezoneNameError):
            build_plan(stop(ENABLED, 55.80, 37.70), timezone_name="Not A Zone")

    def test_unknown_timezone_is_rejected_when_a_database_is_available(self) -> None:
        if not tzdata.probe_tzdata().is_available:
            self.skipTest("no IANA database available to check existence against")
        with self.assertRaises(UnknownTimezoneError):
            build_plan(stop(ENABLED, 55.80, 37.70), timezone_name="Mars/Olympus")

    def test_default_service_duration_must_be_positive(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            build_plan(stop(ENABLED, 55.80, 37.70), default_service_duration=0)

    def test_active_and_disabled_stops_are_separated(self) -> None:
        plan = sample_plan()
        self.assertEqual([stop.id for stop in plan.active_stops()], [ENABLED, ENABLED2])
        self.assertEqual([stop.id for stop in plan.disabled_stops()], [DISABLED])
        self.assertTrue(plan.has_active_stops)


class OrderValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = sample_plan()

    def test_valid_order_is_accepted(self) -> None:
        self.plan.validate_order([StopId(ENABLED2), StopId(ENABLED)])  # no exception

    def test_duplicate_stop_is_rejected(self) -> None:
        with self.assertRaises(InvalidOrderError) as context:
            self.plan.validate_order([StopId(ENABLED), StopId(ENABLED)])
        self.assertIn("duplicate", str(context.exception))

    def test_missing_enabled_stop_is_rejected(self) -> None:
        with self.assertRaises(InvalidOrderError) as context:
            self.plan.validate_order([StopId(ENABLED)])
        self.assertIn(ENABLED2, str(context.exception))

    def test_disabled_stop_is_rejected(self) -> None:
        with self.assertRaises(InvalidOrderError) as context:
            self.plan.validate_order([StopId(ENABLED), StopId(ENABLED2), StopId(DISABLED)])
        self.assertIn("disabled", str(context.exception))

    def test_unknown_stop_is_rejected(self) -> None:
        with self.assertRaises(InvalidOrderError):
            self.plan.validate_order([StopId(ENABLED), StopId(ENABLED2), StopId("ghost")])

    def test_start_location_can_never_appear_in_an_order(self) -> None:
        # I1: the departure location is not a StopId at all, so it cannot be served.
        with self.assertRaises(InvalidOrderError):
            self.plan.validate_order([StopId("Warehouse")])

    def test_empty_plan_accepts_an_empty_order(self) -> None:
        empty = build_plan()
        empty.validate_order([])
        self.assertFalse(empty.has_active_stops)

    def test_order_does_not_duplicate_or_lose_stops(self) -> None:
        self.plan.validate_order([StopId(ENABLED), StopId(ENABLED2)])
        with self.assertRaises(InvalidOrderError):
            self.plan.validate_order([StopId(ENABLED), StopId(ENABLED), StopId(ENABLED2)])


class FirstStopConsistencyTests(unittest.TestCase):
    def test_a_pinned_intent_must_match_an_order_override(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            build_plan(
                stop(ENABLED, 55.80, 37.70),
                stop(ENABLED2, 55.90, 37.80),
                first_service_stop=FirstStopIntent.auto_locked(StopId(ENABLED)),
                order_overrides=OrderOverrides.first_stop(StopId(ENABLED2)),
            )

    def test_matching_pin_is_accepted(self) -> None:
        plan = build_plan(
            stop(ENABLED, 55.80, 37.70),
            stop(ENABLED2, 55.90, 37.80),
            first_service_stop=FirstStopIntent.auto_locked(StopId(ENABLED)),
            order_overrides=OrderOverrides.first_stop(StopId(ENABLED)),
        )
        self.assertEqual(plan.order_overrides.first_stop_id(), ENABLED)

    def test_dynamic_auto_intent_without_overrides_is_valid(self) -> None:
        plan = build_plan(stop(ENABLED, 55.80, 37.70))
        self.assertTrue(plan.first_service_stop.is_dynamic)
        self.assertTrue(plan.order_overrides.is_empty())

    def test_unimplemented_position_constraint_is_rejected_by_the_plan(self) -> None:
        with self.assertRaises(UnsupportedConstraintError):
            build_plan(
                stop(ENABLED, 55.80, 37.70),
                order_overrides=OrderOverrides(
                    (OrderConstraint(OrderConstraintKind.POSITION, StopId(ENABLED), position=0),)
                ),
            )


class FingerprintTests(unittest.TestCase):
    """AUTO must be able to notice that its recommendation went stale (D4, spec section 5)."""

    def fingerprint(self, plan: RoutePlan, **kwargs) -> str:
        return plan.inputs_fingerprint(**kwargs)

    def test_fingerprint_is_deterministic(self) -> None:
        plan = sample_plan()
        self.assertEqual(self.fingerprint(plan), self.fingerprint(plan))
        self.assertEqual(len(self.fingerprint(plan)), 64)

    def test_departure_time_changes_fingerprint(self) -> None:
        base = sample_plan()
        shifted = build_plan(
            stop(ENABLED, 55.80, 37.70, priority=1),
            stop(ENABLED2, 55.90, 37.80),
            stop(DISABLED, 55.85, 37.75, enabled=False),
            departure_time=utc(2026, 9, 11, 4, 0),
        )
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(shifted))

    def test_departure_location_changes_fingerprint(self) -> None:
        base = sample_plan()
        moved = build_plan(
            stop(ENABLED, 55.80, 37.70, priority=1),
            stop(ENABLED2, 55.90, 37.80),
            stop(DISABLED, 55.85, 37.75, enabled=False),
            departure=place("Other warehouse", 55.60, 37.40),
        )
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(moved))

    def test_finish_location_changes_fingerprint(self) -> None:
        base = sample_plan()
        other_finish = build_plan(
            stop(ENABLED, 55.80, 37.70, priority=1),
            stop(ENABLED2, 55.90, 37.80),
            stop(DISABLED, 55.85, 37.75, enabled=False),
            finish=place("Another depot", 55.10, 37.10),
        )
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(other_finish))

    def test_service_window_changes_fingerprint(self) -> None:
        base = sample_plan()
        with_window = build_plan(
            stop(
                ENABLED,
                55.80,
                37.70,
                priority=1,
                window=ServiceWindow.fixed(time(8, 0), time(18, 0)),
            ),
            stop(ENABLED2, 55.90, 37.80),
            stop(DISABLED, 55.85, 37.75, enabled=False),
        )
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(with_window))

    def test_priority_and_enabled_set_change_fingerprint(self) -> None:
        base = sample_plan()
        stops = [stop(ENABLED, 55.80, 37.70, priority=1), stop(ENABLED2, 55.90, 37.80)]
        reprioritised = build_plan(
            dataclasses.replace(stops[0], priority=5), stops[1]
        )
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(reprioritised))

        disabled_change = build_plan(
            stops[0], dataclasses.replace(stops[1], enabled=False)
        )
        self.assertNotEqual(self.fingerprint(base), self.fingerprint(disabled_change))

    def test_notes_do_not_change_fingerprint(self) -> None:
        base = sample_plan()
        annotated = dataclasses.replace(base.stops[0], notes="call the back door")
        changed = dataclasses.replace(base, stops=(annotated,) + base.stops[1:])
        self.assertEqual(self.fingerprint(base), self.fingerprint(changed))

    def test_matrix_fingerprint_participates(self) -> None:
        plan = sample_plan()
        self.assertNotEqual(
            self.fingerprint(plan),
            self.fingerprint(plan, matrix_fingerprint="traffic-v2"),
        )


if __name__ == "__main__":
    unittest.main()
