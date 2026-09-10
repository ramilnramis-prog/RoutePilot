"""``input_position``: immutable input-order provenance (v2 sections 25 and 30).

``input_position`` records where a stop stood in the list the user supplied or imported. It is not
the current route order: optimization, route reordering and drag/reorder must never change it, and
the user-facing BEFORE baseline is built from it.
"""

from __future__ import annotations

import dataclasses
import unittest

from core.model.first_stop import FirstStopIntent
from core.model.ids import StopId
from core.model.order_override import OrderOverrides
from core.validation.errors import InvalidRoutePlanError, InvalidRouteStopError
from tests.support import build_plan, stop


def stops_abc():
    return (
        stop("a", 55.80, 37.70),
        stop("b", 55.82, 37.72),
        stop("c", 55.84, 37.74),
    )


class InputPositionValidationTests(unittest.TestCase):
    def test_negative_input_position_is_rejected(self) -> None:
        with self.assertRaises(InvalidRouteStopError) as context:
            stop("s1", 55.75, 37.62, input_position=-1)
        self.assertIn("input_position must be >= 0", str(context.exception))

    def test_non_integer_input_position_is_rejected(self) -> None:
        for bad_value in (1.5, "0", True, None):
            with self.subTest(value=bad_value):
                with self.assertRaises(InvalidRouteStopError):
                    stop("s1", 55.75, 37.62, input_position=bad_value)

    def test_duplicate_input_position_is_rejected(self) -> None:
        first, second, _ = stops_abc()
        with self.assertRaises(InvalidRoutePlanError) as context:
            build_plan(first, second, input_positions=[3, 3])
        self.assertIn("duplicate input_position", str(context.exception))

    def test_gaps_are_valid_and_are_not_renumbered(self) -> None:
        first, second, third = stops_abc()
        plan = build_plan(first, second, third, input_positions=[0, 1, 4])
        self.assertEqual([s.input_position for s in plan.stops], [0, 1, 4])

        sparse = build_plan(first, second, third, input_positions=[7, 21, 90])
        self.assertEqual([s.input_position for s in sparse.stops], [7, 21, 90])

    def test_a_plan_keeps_its_stops_in_input_order(self) -> None:
        first, second, third = stops_abc()
        # Passed in one order, positioned in another: the plan must not trust the caller's order.
        plan = build_plan(first, second, third, input_positions=[9, 4, 0])
        self.assertEqual([s.id for s in plan.stops], ["c", "b", "a"])
        self.assertEqual([s.input_position for s in plan.stops], [0, 4, 9])


class UserBaselineTests(unittest.TestCase):
    """v2 section 30: START -> enabled stops sorted by input_position -> FINISH."""

    def test_user_baseline_follows_input_position_not_storage_order(self) -> None:
        first, second, third = stops_abc()
        plan = build_plan(first, second, third, input_positions=[7, 0, 4])
        self.assertEqual(plan.user_baseline_order(), (StopId("b"), StopId("c"), StopId("a")))
        self.assertEqual(
            plan.user_baseline_order(),
            tuple(StopId(sid) for sid in ("b", "c", "a")),
        )

    def test_disabled_stops_are_omitted_without_renumbering_the_rest(self) -> None:
        first, second, third = stops_abc()
        disabled_second = dataclasses.replace(second, enabled=False)
        plan = build_plan(first, disabled_second, third, input_positions=[0, 5, 9])

        self.assertEqual(plan.user_baseline_order(), (StopId("a"), StopId("c")))
        self.assertEqual([s.input_position for s in plan.active_stops()], [0, 9])
        self.assertEqual([s.input_position for s in plan.disabled_stops()], [5])
        # The remaining positions were not rewritten into a contiguous range.
        self.assertEqual([s.input_position for s in plan.stops], [0, 5, 9])

    def test_a_new_stop_appends_a_free_position(self) -> None:
        first, second, _ = stops_abc()
        plan = build_plan(first, second, input_positions=[0, 7])
        self.assertEqual(plan.next_input_position(), 8)

        appended = dataclasses.replace(
            plan,
            stops=plan.stops
            + (
                dataclasses.replace(
                    second, id=StopId("new"), input_position=plan.next_input_position()
                ),
            ),
        )
        self.assertEqual([s.input_position for s in appended.stops], [0, 7, 8])
        # Historical positions are untouched by an append.
        self.assertEqual(appended.stop_by_id(StopId("a")).input_position, 0)
        self.assertEqual(appended.stop_by_id(StopId("b")).input_position, 7)


class ImmutabilityTests(unittest.TestCase):
    def test_input_position_cannot_be_mutated_in_place(self) -> None:
        plan = build_plan(*stops_abc())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.stops[0].input_position = 99  # type: ignore[misc]

    def test_reordering_a_plan_never_changes_input_position(self) -> None:
        # A route reorder (what optimization produces) changes the order of the stops tuple,
        # not the historical input positions.
        plan = build_plan(*stops_abc(), input_positions=[2, 4, 9])
        before = {s.id: s.input_position for s in plan.stops}

        reordered = dataclasses.replace(plan, stops=tuple(reversed(plan.stops)))

        self.assertEqual({s.id: s.input_position for s in reordered.stops}, before)
        self.assertEqual(reordered.user_baseline_order(), plan.user_baseline_order())

    def test_order_overrides_and_driver_selection_never_change_input_position(self) -> None:
        plan = build_plan(*stops_abc(), input_positions=[2, 4, 9])
        before = {s.id: s.input_position for s in plan.stops}

        constrained = dataclasses.replace(
            plan,
            first_service_stop=FirstStopIntent.manual_choice(StopId("c")),
            order_overrides=OrderOverrides.first_stop(StopId("c")),
        )

        self.assertEqual({s.id: s.input_position for s in constrained.stops}, before)
        self.assertEqual(constrained.user_baseline_order(), plan.user_baseline_order())

    def test_input_order_does_not_invalidate_the_recommendation_fingerprint(self) -> None:
        # The recommendation does not depend on the order the stops arrived in (v2 section 7),
        # so swapping input positions must not look like a changed recommendation.
        first, second, _ = stops_abc()
        left = build_plan(first, second, input_positions=[0, 1])
        right = build_plan(first, second, input_positions=[1, 0])

        self.assertEqual([s.id for s in left.stops], ["a", "b"])
        self.assertEqual([s.id for s in right.stops], ["b", "a"])
        self.assertEqual(left.inputs_fingerprint(), right.inputs_fingerprint())


if __name__ == "__main__":
    unittest.main()
