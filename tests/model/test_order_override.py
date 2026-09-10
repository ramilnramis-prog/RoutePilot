"""Generic order override constraints (D21, spec section 18)."""

from __future__ import annotations

import unittest

from core.model.ids import StopId
from core.model.order_override import (
    OrderConstraint,
    OrderConstraintKind,
    OrderOverrides,
)
from core.validation.errors import (
    InvalidRoutePlanError,
    UnsupportedConstraintError,
)


class OrderOverrideTests(unittest.TestCase):
    def test_first_stop_constraint_is_supported(self) -> None:
        overrides = OrderOverrides.first_stop(StopId("s1"))
        overrides.validate_supported()  # must not raise
        self.assertEqual(overrides.first_stop_id(), "s1")
        self.assertFalse(overrides.is_empty())

    def test_empty_overrides_have_no_first_stop(self) -> None:
        overrides = OrderOverrides.empty()
        self.assertTrue(overrides.is_empty())
        self.assertIsNone(overrides.first_stop_id())

    def test_only_one_first_stop_constraint_is_allowed(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            OrderOverrides(
                (
                    OrderConstraint(OrderConstraintKind.FIRST_STOP, StopId("s1")),
                    OrderConstraint(OrderConstraintKind.FIRST_STOP, StopId("s2")),
                )
            )

    def test_position_constraint_is_representable_but_rejected(self) -> None:
        # D16: declared in the domain, not implemented - it must not be silently ignored.
        overrides = OrderOverrides(
            (OrderConstraint(OrderConstraintKind.POSITION, StopId("s2"), position=3),)
        )
        self.assertEqual(len(overrides.constraints), 1)
        with self.assertRaises(UnsupportedConstraintError):
            overrides.validate_supported()

    def test_first_stop_constraint_must_not_carry_a_position(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            OrderConstraint(OrderConstraintKind.FIRST_STOP, StopId("s1"), position=0)

    def test_position_constraint_requires_a_position(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            OrderConstraint(OrderConstraintKind.POSITION, StopId("s1"))

    def test_constraint_needs_a_stop_id(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            OrderConstraint(OrderConstraintKind.FIRST_STOP, StopId("  "))


if __name__ == "__main__":
    unittest.main()
