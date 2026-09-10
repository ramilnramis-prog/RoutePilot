"""Cost scoring (decision D13, D31).

One job: turn a measured cost breakdown into a score using a policy's weights. Components the
policy does not weight contribute zero, which keeps the breakdown complete and auditable while
the objective stays explicitly configured.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from core.model.cost_policy import CostComponent, RouteCostPolicy
from core.validation.errors import InvalidCostPolicyError

__all__ = ["breakdown_as_tuple", "score_breakdown", "weighted_components"]


def score_breakdown(
    breakdown: Mapping[CostComponent, float],
    policy: RouteCostPolicy,
) -> float:
    """Weighted sum of a cost breakdown.

    Raises:
        InvalidCostPolicyError: a component or a value is not a finite, non-negative number.
    """
    total = 0.0
    for component, value in breakdown.items():
        if not isinstance(component, CostComponent):
            try:
                component = CostComponent(component)
            except ValueError:
                raise InvalidCostPolicyError(f"unknown cost component {component!r}") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidCostPolicyError(
                f"cost value for {component.value!r} must be a number, got "
                f"{type(value).__name__}"
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise InvalidCostPolicyError(
                f"cost value for {component.value!r} must be finite, got {value!r}"
            )
        if numeric < 0:
            raise InvalidCostPolicyError(
                f"cost value for {component.value!r} must be >= 0, got {value!r}"
            )
        total += policy.weight(component) * numeric
    return total


def breakdown_as_tuple(
    breakdown: Mapping[CostComponent, float],
) -> tuple[tuple[CostComponent, float], ...]:
    """Deterministic (sorted) representation of a breakdown, for reports and tests."""
    return tuple(
        sorted(
            ((component, float(value)) for component, value in breakdown.items()),
            key=lambda item: item[0].value,
        )
    )


def weighted_components(policy: RouteCostPolicy) -> tuple[CostComponent, ...]:
    """Components the policy actually scores, sorted for stable output."""
    return tuple(sorted(policy.weights, key=lambda component: component.value))
