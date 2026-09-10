"""Complete-route optimization (Stage 2, spec sections 12, 15, 21, 30).

Two halves, deliberately separated:

* :mod:`core.engine.optimizer.route_evaluation` - evaluate one **complete** order
  (``START -> enabled stops -> FINISH``, final leg included) and turn it into validated
  metrics, an explicit violation set and a committed solution;
* :mod:`core.engine.optimizer.solve` - the solver entry point (greedy seed + local
  improvement) that later units provide.

Only the evaluation half exists today. It reuses the authoritative timeline arithmetic
(:func:`core.time.timeline.compute_stop_timeline`) for every service stop, so a computed
leg and a real route leg can never drift apart.
"""

from __future__ import annotations

from core.engine.optimizer.route_evaluation import (
    RouteEvaluation,
    build_solution,
    evaluate_order,
    user_baseline_order,
)
from core.engine.optimizer.solve import solve

__all__ = [
    "RouteEvaluation",
    "build_solution",
    "evaluate_order",
    "solve",
    "user_baseline_order",
]
