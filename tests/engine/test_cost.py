"""Cost scoring (D13, D31)."""

from __future__ import annotations

import unittest

from core.engine.cost import breakdown_as_tuple, score_breakdown, weighted_components
from core.model.cost_policy import (
    CostComponent,
    RouteCostPolicy,
    demo_provisional_policy,
)
from core.validation.errors import InvalidCostPolicyError


class ScoreBreakdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = demo_provisional_policy()  # travel_time=1, waiting_time=2

    def test_score_is_the_weighted_sum(self) -> None:
        score = score_breakdown(
            {
                CostComponent.TRAVEL_TIME: 14100.0,
                CostComponent.WAITING_TIME: 300.0,
            },
            self.policy,
        )
        self.assertEqual(score, 14100.0 + 2 * 300.0)

    def test_unweighted_components_contribute_nothing(self) -> None:
        with_distance = score_breakdown(
            {
                CostComponent.TRAVEL_TIME: 100.0,
                CostComponent.WAITING_TIME: 0.0,
                CostComponent.DISTANCE: 999_999.0,
            },
            self.policy,
        )
        self.assertEqual(with_distance, 100.0)

    def test_measurements_with_no_policy_weights_score_zero(self) -> None:
        policy = RouteCostPolicy(name="none")
        self.assertEqual(
            score_breakdown({CostComponent.TRAVEL_TIME: 5000.0}, policy), 0.0
        )

    def test_component_names_are_coerced_from_strings(self) -> None:
        score = score_breakdown({"travel_time": 10.0}, self.policy)
        self.assertEqual(score, 10.0)

    def test_unknown_component_is_rejected(self) -> None:
        with self.assertRaises(InvalidCostPolicyError):
            score_breakdown({"fuel_burn": 1.0}, self.policy)

    def test_invalid_values_are_rejected(self) -> None:
        for bad_value in (-1.0, float("inf"), float("nan"), "slow", True):
            with self.subTest(value=bad_value):
                with self.assertRaises(InvalidCostPolicyError):
                    score_breakdown({CostComponent.TRAVEL_TIME: bad_value}, self.policy)

    def test_breakdown_tuple_is_sorted_and_stable(self) -> None:
        first = breakdown_as_tuple(
            {CostComponent.WAITING_TIME: 2.0, CostComponent.TRAVEL_TIME: 1.0}
        )
        second = breakdown_as_tuple(
            {CostComponent.TRAVEL_TIME: 1.0, CostComponent.WAITING_TIME: 2.0}
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [component.value for component, _ in first], ["travel_time", "waiting_time"]
        )

    def test_weighted_components_lists_only_weighted_ones(self) -> None:
        self.assertEqual(
            [component.value for component in weighted_components(self.policy)],
            ["travel_time", "waiting_time"],
        )


if __name__ == "__main__":
    unittest.main()
