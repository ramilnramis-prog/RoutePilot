"""First-stop candidate evaluation - the recommendation engine (spec sections 1, 8, 9; D32).

This module **evaluates** every possible first stop and ranks them: it times the first leg with the
real timeline arithmetic and prices it with the cost policy. What it produces is a
**recommendation**. It deliberately does **not** select, pin, persist or apply anything - the
driver decides (D4), and the driver's decision lives in
:class:`~core.model.first_stop.FirstStopIntent`, not here.

Spec section 8 requires candidate quality to include the route *after* the candidate, and D32
requires ranking **complete route outcomes** (``START -> candidate -> optimized remaining stops ->
FINISH``). The optimizer that can produce those outcomes is Stage 2, so
:attr:`CandidateEvaluation.remaining_route_estimate` is still ``None`` and the score covers the
first leg only. This is stated rather than hidden, because on the first leg alone every candidate
that arrives before opening has the same ``travel + waiting`` total - exactly the degeneracy the
remaining-route term exists to break.

A candidate whose **hard** service window cannot be met is never ranked: it is returned in a
separate ``infeasible`` collection with its explicit Violation, so infeasibility is reported rather
than folded into a comparable score (D13 amendment). Once complete-route outcomes exist, this rule
applies to the whole route, not just the first leg (D32).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.engine.cost import breakdown_as_tuple, score_breakdown
from core.engine.providers import TravelMatrix
from core.model.cost_policy import CostComponent, RouteCostPolicy
from core.model.ids import PlanId, StopId
from core.model.route_plan import RoutePlan
from core.model.service_window import WindowEndPolicy
from core.model.solution import (
    Feasibility,
    StopTimeline,
    Violation,
)
from core.model.value_objects import DurationSec, Instant
from core.time import timeline as timeline_engine
from core.validation.errors import InvalidCostPolicyError, InvalidRoutePlanError

__all__ = [
    "CandidateEvaluation",
    "FirstStopEvaluationReport",
    "evaluate_first_stop_candidates",
]


@dataclass(frozen=True)
class CandidateEvaluation:
    """One possible first stop, timed and priced.

    ``score`` is only comparable between **feasible** candidates: an infeasible stop is excluded
    from ranking and carries a ``violation`` instead of a punitive score.
    """

    stop_id: StopId
    timeline: StopTimeline
    distance_m: float
    cost_breakdown: tuple[tuple[CostComponent, float], ...]
    score: float
    violation: Violation | None = None
    #: Reserved for spec section 8; the optimizer that can produce it arrives in Stage 2.
    remaining_route_estimate: DurationSec | None = None

    def __post_init__(self) -> None:
        if self.distance_m < 0:
            raise InvalidRoutePlanError("distance_m must be >= 0")
        if not math.isfinite(self.score):
            raise InvalidRoutePlanError(f"score must be finite, got {self.score!r}")
        if (self.violation is not None) != self.timeline.is_infeasible:
            raise InvalidRoutePlanError(
                "a candidate carries a violation exactly when its timeline is infeasible; "
                "infeasibility is never implied or hidden"
            )

    # ---- delegated timeline facts (single source of truth) ------------- #
    @property
    def feasible(self) -> bool:
        return not self.timeline.is_infeasible

    @property
    def travel_time(self) -> DurationSec:
        return self.timeline.travel_time

    @property
    def waiting_time(self) -> DurationSec:
        return self.timeline.waiting_time

    @property
    def estimated_arrival(self) -> Instant:
        return self.timeline.estimated_arrival

    @property
    def service_start(self) -> Instant:
        return self.timeline.service_start

    @property
    def estimated_departure(self) -> Instant:
        return self.timeline.estimated_departure

    @property
    def service_window_start(self) -> Instant | None:
        return self.timeline.service_window_start

    @property
    def service_window_end(self) -> Instant | None:
        return self.timeline.service_window_end

    @property
    def window_end_policy(self) -> WindowEndPolicy | None:
        return self.timeline.window_end_policy

    @property
    def lateness(self) -> DurationSec:
        return self.timeline.lateness

    @property
    def finish_overtime(self) -> DurationSec:
        return self.timeline.finish_overtime

    @property
    def feasibility(self) -> Feasibility:
        return self.timeline.feasibility

    # ---- cost ---------------------------------------------------------- #
    def component(self, component: CostComponent) -> float:
        """Measured value of one cost component (0.0 when not part of the breakdown)."""
        for candidate_component, value in self.cost_breakdown:
            if candidate_component is component:
                return value
        return 0.0

    def breakdown_dict(self) -> dict[CostComponent, float]:
        return dict(self.cost_breakdown)


@dataclass(frozen=True)
class FirstStopEvaluationReport:
    """Every candidate for the first service stop, ranked deterministically - a recommendation.

    Ranking rule: feasible candidates only, ordered by ``(score, travel_time, stop_id)``. The
    explicit tie-break makes the result reproducible even when a weight set cannot separate
    candidates. The report is advisory input for the driver, never a decision (D32).
    """

    plan_id: PlanId
    inputs_fingerprint: str
    policy_name: str
    policy_is_provisional: bool
    window_end_policy: WindowEndPolicy
    ranked: tuple[CandidateEvaluation, ...] = field(default_factory=tuple)
    infeasible: tuple[CandidateEvaluation, ...] = field(default_factory=tuple)
    disabled_stop_ids: tuple[StopId, ...] = field(default_factory=tuple)

    @property
    def has_candidates(self) -> bool:
        return bool(self.ranked) or bool(self.infeasible)

    def recommended(self) -> CandidateEvaluation | None:
        """The candidate RoutePilot *recommends* as the first stop, or ``None`` if none is feasible.

        A recommendation only: nothing is applied, nothing is pinned and no working route is
        committed until the driver chooses (D4/D32, I4).
        """
        return self.ranked[0] if self.ranked else None

    @property
    def recommended_stop_id(self) -> StopId | None:
        recommended = self.recommended()
        return recommended.stop_id if recommended is not None else None

    def nearest(self) -> CandidateEvaluation | None:
        """Feasible candidate with the shortest first leg (the naive answer)."""
        if not self.ranked:
            return None
        return min(self.ranked, key=lambda e: (e.travel_time, e.stop_id))

    def farthest(self) -> CandidateEvaluation | None:
        """Feasible candidate with the longest first leg."""
        if not self.ranked:
            return None
        return min(self.ranked, key=lambda e: (-e.travel_time, e.stop_id))

    def find(self, stop_id: StopId) -> CandidateEvaluation | None:
        for evaluation in self.ranked:
            if evaluation.stop_id == stop_id:
                return evaluation
        for evaluation in self.infeasible:
            if evaluation.stop_id == stop_id:
                return evaluation
        return None

    def rank_of(self, stop_id: StopId) -> int | None:
        """1-based rank among feasible candidates, or ``None`` for infeasible/unknown stops."""
        for position, evaluation in enumerate(self.ranked, start=1):
            if evaluation.stop_id == stop_id:
                return position
        return None

    def ranked_ids(self) -> tuple[StopId, ...]:
        return tuple(evaluation.stop_id for evaluation in self.ranked)

    def top(self, count: int) -> tuple[CandidateEvaluation, ...]:
        return self.ranked[:count]


def evaluate_first_stop_candidates(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    policy: RouteCostPolicy | None = None,
) -> FirstStopEvaluationReport:
    """Time and price every possible first stop of ``plan``.

    The first leg is computed by the same :func:`core.time.timeline.compute_stop_timeline` used
    for real route legs, so a candidate's numbers can never drift from the timeline.

    Raises:
        InvalidCostPolicyError: the policy has no weights at all. Ranking candidates with a
            zero objective would look like a decision while carrying no information.
        StopNotGeocodedError: an enabled stop has no coordinates (resolve addresses first,
            spec section 16).
    """
    cost_policy = policy if policy is not None else plan.cost_policy
    if not cost_policy.is_weighted():
        raise InvalidCostPolicyError(
            f"cost policy {cost_policy.name!r} has no weights, so first-stop candidates cannot be "
            "ranked meaningfully (D31: weights are configuration, not hidden defaults)"
        )

    tzinfo = plan.load_timezone()
    evaluations: list[CandidateEvaluation] = []

    for stop in plan.active_stops():
        timeline = timeline_engine.compute_stop_timeline(
            plan=plan,
            stop=stop,
            departure_from_previous=plan.departure_time,
            previous_point=plan.departure_point,
            travel_provider=travel_matrix,
            tzinfo=tzinfo,
        )
        location = stop.location
        assert location is not None  # compute_stop_timeline raises otherwise
        distance_m = float(travel_matrix.distance_meters(plan.departure_point, location))
        breakdown = {
            CostComponent.TRAVEL_TIME: float(timeline.travel_time),
            CostComponent.WAITING_TIME: float(timeline.waiting_time),
            CostComponent.DISTANCE: distance_m,
        }
        evaluations.append(
            CandidateEvaluation(
                stop_id=stop.id,
                timeline=timeline,
                distance_m=distance_m,
                cost_breakdown=breakdown_as_tuple(breakdown),
                score=score_breakdown(breakdown, cost_policy),
                violation=timeline_engine.violation_for(timeline),
            )
        )

    ranked = tuple(
        sorted(
            (evaluation for evaluation in evaluations if evaluation.feasible),
            key=lambda evaluation: (
                evaluation.score,
                evaluation.travel_time,
                evaluation.stop_id,
            ),
        )
    )
    infeasible = tuple(
        sorted(
            (evaluation for evaluation in evaluations if not evaluation.feasible),
            key=lambda evaluation: (evaluation.travel_time, evaluation.stop_id),
        )
    )

    return FirstStopEvaluationReport(
        plan_id=plan.id,
        inputs_fingerprint=plan.inputs_fingerprint(),
        policy_name=cost_policy.name,
        policy_is_provisional=cost_policy.provisional,
        window_end_policy=plan.window_end_policy,
        ranked=ranked,
        infeasible=infeasible,
        disabled_stop_ids=tuple(stop.id for stop in plan.disabled_stops()),
    )
