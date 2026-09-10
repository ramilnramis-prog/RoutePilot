"""Optimizer entry point (Stage 2, spec sections 12, 21, 30; decision D17).

Decision D17 fixes the shape of the MVP optimizer:

    constraint-aware greedy seed -> deterministic local improvement (2-opt class)

and requires that optimization stays behind a solver abstraction, isolated from UI, HTTP,
storage and routing vendors. The committed objective is complete-route feasibility and
elapsed time - never a hidden numeric penalty (D13 amendment).

Status: **the solver itself is not implemented in this unit.** What exists is the evaluation
half, :mod:`core.engine.optimizer.route_evaluation`: it evaluates a complete order (including
the final leg to FINISH), validates it, reports hard-window violations explicitly and builds
the committed solution with the USER and ALGORITHM baselines.

The stage change set splits the remaining work so nothing is silently approximated:

* unit U2 - the greedy seed (the ``algorithm_order`` this entry point receives) and its
  deterministic tie-breaking;
* unit U3 - deterministic local improvement, which may never accept a move that worsens the
  accepted objective (spec section 21, D17);
* unit U4 - the recommendation over complete route outcomes, top-K and the measured
  performance benchmark (spec sections 13, 20).

Until those exist, :func:`solve` raises instead of returning an order, so no caller can mistake
an unimplemented solver for a working one (D16).
"""

from __future__ import annotations

from collections.abc import Sequence

from core.engine.providers import TravelMatrix
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.solution import RouteSolution
from core.validation.errors import UnsupportedFeatureError

__all__ = ["solve"]


def solve(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    algorithm_order: Sequence[StopId],
) -> RouteSolution:
    """Build the committed route of ``plan`` around the driver's selected first stop.

    Not implemented in this unit. Implementing it requires the greedy seed and the local
    improvement (Stage 2 change set items 6/7, D17), and a partial solver would silently
    understate the route - so it raises :class:`UnsupportedFeatureError` instead of returning
    a route nobody measured.

    The pieces that *are* implemented are reached directly:
    :func:`core.engine.optimizer.evaluate_order` for one complete route and
    :func:`core.engine.optimizer.build_solution` for the committed solution plus its baselines.
    """
    raise UnsupportedFeatureError(
        "the RoutePilot optimizer (constraint-aware greedy seed + deterministic local "
        "improvement)",
        planned_stage="the next Stage 2 unit",
    )
