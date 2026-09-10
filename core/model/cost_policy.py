"""Route cost policy (decision D13, amended).

The policy names every component the product will eventually score, and records its
**implementation status**. Two rules make the policy honest rather than aspirational:

1. no arbitrary weights are hardcoded at early stages - a policy with no weights is valid and
   is the Stage 0 default;
2. a weight may only be assigned to a component whose status is ``implemented``. Claiming to
   score side-of-road or U-turns without road geometry is therefore impossible by construction
   (D16, spec section 11).

The D13 amendment is encoded in the declarations: a **hard** service-window miss is an explicit
``Violation``, never a penalty. ``time_window_violation_penalty`` is declared as applying only to
explicitly *soft/preferred* windows, which do not exist yet.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from core.validation.errors import InvalidCostPolicyError, UnsupportedFeatureError

__all__ = [
    "ComponentStatus",
    "CostComponent",
    "CostComponentDeclaration",
    "RouteCostPolicy",
    "STAGE0_COMPONENT_DECLARATIONS",
    "default_component_declarations",
    "empty_cost_policy",
]


class CostComponent(str, Enum):
    """Scoring components named by spec section 10."""

    TRAVEL_TIME = "travel_time"
    DISTANCE = "distance"
    WAITING_TIME = "waiting_time"
    EARLY_ARRIVAL_PENALTY = "early_arrival_penalty"
    LATE_ARRIVAL_PENALTY = "late_arrival_penalty"
    TIME_WINDOW_VIOLATION_PENALTY = "time_window_violation_penalty"
    U_TURN_PENALTY = "u_turn_penalty"
    WRONG_SIDE_PENALTY = "wrong_side_penalty"
    BACKTRACKING_PENALTY = "backtracking_penalty"
    PRIORITY_PENALTY = "priority_penalty"
    FINISH_DIRECTION_PENALTY = "finish_direction_penalty"
    FIRST_STOP_REMAINING_ROUTE_WEIGHT = "first_stop_remaining_route_weight"


class ComponentStatus(str, Enum):
    """Whether a component can actually be computed today (D16)."""

    #: Computed by implemented engine code; safe to weight.
    IMPLEMENTED = "implemented"
    #: Named by the specification, not implemented yet.
    PLANNED = "planned"
    #: Impossible without data the domain does not have (road geometry, direction, traffic).
    REQUIRES_PROVIDER = "requires_provider"


@dataclass(frozen=True)
class CostComponentDeclaration:
    """Implementation status of one cost component."""

    component: CostComponent
    status: ComponentStatus
    requires: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.status is ComponentStatus.REQUIRES_PROVIDER and not self.requires:
            raise InvalidCostPolicyError(
                f"component {self.component.value!r} is marked requires_provider but does not "
                "name the provider capability it needs"
            )


def _declaration(
    component: CostComponent,
    status: ComponentStatus,
    note: str,
    *,
    requires: str | None = None,
) -> CostComponentDeclaration:
    return CostComponentDeclaration(component, status, requires, note)


#: Stage 0 capability table. Nothing is implemented yet, so nothing may be weighted.
STAGE0_COMPONENT_DECLARATIONS: tuple[CostComponentDeclaration, ...] = (
    _declaration(
        CostComponent.TRAVEL_TIME,
        ComponentStatus.PLANNED,
        "travel time is computed per leg in the timeline; using it as an objective term is "
        "part of the optimizer stage",
    ),
    _declaration(
        CostComponent.DISTANCE,
        ComponentStatus.PLANNED,
        "distance comes from the travel matrix; using it as an objective term is part of the "
        "optimizer stage",
    ),
    _declaration(
        CostComponent.WAITING_TIME,
        ComponentStatus.PLANNED,
        "waiting time is computed per stop in the timeline; scoring it is an optimizer concern",
    ),
    _declaration(
        CostComponent.EARLY_ARRIVAL_PENALTY,
        ComponentStatus.PLANNED,
        "early arrival is allowed and normally shows up as waiting_time; a separate penalty "
        "needs an explicit product decision",
    ),
    _declaration(
        CostComponent.LATE_ARRIVAL_PENALTY,
        ComponentStatus.PLANNED,
        "applies only to explicitly soft/preferred windows, which do not exist yet "
        "(D13 amendment); a hard window miss is a Violation, not a penalty",
    ),
    _declaration(
        CostComponent.TIME_WINDOW_VIOLATION_PENALTY,
        ComponentStatus.PLANNED,
        "reserved for soft/preferred windows only; hard infeasibility is represented as an "
        "explicit Violation and must never be hidden in a numeric penalty (D13 amendment)",
    ),
    _declaration(
        CostComponent.U_TURN_PENALTY,
        ComponentStatus.REQUIRES_PROVIDER,
        "a U-turn is a property of road geometry and direction, not of coordinates",
        requires="RoutingProvider (road geometry, permitted manoeuvres)",
    ),
    _declaration(
        CostComponent.WRONG_SIDE_PENALTY,
        ComponentStatus.REQUIRES_PROVIDER,
        "the true side of the road cannot be inferred from latitude/longitude (spec section 11); "
        "RoutePilot does not fake this with geometry",
        requires="RoutingProvider (road side / approach direction)",
    ),
    _declaration(
        CostComponent.BACKTRACKING_PENALTY,
        ComponentStatus.REQUIRES_PROVIDER,
        "detecting real backtracking needs road paths rather than straight-line coordinates",
        requires="RoutingProvider (road path per leg)",
    ),
    _declaration(
        CostComponent.PRIORITY_PENALTY,
        ComponentStatus.PLANNED,
        "priority is stored on the stop; turning it into an objective term is an optimizer "
        "concern with configurable weights",
    ),
    _declaration(
        CostComponent.FINISH_DIRECTION_PENALTY,
        ComponentStatus.PLANNED,
        "direction relative to FINISH is computable from the matrix once the optimizer exists",
    ),
    _declaration(
        CostComponent.FIRST_STOP_REMAINING_ROUTE_WEIGHT,
        ComponentStatus.PLANNED,
        "the quality of a first-stop candidate must include the route after it (spec section 8); "
        "implemented with the first-stop selector",
    ),
)


def default_component_declarations() -> dict[CostComponent, CostComponentDeclaration]:
    """Stage 0 capability table as a fresh mapping."""
    return {declaration.component: declaration for declaration in STAGE0_COMPONENT_DECLARATIONS}


@dataclass(frozen=True)
class RouteCostPolicy:
    """Configurable scoring policy.

    Stage 0 ships **no weights**: an empty policy is honest, whereas invented numbers would
    silently become product truth (spec section 10).
    """

    name: str
    weights: Mapping[CostComponent, float] = field(default_factory=dict)
    declarations: Mapping[CostComponent, CostComponentDeclaration] = field(
        default_factory=default_component_declarations
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InvalidCostPolicyError("a cost policy needs a non-empty name")

        declarations = {
            (component if isinstance(component, CostComponent) else CostComponent(component)): declaration
            for component, declaration in self.declarations.items()
        }
        object.__setattr__(self, "declarations", declarations)

        missing = [component for component in CostComponent if component not in declarations]
        if missing:
            raise InvalidCostPolicyError(
                "every cost component needs a declaration; missing: "
                f"{[component.value for component in missing]}"
            )

        weights: dict[CostComponent, float] = {}
        for component, weight in self.weights.items():
            if not isinstance(component, CostComponent):
                try:
                    component = CostComponent(component)
                except ValueError:
                    raise InvalidCostPolicyError(
                        f"unknown cost component {component!r}"
                    ) from None
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise InvalidCostPolicyError(
                    f"weight for {component.value!r} must be a number, got "
                    f"{type(weight).__name__}"
                )
            if not math.isfinite(float(weight)):
                raise InvalidCostPolicyError(
                    f"weight for {component.value!r} must be finite, got {weight!r}"
                )
            if float(weight) < 0:
                raise InvalidCostPolicyError(
                    f"weight for {component.value!r} must be >= 0, got {weight!r}"
                )
            declaration = declarations[component]
            if declaration.status is not ComponentStatus.IMPLEMENTED:
                raise UnsupportedFeatureError(
                    f"cost component {component.value!r} "
                    f"(status={declaration.status.value}"
                    + (f", requires {declaration.requires}" if declaration.requires else "")
                    + ")",
                    planned_stage="the stage that implements it",
                )
            weights[component] = float(weight)
        object.__setattr__(self, "weights", weights)

    # ---- queries ------------------------------------------------------- #
    def declaration(self, component: CostComponent) -> CostComponentDeclaration:
        return self.declarations[component]

    def weight(self, component: CostComponent) -> float:
        """Weight of a component; ``0.0`` when unset."""
        return float(self.weights.get(component, 0.0))

    def is_weighted(self) -> bool:
        return bool(self.weights)

    def unimplemented_components(self) -> tuple[CostComponent, ...]:
        """Components that cannot be scored yet, in declaration order."""
        return tuple(
            declaration.component
            for declaration in self.declarations.values()
            if declaration.status is not ComponentStatus.IMPLEMENTED
        )

    def describe(self) -> str:
        if not self.weights:
            return f"{self.name} (no weights configured yet)"
        parts = [f"{component.value}={weight:g}" for component, weight in sorted(
            self.weights.items(), key=lambda item: item[0].value
        )]
        return f"{self.name} ({', '.join(parts)})"


def empty_cost_policy(name: str = "stage0_no_weights") -> RouteCostPolicy:
    """A policy with the Stage 0 capability table and no weights."""
    return RouteCostPolicy(name=name)
