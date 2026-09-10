"""Cost policy capability rules (D13 amended, D16; spec sections 10 and 11)."""

from __future__ import annotations

import unittest

from core.model.cost_policy import (
    ComponentStatus,
    CostComponent,
    CostComponentDeclaration,
    RouteCostPolicy,
    empty_cost_policy,
)
from core.validation.errors import InvalidCostPolicyError, UnsupportedFeatureError


class CostPolicyTests(unittest.TestCase):
    def test_stage0_policy_declares_every_component_and_no_weights(self) -> None:
        policy = empty_cost_policy()
        self.assertFalse(policy.is_weighted())
        self.assertEqual(policy.weights, {})
        self.assertIn("no weights configured yet", policy.describe())
        for component in CostComponent:
            declaration = policy.declaration(component)
            self.assertIs(declaration.component, component)
            self.assertTrue(declaration.note, f"{component} needs a note explaining its status")

    def test_missing_declaration_is_rejected(self) -> None:
        with self.assertRaises(InvalidCostPolicyError):
            RouteCostPolicy(
                name="partial",
                declarations={
                    CostComponent.TRAVEL_TIME: CostComponentDeclaration(
                        CostComponent.TRAVEL_TIME, ComponentStatus.PLANNED
                    )
                },
            )

    def test_weight_requires_an_implemented_component(self) -> None:
        # Nothing is implemented in Stage 0, so a weight must be refused, not quietly accepted.
        with self.assertRaises(UnsupportedFeatureError):
            RouteCostPolicy(
                name="premature",
                weights={CostComponent.TRAVEL_TIME: 1.0},
            )

    def test_provider_dependent_components_cannot_be_scored(self) -> None:
        policy = empty_cost_policy()
        for component in (
            CostComponent.U_TURN_PENALTY,
            CostComponent.WRONG_SIDE_PENALTY,
            CostComponent.BACKTRACKING_PENALTY,
        ):
            declaration = policy.declaration(component)
            self.assertIs(declaration.status, ComponentStatus.REQUIRES_PROVIDER)
            self.assertTrue(declaration.requires)
            with self.assertRaises(UnsupportedFeatureError):
                RouteCostPolicy(name="fake", weights={component: 1.0})

    def test_side_of_road_is_not_inferred_from_coordinates(self) -> None:
        declaration = empty_cost_policy().declaration(CostComponent.WRONG_SIDE_PENALTY)
        self.assertIn("latitude/longitude", declaration.note)

    def test_requires_provider_declaration_must_name_its_requirement(self) -> None:
        with self.assertRaises(InvalidCostPolicyError):
            CostComponentDeclaration(CostComponent.WRONG_SIDE_PENALTY, ComponentStatus.REQUIRES_PROVIDER)

    def test_invalid_weights_are_rejected(self) -> None:
        for bad_weight in (-1.0, float("inf"), float("nan"), "heavy", True):
            with self.assertRaises(InvalidCostPolicyError):
                RouteCostPolicy(name="bad", weights={CostComponent.TRAVEL_TIME: bad_weight})

    def test_policy_needs_a_name(self) -> None:
        with self.assertRaises(InvalidCostPolicyError):
            RouteCostPolicy(name="   ")

    def test_time_window_violation_penalty_is_reserved_for_soft_windows(self) -> None:
        # D13 amendment: a hard window miss is a Violation, never a numeric penalty.
        note = empty_cost_policy().declaration(CostComponent.TIME_WINDOW_VIOLATION_PENALTY).note
        self.assertIn("soft", note)
        self.assertIn("Violation", note)


if __name__ == "__main__":
    unittest.main()
