"""The optimizer entry point (Stage 2 unit U2; v2 sections 12, 15, 21, 30; decision D17).

D17 fixes the shape of the MVP optimizer - *constraint-aware greedy seed, then deterministic local
improvement (2-opt class)* - and requires it to stay isolated from UI, HTTP, storage and routing
vendors. This module is that pipeline and nothing else:

    RouteProblem -> greedy_seed -> improve -> evaluate_order -> build_solution

Every stage is separately callable and separately tested, and the final answer is always the
authoritative :func:`~core.engine.optimizer.route_evaluation.evaluate_order` of the produced
order. The fast path is the inner loop's view of the same arithmetic; it is never the source of
the committed metrics.

The committed objective is complete-route elapsed time, FINISH leg included (v2 section 15), and
hard-window feasibility stays explicit: the optimizer minimizes the number of hard-window
violations first and never turns one into a numeric penalty (D13 amendment).

Not here, on purpose: candidate ranking, top-K, the benchmark tool and any prefilter. Spec section
20 forbids a fixed-K prefilter before the exhaustive approach has been benchmarked, and those are
later units.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.engine.optimizer.cache import CacheStats
from core.engine.optimizer.local_search import LocalSearchResult, improve
from core.engine.optimizer.route_evaluation import RouteEvaluation, evaluate_order
from core.engine.optimizer.route_problem import (
    FastEvaluation,
    RouteProblem,
    fast_evaluate,
)
from core.engine.optimizer.seed import greedy_seed
from core.model.ids import StopId

__all__ = ["OptimizedRoute", "optimize"]


@dataclass(frozen=True)
class OptimizedRoute:
    """One optimized route with everything needed to explain and to audit it.

    ``evaluation`` is authoritative (v2 section 15 metrics, explicit violations).
    ``algorithm_evaluation`` is the greedy seed evaluated the same way: the ALGORITHM BASELINE of
    v2 section 30, kept for optimizer-quality diagnostics and never shown as the user's BEFORE
    route (D22). ``local_search`` reports the accepted moves and the bounded evaluation count.
    """

    order: tuple[StopId, ...]
    evaluation: RouteEvaluation
    algorithm_evaluation: RouteEvaluation
    local_search: LocalSearchResult
    cache_stats: CacheStats

    def __post_init__(self) -> None:
        order = tuple(self.order)
        object.__setattr__(self, "order", order)
        if self.evaluation.order != order:
            raise ValueError("the optimized order and its evaluation must be the same route")
        if self.algorithm_evaluation.order[0] != order[0]:
            raise ValueError(
                "the algorithm baseline must start at the same selected first stop as the "
                "committed route (I3, D10)"
            )

    @property
    def seed_objective_sec(self) -> int:
        """Elapsed seconds of the greedy seed's complete route (waiting included, v2 section 15)."""
        return self.local_search.seed_objective

    @property
    def final_objective_sec(self) -> int:
        """Elapsed seconds of the optimized complete route (waiting included, v2 section 15)."""
        return self.local_search.final_objective


def optimize(problem: RouteProblem) -> OptimizedRoute:
    """Run the whole deterministic pipeline on a prepared problem.

    Deterministic: no wall-clock, no randomness and no network are involved, so an identical
    :class:`RouteProblem` always produces an identical route, identical metrics and identical
    accepted moves.
    """
    seed_order: tuple[StopId, ...] = greedy_seed(problem) if problem.stop_count else ()
    seed_fast = fast_evaluate(problem, seed_order)
    search = improve(problem, seed_order)
    final_fast = fast_evaluate(problem, search.order)
    evaluation = evaluate_order(
        plan=problem.plan, travel_matrix=problem.legs, order=search.order
    )
    algorithm_evaluation = evaluate_order(
        plan=problem.plan, travel_matrix=problem.legs, order=seed_order
    )

    _assert_fast_path_agrees(problem, seed_fast, algorithm_evaluation)
    _assert_fast_path_agrees(problem, final_fast, evaluation)

    return OptimizedRoute(
        order=search.order,
        evaluation=evaluation,
        algorithm_evaluation=algorithm_evaluation,
        local_search=search,
        cache_stats=problem.cache_stats,
    )


def _assert_fast_path_agrees(
    problem: RouteProblem, fast: FastEvaluation, authoritative: RouteEvaluation
) -> None:
    """The optimized path keeps its own invariant checked: the fast path is not a second model.

    This compares the fast pass against the authoritative evaluation of the same order on every
    optimization run, so a drift between them is a loud error instead of a quietly different
    number. It is a comparison of results, not a second computation of the route.
    """
    metrics = authoritative.metrics
    mismatches = []
    if fast.finish_elapsed_sec != metrics.duration_sec:
        mismatches.append(f"objective {fast.finish_elapsed_sec} != {metrics.duration_sec}")
    if fast.travel_sec != metrics.travel_sec:
        mismatches.append(f"travel {fast.travel_sec} != {metrics.travel_sec}")
    if fast.waiting_sec != metrics.waiting_sec:
        mismatches.append(f"waiting {fast.waiting_sec} != {metrics.waiting_sec}")
    if fast.service_sec != metrics.service_sec:
        mismatches.append(f"service {fast.service_sec} != {metrics.service_sec}")
    if fast.distance_m != metrics.distance_m:
        mismatches.append(f"distance {fast.distance_m} != {metrics.distance_m}")
    if fast.violations != len(authoritative.violations):
        mismatches.append(
            f"violations {fast.violations} != {len(authoritative.violations)}"
        )
    if mismatches:
        raise AssertionError(
            "the fast evaluation path disagrees with the authoritative evaluate_order for plan "
            f"{problem.plan.id!r}: " + "; ".join(mismatches)
        )
