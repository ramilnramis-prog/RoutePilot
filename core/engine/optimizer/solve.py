"""Optimizer entry point (Stage 2, spec sections 12, 21, 30; decisions D17, D22, D32).

Decision D17 fixes the shape of the MVP optimizer:

    constraint-aware greedy seed -> deterministic local improvement (2-opt class)

and requires that optimization stays behind a solver abstraction, isolated from UI, HTTP,
storage and routing vendors. This module is that boundary:

* :func:`optimize` runs the pipeline on a prepared
  :class:`~core.engine.optimizer.route_problem.RouteProblem` and returns the order, the
  authoritative evaluation, the greedy seed's evaluation (the ALGORITHM BASELINE of v2 section 30)
  and the local-search evidence;
* :func:`solve_route` commits that route with :func:`build_solution`, so the
  :class:`~core.model.solution.RouteSolution` carries the OPTIMIZED route plus the USER and
  ALGORITHM baselines (D22) and the plan's recommendation fingerprint (v2 section 7).

A committed route requires the driver's explicit first stop. When the plan is still
``awaiting_first_stop_choice``, :func:`solve_route` raises instead of choosing a stop on the
driver's behalf (I4/D32): the engine recommends, the driver decides.
"""

from __future__ import annotations

from core.engine.optimizer.cache import LegCache
from core.engine.optimizer.optimize import optimize
from core.engine.optimizer.route_evaluation import build_solution
from core.engine.optimizer.route_problem import build_problem
from core.engine.providers import TravelMatrix
from core.model.route_plan import RoutePlan
from core.model.solution import RouteSolution

__all__ = ["solve", "solve_route"]


def solve_route(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    cache: LegCache | None = None,
) -> RouteSolution:
    """Optimize ``plan`` around the driver's selected first stop and commit the route.

    Args:
        plan: the plan to route; it must carry an explicit driver selection (I3/I4, D32).
        travel_matrix: travel times and distances; it is wrapped in a shared
            :class:`~core.engine.optimizer.cache.LegCache`, so every leg is priced once.
        cache: an existing cache to reuse, for a caller that evaluates several candidates and
            wants to share their common legs (v2 section 20).

    Raises:
        InvalidRoutePlanError: the plan has no driver-selected first service stop.
        InvalidOrderError: the produced route is not exactly the enabled stops, each once.
    """
    problem = build_problem(plan=plan, travel_matrix=travel_matrix, cache=cache)
    optimized = optimize(problem)
    return build_solution(
        plan=plan,
        travel_matrix=problem.legs,
        order=optimized.order,
        algorithm_order=optimized.algorithm_evaluation.order,
    )


def solve(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    algorithm_order=None,
    cache: LegCache | None = None,
) -> RouteSolution:
    """The solver boundary: optimize ``plan`` and commit the route.

    ``algorithm_order`` is accepted for compatibility with the earlier evaluation-only boundary
    and is ignored: the ALGORITHM baseline is now the greedy seed this optimizer actually
    produced, which is what v2 section 30 asks for. Passing a caller-supplied seed would let the
    baseline describe a route the optimizer never built.

    Raises:
        InvalidRoutePlanError: the plan has no driver-selected first service stop (I4/D32).
    """
    del algorithm_order
    return solve_route(plan=plan, travel_matrix=travel_matrix, cache=cache)
