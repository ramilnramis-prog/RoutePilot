"""Generic user ordering constraints (decision D21, spec section 18).

The domain models ordering constraints generically so that future drag/reorder does not
require redesigning :class:`~core.model.route_plan.RoutePlan`:

* ``first_stop`` - implemented. The driver picked (or locked) the first service stop.
* ``position``   - reserved for future drag/reorder. The type can represent it; this version
  rejects it with :class:`~core.validation.errors.UnsupportedConstraintError` instead of
  pretending to honour it (D16).

No constraint solver is built here - constraint *representation* is not constraint
*resolution*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.model.ids import StopId
from core.validation.errors import (
    InvalidRoutePlanError,
    UnsupportedConstraintError,
)

__all__ = ["OrderConstraint", "OrderConstraintKind", "OrderOverrides"]


class OrderConstraintKind(str, Enum):
    """Kind of user ordering constraint."""

    FIRST_STOP = "first_stop"
    POSITION = "position"


@dataclass(frozen=True)
class OrderConstraint:
    """A single user ordering constraint."""

    kind: OrderConstraintKind
    stop_id: StopId
    position: int | None = None

    def __post_init__(self) -> None:
        kind = self.kind
        if not isinstance(kind, OrderConstraintKind):
            try:
                kind = OrderConstraintKind(kind)
            except ValueError:
                raise InvalidRoutePlanError(
                    f"unknown order constraint kind {self.kind!r}; expected one of "
                    f"{[k.value for k in OrderConstraintKind]}"
                ) from None
            object.__setattr__(self, "kind", kind)

        if not isinstance(self.stop_id, str) or not self.stop_id.strip():
            raise InvalidRoutePlanError("an order constraint needs a non-empty stop_id")

        if kind is OrderConstraintKind.FIRST_STOP:
            if self.position is not None:
                raise InvalidRoutePlanError(
                    "a first_stop constraint must not carry a position"
                )
        else:  # POSITION
            if self.position is None or self.position < 0:
                raise InvalidRoutePlanError(
                    "a position constraint requires position >= 0"
                )


@dataclass(frozen=True)
class OrderOverrides:
    """The user's ordering constraints.

    In this version only ``first_stop`` is implemented. ``position`` constraints are
    representable but rejected loudly by :meth:`validate_supported`.
    """

    constraints: tuple[OrderConstraint, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        constraints = tuple(self.constraints)
        object.__setattr__(self, "constraints", constraints)
        first_stop_constraints = [
            c for c in constraints if c.kind is OrderConstraintKind.FIRST_STOP
        ]
        if len(first_stop_constraints) > 1:
            raise InvalidRoutePlanError(
                "at most one first_stop constraint is allowed, got "
                f"{len(first_stop_constraints)}"
            )

    @classmethod
    def empty(cls) -> "OrderOverrides":
        return cls(())

    @classmethod
    def first_stop(cls, stop_id: StopId) -> "OrderOverrides":
        """Constraint form of a pinned first stop."""
        return cls((OrderConstraint(OrderConstraintKind.FIRST_STOP, stop_id),))

    def first_stop_id(self) -> StopId | None:
        """The constrained first stop, if any."""
        for constraint in self.constraints:
            if constraint.kind is OrderConstraintKind.FIRST_STOP:
                return constraint.stop_id
        return None

    def is_empty(self) -> bool:
        return not self.constraints

    def validate_supported(self) -> None:
        """Reject constraint kinds that are declared but not implemented (D16)."""
        for constraint in self.constraints:
            if constraint.kind is not OrderConstraintKind.FIRST_STOP:
                raise UnsupportedConstraintError(
                    f"order constraint kind {constraint.kind.value!r} "
                    "(drag/reorder position constraints)",
                    planned_stage="a later stage",
                )
