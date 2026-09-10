"""Cost policy capability rules (D13 amended, D16; spec sections 10 and 11)."""

from __future__ import annotations

import unittest

from core.model.cost_policy import (
    DEMO_PROVISIONAL_POLICY_NAME,
    ComponentStatus,
    CostComponent,
    CostComponentDeclaration,
    RouteCostPolicy,
    demo_provisional_policy,
    empty_cost_policy,
)
from core.validation.errors import InvalidCostPolicyError, UnsupportedFeatureError


class CostPolicyTests(unittest.TestCase):
    def test_stage0_policy_declares_every_component_and_no_weights(self) -> None:
        policy = empty_cost_policy()
        self.assertFalse(policy.is_weighted())
        self.assertFalse(policy.provisional)
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
        # Priority weighting is only meaningful once a product decision defines its value, and the
        # component is declared 'planned', so a weight must be refused rather than quietly accepted.
        with self.assertRaises(UnsupportedFeatureError):
            RouteCostPolicy(
                name="premature",
                weights={CostComponent.PRIORITY_PENALTY: 1.0},
            )

    def test_implemented_components_can_be_weighted(self) -> None:
        policy = RouteCostPolicy(
            name="stage1",
            weights={CostComponent.TRAVEL_TIME: 1.0, CostComponent.WAITING_TIME: 2.0},
        )
        self.assertTrue(policy.is_weighted())
        self.assertEqual(policy.weight(CostComponent.TRAVEL_TIME), 1.0)
        self.assertEqual(policy.weight(CostComponent.WAITING_TIME), 2.0)
        self.assertEqual(policy.weight(CostComponent.DISTANCE), 0.0)
        self.assertIn("travel_time=1", policy.describe())

    def test_demo_policy_is_marked_provisional(self) -> None:
        policy = demo_provisional_policy()
        self.assertTrue(policy.provisional)
        self.assertEqual(policy.name, DEMO_PROVISIONAL_POLICY_NAME)
        self.assertTrue(policy.notes)
        self.assertIn("PROVISIONAL", policy.describe())

    def test_remaining_route_weight_is_not_part_of_the_demo_policy(self) -> None:
        # Spec section 8 is not implemented yet, so the policy must not pretend to score it.
        policy = demo_provisional_policy()
        self.assertEqual(
            policy.weight(CostComponent.FIRST_STOP_REMAINING_ROUTE_WEIGHT), 0.0
        )
        self.assertIs(
            policy.declaration(CostComponent.FIRST_STOP_REMAINING_ROUTE_WEIGHT).status,
            ComponentStatus.PLANNED,
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
