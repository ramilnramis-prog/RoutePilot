"""Complete-route optimization (Stage 2, spec sections 12, 15, 21, 30).

Three layers, deliberately separated:

* :mod:`core.engine.optimizer.route_evaluation` - evaluate one **complete** order
  (``START -> enabled stops -> FINISH``, final leg included) and turn it into validated metrics,
  an explicit violation set and a committed solution. This stays the authoritative arithmetic;
* :mod:`core.engine.optimizer.route_problem` - the frozen, prepared problem (plan + shared leg
  cache + the driver's selected first stop + a precomputed service-window table) and the fast
  forward pass the optimizer's inner loop uses;
* :mod:`core.engine.optimizer.seed`, :mod:`core.engine.optimizer.local_search` and
  :mod:`core.engine.optimizer.optimize` - the constraint-aware greedy seed, deterministic local
  improvement and the pipeline that runs them (D17).

:mod:`core.engine.optimizer.cache` memoizes legs and reports hits and misses, so the reuse is
measurable (v2 section 20); :mod:`core.engine.optimizer.route_fingerprint` adds the committed
route's own fingerprint, which - unlike ``RoutePlan.inputs_fingerprint()`` - does depend on the
driver's selected first stop (v2 section 7).

Candidate ranking, top-K and ``no_fully_feasible_route`` integration are later Stage 2 units and
are not implemented here. The exhaustive first-stop benchmark of v2 section 20 lives in
``tools/benchmark_optimizer.py`` and its deterministic ~100-stop fixture in
``demo/scale_dataset.py``.
"""

from __future__ import annotations

from core.engine.optimizer.cache import CacheStats, LegCache
from core.engine.optimizer.local_search import LocalSearchResult, RouteMove, improve
from core.engine.optimizer.optimize import OptimizationEvidence, OptimizedRoute, optimize
from core.engine.optimizer.route_evaluation import (
    RouteEvaluation,
    build_solution,
    evaluate_order,
    user_baseline_order,
)
from core.engine.optimizer.route_fingerprint import route_fingerprint
from core.engine.optimizer.route_problem import (
    FastEvaluation,
    OpenState,
    ResolvedWindow,
    RouteProblem,
    build_problem,
    fast_evaluate,
    fast_evaluation_key,
    fast_objective,
    fast_route_evaluation,
)
from core.engine.optimizer.seed import SeedResult, build_seed, greedy_seed
from core.engine.optimizer.solve import solve, solve_route

__all__ = [
    "CacheStats",
    "FastEvaluation",
    "LegCache",
    "LocalSearchResult",
    "OpenState",
    "OptimizationEvidence",
    "OptimizedRoute",
    "ResolvedWindow",
    "RouteEvaluation",
    "RouteMove",
    "RouteProblem",
    "SeedResult",
    "build_problem",
    "build_seed",
    "build_solution",
    "evaluate_order",
    "fast_evaluate",
    "fast_evaluation_key",
    "fast_objective",
    "fast_route_evaluation",
    "greedy_seed",
    "improve",
    "optimize",
    "route_fingerprint",
    "solve",
    "solve_route",
    "user_baseline_order",
]
