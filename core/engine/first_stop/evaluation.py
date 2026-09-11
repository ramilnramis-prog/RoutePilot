"""Complete-route first-stop recommendation (Stage 2 unit U4; v2 sections 12-14, 20, D32).

This module **evaluates** every enabled stop as a first-stop candidate and ranks the resulting
**complete routes**. It produces a **recommendation**; it never selects, pins, persists or applies
anything - the driver decides (D4/D32/I5).

What "complete" means here, and why it is not the first leg (v2 sections 12, 15, D32):

* every candidate is optimized as ``START -> candidate -> optimized remaining enabled stops ->
  FINISH``, with the final leg to FINISH included in every metric;
* the candidate set is **exhaustive**: one optimizer run per enabled stop, with no prefilter, no
  shortlist and no skipped candidate (v2 section 20, D34). Only the *per-candidate* search effort
  is bounded, and that bound is U3's documented behaviour (``budget_exhausted``), not a candidate
  filter;
* one :class:`~core.engine.optimizer.cache.LegCache` is shared by the whole evaluation, so the legs
  the candidates have in common are priced once and the reuse is measured instead of claimed
  (v2 section 20). The *prepared* problem is deliberately **not** shared: every candidate is its
  own :class:`~core.engine.optimizer.route_problem.RouteProblem` (``with_first_stop``) and rebuilds
  its tables. What is reused across candidates is only that leg cache and the process-wide
  timezone-resolution memo; ``evaluate_first_stop_candidates`` gives the measured cost (U4 review).

Feasibility is a property of the **complete** route (v2 section 14, D32): a candidate whose
remainder contains a hard-window miss is not in the ranking at all. It is kept in ``rejected`` with
the ids of the stops that violate and the reason, so infeasibility is reported as data rather than
folded into a comparable score (D13 amendment). When no candidate's complete route is fully
feasible the status is ``no_fully_feasible_route`` and there is no recommended stop - never a
fabricated winner.

The objective comes from the configured :class:`~core.model.cost_policy.RouteCostPolicy` applied to
the complete route's measured breakdown (travel, waiting, distance) through
:func:`core.engine.cost.score_breakdown`. No weight is invented or tuned here, and service time -
constant across the candidates of one plan - is reported, never scored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from core.engine.cost import score_breakdown
from core.engine.optimizer.cache import CacheStats
from core.engine.optimizer.optimize import optimize
from core.engine.optimizer.route_evaluation import RouteEvaluation
from core.engine.optimizer.route_problem import RouteProblem, build_problem
from core.engine.providers import TravelMatrix
from core.model.cost_policy import CostComponent, RouteCostPolicy
from core.model.first_stop import (
    CandidateDiagnostic,
    CandidateMetrics,
    FirstStopCandidate,
    FirstStopRecommendation,
    RecommendationStatus,
)
from core.model.ids import PlanId, StopId
from core.model.route_plan import RoutePlan
from core.model.service_window import WindowEndPolicy
from core.model.solution import StopTimeline
from core.model.value_objects import DurationSec, Instant
from core.time import timeline as timeline_engine
from core.validation.errors import InvalidCostPolicyError, InvalidRoutePlanError

__all__ = [
    "FirstStopEvaluationReport",
    "diagnostics_of",
    "evaluate_first_stop_candidates",
    "metrics_of",
    "objective_of",
    "ranking_key",
    "score_of",
]


# --------------------------------------------------------------------------- #
# objective: the configured policy applied to the complete-route breakdown
# --------------------------------------------------------------------------- #
def metrics_of(evaluation: RouteEvaluation) -> CandidateMetrics:
    """The measured objective components of an evaluated complete route (v2 sections 12, 16)."""
    metrics = evaluation.metrics
    return CandidateMetrics(
        travel_sec=metrics.travel_sec,
        waiting_sec=metrics.waiting_sec,
        distance_m=metrics.distance_m,
    )


def score_of(metrics: CandidateMetrics, policy: RouteCostPolicy) -> float:
    """The candidate's objective: the configured policy weighted over its measured breakdown.

    The breakdown names the complete route's travel seconds, waiting seconds and metric distance.
    A component the policy does not weight contributes zero, so the objective stays explicitly
    configured and no weight is invented here (D13, D31).
    """
    return score_breakdown(
        {
            CostComponent.TRAVEL_TIME: float(metrics.travel_sec),
            CostComponent.WAITING_TIME: float(metrics.waiting_sec),
            CostComponent.DISTANCE: float(metrics.distance_m),
        },
        policy,
    )


def objective_of(evaluation: RouteEvaluation, policy: RouteCostPolicy) -> float:
    """The objective of an evaluated complete route."""
    return score_of(metrics_of(evaluation), policy)


def _check_service_time_is_constant(candidates: "tuple[FirstStopCandidate, ...]") -> None:
    """Every candidate serves the same stops, so total service time cannot separate them.

    It is reported rather than scored. This check makes a drift loud instead of letting a
    non-constant service figure silently look like a candidate-specific benefit.
    """
    service_times = {candidate.total_service_time for candidate in candidates}
    if len(service_times) > 1:
        raise InvalidRoutePlanError(
            "total service time must be identical across the candidates of one plan (every "
            f"candidate serves the same enabled stops), got {sorted(service_times)}"
        )


# --------------------------------------------------------------------------- #
# diagnostics: why a complete route was rejected (v2 section 14, D9)
# --------------------------------------------------------------------------- #
def _violated_stop_ids(evaluation: RouteEvaluation) -> tuple[StopId, ...]:
    """Ascending ids of the stops whose hard window the complete route misses.

    Ascending by id (not by route position) so the reported reason cannot depend on which
    order the optimizer happened to produce.
    """
    return tuple(
        sorted(timeline.stop_id for timeline in evaluation.timelines if timeline.is_infeasible)
    )


def diagnostics_of(
    evaluation: RouteEvaluation, *, first_stop_id: StopId | None = None
) -> tuple[CandidateDiagnostic, ...]:
    """One explicit rejection reason per violating stop of a complete route (v2 section 14, D9).

    Each entry names the violating stop (``stop_id``), the violation kind as its ``code``, the
    violation's own message, a ``reason`` that says which candidate's complete route could not
    serve it, and - as the field ``candidate_stop_id`` - the candidate that reason belongs to. The
    candidate link is data, not a substring of the prose, so a caller can ask a rejected candidate
    for its own reasons by its own id even when the miss is at a **later** stop, where the
    violating stop is a different stop from the candidate.
    """
    candidate_id = first_stop_id if first_stop_id is not None else (
        evaluation.order[0] if evaluation.order else None
    )
    diagnostics = [
        _diagnostic_for(timeline, candidate_id)
        for timeline in evaluation.timelines
        if timeline.is_infeasible
    ]
    # Fully ordered by the diagnostic's own data - violating stop, code, then candidate - so the
    # order never depends on the order the optimizer happened to produce the timelines in.
    diagnostics.sort(
        key=lambda diagnostic: (
            diagnostic.stop_id,
            diagnostic.code,
            diagnostic.candidate_stop_id or "",
        )
    )
    return tuple(diagnostics)


def _diagnostic_for(timeline: StopTimeline, candidate_id: StopId | None) -> CandidateDiagnostic:
    violation = timeline_engine.violation_for(timeline)
    if violation is None:  # pragma: no cover - an infeasible timeline always carries one
        raise InvalidRoutePlanError(
            f"stop {timeline.stop_id!r} is infeasible but carries no explicit violation"
        )
    policy_name = (
        timeline.window_end_policy.value if timeline.window_end_policy else "no window end policy"
    )
    return CandidateDiagnostic(
        stop_id=timeline.stop_id,
        code=violation.kind.value,
        message=violation.message,
        reason=(
            f"candidate first stop {candidate_id}: stop {timeline.stop_id} cannot be served "
            f"within its permitted window under {policy_name} (lateness {timeline.lateness}s)"
        ),
        candidate_stop_id=candidate_id,
        violation_kind=violation.kind,
    )


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #
def ranking_key(
    candidate: FirstStopCandidate, *, score: float, input_position: int
) -> tuple[float, DurationSec, int, StopId]:
    """The deterministic ranking key of a fully feasible candidate.

    ``(objective, complete route duration, input_position, stop_id)``. A weight set that cannot
    separate two candidates still leaves a reproducible order, and ``input_position`` - the
    immutable input-order provenance of D33 - is the stable human-facing tie-break before the id.
    """
    return (
        score,
        candidate.estimated_complete_route_duration,
        input_position,
        candidate.stop_id,
    )


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FirstStopEvaluationReport:
    """The complete-route recommendation outcome for one plan (v2 sections 12-14, D32).

    ``ranked`` holds only candidates whose complete route is fully feasible, ordered
    deterministically by :func:`ranking_key`; ``ranked[0]`` is the recommendation and ``top(k)``
    exposes the alternatives (v2 section 13, D32: alternatives are never hidden). ``rejected``
    holds every candidate whose complete route violates a hard window, each with its violating
    stop ids and reasons (v2 section 14).

    The report is advisory input for the driver and nothing else: it never selects, pins or
    applies a first stop, so ``plan.first_stop_state`` stays ``awaiting_first_stop_choice``
    (D4/D32/I5).
    """

    plan_id: PlanId
    status: RecommendationStatus
    inputs_fingerprint: str
    policy_name: str
    policy_is_provisional: bool
    window_end_policy: WindowEndPolicy
    ranked: tuple[FirstStopCandidate, ...] = field(default_factory=tuple)
    rejected: tuple[FirstStopCandidate, ...] = field(default_factory=tuple)
    diagnostics: tuple[CandidateDiagnostic, ...] = field(default_factory=tuple)
    disabled_stop_ids: tuple[StopId, ...] = field(default_factory=tuple)
    candidates_evaluated: int = 0
    optimizer_runs: int = 0
    cache_stats: CacheStats = CacheStats(0, 0, 0)
    resolved_at: Instant | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ranked", tuple(self.ranked))
        object.__setattr__(self, "rejected", tuple(self.rejected))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "disabled_stop_ids", tuple(self.disabled_stop_ids))
        if not isinstance(self.status, RecommendationStatus):
            object.__setattr__(self, "status", RecommendationStatus(self.status))

        if self.status is RecommendationStatus.RECOMMENDED and not self.ranked:
            raise InvalidRoutePlanError(
                "status='recommended' requires at least one fully feasible ranked candidate"
            )
        if self.status is not RecommendationStatus.RECOMMENDED and self.ranked:
            raise InvalidRoutePlanError(
                f"status={self.status.value!r} must not carry ranked candidates: an infeasible "
                "candidate is never presented as a valid route (v2 section 14)"
            )
        if self.candidates_evaluated != len(self.ranked) + len(self.rejected):
            raise InvalidRoutePlanError(
                "every evaluated candidate is either ranked or rejected: the report must not "
                "silently drop a candidate (v2 section 20 forbids an unreported prefilter)"
            )

    # ---- the recommendation -------------------------------------------- #
    @property
    def status_name(self) -> str:
        return self.status.value

    def recommended(self) -> FirstStopCandidate | None:
        """The candidate RoutePilot *recommends*, or ``None`` when none is fully feasible.

        A recommendation only: nothing is applied, nothing is pinned and no working route is
        committed until the driver chooses (D4/D32, I4/I5).
        """
        return self.ranked[0] if self.ranked else None

    @property
    def recommended_stop_id(self) -> StopId | None:
        recommended = self.recommended()
        return recommended.stop_id if recommended is not None else None

    def top(self, count: int) -> tuple[FirstStopCandidate, ...]:
        """The highest-ranked fully feasible candidates, best first (v2 section 13)."""
        if count < 0:
            raise ValueError("count must be >= 0")
        return self.ranked[:count]

    def to_recommendation(self, *, resolved_at: Instant | None = None) -> FirstStopRecommendation:
        """The domain recommendation this report describes (D11/D32).

        ``resolved_at`` defaults to the plan's departure time, so the value is deterministic and
        never reads a wall clock; the fingerprint is the plan's ``inputs_fingerprint``, which
        deliberately excludes the driver's decision (v2 section 7, D4/D33).
        """
        resolved = resolved_at if resolved_at is not None else self.resolved_at
        if self.status is not RecommendationStatus.RECOMMENDED or not self.ranked:
            return FirstStopRecommendation.none(self.status, diagnostics=self.diagnostics)
        if resolved is None:
            raise InvalidRoutePlanError(
                "a 'recommended' outcome needs resolved_at; the evaluation report carries the "
                "plan's departure time so the result never reads a wall clock"
            )
        return FirstStopRecommendation.recommended(
            self.ranked[0].stop_id,
            ranked=self.ranked,
            resolved_at=resolved,
            inputs_fingerprint=self.inputs_fingerprint,
            diagnostics=self.diagnostics,
        )

    # ---- queries -------------------------------------------------------- #
    @property
    def has_candidates(self) -> bool:
        return bool(self.ranked) or bool(self.rejected)

    def ranked_ids(self) -> tuple[StopId, ...]:
        return tuple(candidate.stop_id for candidate in self.ranked)

    def rejected_ids(self) -> tuple[StopId, ...]:
        return tuple(candidate.stop_id for candidate in self.rejected)

    def find(self, stop_id: StopId) -> FirstStopCandidate | None:
        """A candidate by stop id, whether it was ranked or rejected."""
        for candidate in self.ranked:
            if candidate.stop_id == stop_id:
                return candidate
        for candidate in self.rejected:
            if candidate.stop_id == stop_id:
                return candidate
        return None

    def rank_of(self, stop_id: StopId) -> int | None:
        """1-based rank among fully feasible candidates, or ``None`` for rejected/unknown stops."""
        for position, candidate in enumerate(self.ranked, start=1):
            if candidate.stop_id == stop_id:
                return position
        return None

    def reasons_for(self, candidate_stop_id: StopId) -> tuple[CandidateDiagnostic, ...]:
        """Every rejection reason recorded for one candidate, looked up by the candidate's own id.

        The key is the **candidate's** first stop (:attr:`CandidateDiagnostic.candidate_stop_id`),
        never the violating stop's: a rejected candidate whose own first stop is served inside its
        window has no diagnostic whose ``stop_id`` is its own, so keying on the violating stop
        answered nothing for it. Each returned diagnostic also carries the violating stop it is
        about in ``stop_id``, and the candidate's full set is
        :attr:`FirstStopCandidate.violating_stop_ids`; a stop that is not a rejected candidate
        (ranked, disabled or unknown) has no reasons and yields ``()``.
        """
        return tuple(
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.candidate_stop_id == candidate_stop_id
        )

    def describe_status(self) -> str:
        if self.status is RecommendationStatus.RECOMMENDED:
            return (
                f"recommended {self.recommended_stop_id} of {self.candidates_evaluated} "
                f"candidates evaluated ({len(self.ranked)} fully feasible, "
                f"{len(self.rejected)} rejected)"
            )
        if self.status is RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE:
            return (
                f"no_fully_feasible_route: all {self.candidates_evaluated} candidates have a "
                f"violating complete route ({len(self.diagnostics)} violations reported)"
            )
        return f"{self.status.value}: nothing to recommend"


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
def evaluate_first_stop_candidates(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    policy: RouteCostPolicy | None = None,
) -> FirstStopEvaluationReport:
    """Evaluate the **complete route** of every enabled stop as a first-stop candidate.

    The candidate set is exhaustive (v2 section 20, D34): one optimizer run per enabled stop, in
    the plan's ``input_position`` order, with no prefilter and no shortlist. All runs share one
    :class:`~core.engine.optimizer.cache.LegCache`, and the candidates' own route arithmetic is
    the authoritative :func:`core.engine.optimizer.route_evaluation.evaluate_order`, so a
    candidate's previewed complete route is produced by the same optimizer that will build the
    committed route (I6).

    What is shared and what is rebuilt, stated exactly (U4 review; corrected claim, not a silent
    one). **Shared across the candidates:** the one
    :class:`~core.engine.optimizer.cache.LegCache` - every leg is priced once and the reuse is
    reported in ``cache_stats`` instead of claimed - and the process-wide timezone-resolution memo
    ``core.engine.optimizer.route_problem._resolve_local_cached``, which resolves a
    ``(zone, local date, wall-clock time)`` at most once per process. **Rebuilt per candidate:**
    the :class:`~core.engine.optimizer.route_problem.RouteProblem` and every table it prepares
    (the frozen STOP/FINISH travel and distance snapshots, the per-stop input positions and the
    precomputed service-window table with its arrival-clock rows), because
    :meth:`~core.engine.optimizer.route_problem.RouteProblem.with_first_stop` constructs a fresh
    problem for each candidate. Measured for the 30-stop demo plan
    (:func:`demo.dataset.build_demo_plan`, 30 candidates, 30 optimizer runs):
    ``core.time.tz.resolve_local_datetime`` is called **3462** times - 270 local-midnight day
    boundaries (30 candidate problems x 9 prepared dates, re-resolved per candidate because that
    boundary is not memoized), 72 distinct fixed-window resolutions charged to the prepared
    window tables (the process-wide memo answers the other 2898 lookups on that path), and 3120
    inside the authoritative per-candidate route evaluation (1800 ``resolve_service_window``
    calls). Nothing here depends on the prepared tables being shared.

    The plan is never mutated: the recommendation is derived, advisory state (D4/D32/I5), so
    ``plan.first_stop_state`` stays ``awaiting_first_stop_choice`` afterwards.

    Raises:
        InvalidCostPolicyError: the policy has no weights at all. Ranking candidates with a
            zero objective would look like a decision while carrying no information (D31).
        StopNotGeocodedError: an enabled stop has no coordinates (resolve addresses first, spec
            section 16).
        MissingServiceDurationError: an enabled stop has no duration and the plan no default.
    """
    cost_policy = policy if policy is not None else plan.cost_policy
    if not cost_policy.is_weighted():
        raise InvalidCostPolicyError(
            f"cost policy {cost_policy.name!r} has no weights, so first-stop candidates cannot be "
            "ranked meaningfully (D31: weights are configuration, not hidden defaults)"
        )

    disabled_stop_ids = tuple(stop.id for stop in plan.disabled_stops())
    active_stops = plan.active_stops()
    if not active_stops:
        status = (
            RecommendationStatus.EMPTY_PLAN
            if not plan.stops
            else RecommendationStatus.NO_ACTIVE_STOPS
        )
        return FirstStopEvaluationReport(
            plan_id=plan.id,
            status=status,
            inputs_fingerprint=plan.inputs_fingerprint(),
            policy_name=cost_policy.name,
            policy_is_provisional=cost_policy.provisional,
            window_end_policy=plan.window_end_policy,
            disabled_stop_ids=disabled_stop_ids,
            resolved_at=plan.departure_time,
        )

    # One candidate is one problem: ``with_first_stop`` builds a **fresh** ``RouteProblem`` per
    # candidate, so its prepared tables are rebuilt per candidate, not prepared once for the whole
    # evaluation (U4 review: the earlier comment here claimed otherwise and was wrong). What the
    # candidates genuinely share is the one leg cache below - every leg is priced once and the
    # reuse is measured in ``cache_stats`` - and the process-wide timezone-resolution memo
    # ``route_problem._resolve_local_cached``. See ``evaluate_first_stop_candidates``'s docstring
    # for the measured cost of the rebuild on the 30-stop demo plan.
    base_problem = build_problem(
        plan=plan,
        travel_matrix=travel_matrix,
        first_stop_id=active_stops[0].id,
    )

    candidates: list[FirstStopCandidate] = []
    diagnostics: list[CandidateDiagnostic] = []
    ranking_scores: dict[StopId, float] = {}
    input_positions: dict[StopId, int] = {}

    for stop in active_stops:
        problem: RouteProblem = base_problem.with_first_stop(stop.id)
        optimized = optimize(problem)
        evaluation = optimized.evaluation
        objective = objective_of(evaluation, cost_policy)
        candidate = _build_candidate(
            evaluation=evaluation,
            stop_id=stop.id,
            objective=objective,
        )
        if evaluation.order != optimized.order:  # pragma: no cover - optimize guarantees it
            raise InvalidRoutePlanError(
                "the optimizer's committed order and its authoritative evaluation must be the "
                "same route (v2 section 15)"
            )
        candidates.append(candidate)
        input_positions[stop.id] = stop.input_position
        ranking_scores[stop.id] = objective
        if not candidate.feasible:
            diagnostics.extend(diagnostics_of(evaluation, first_stop_id=stop.id))

    _check_service_time_is_constant(tuple(candidates))

    ranked = tuple(
        sorted(
            (candidate for candidate in candidates if candidate.feasible),
            key=lambda candidate: ranking_key(
                candidate,
                score=ranking_scores[candidate.stop_id],
                input_position=input_positions[candidate.stop_id],
            ),
        )
    )
    rejected = tuple(
        sorted(
            (candidate for candidate in candidates if not candidate.feasible),
            key=lambda candidate: (
                candidate.estimated_complete_route_duration,
                input_positions[candidate.stop_id],
                candidate.stop_id,
            ),
        )
    )

    return FirstStopEvaluationReport(
        plan_id=plan.id,
        status=(
            RecommendationStatus.RECOMMENDED
            if ranked
            else RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE
        ),
        inputs_fingerprint=plan.inputs_fingerprint(),
        policy_name=cost_policy.name,
        policy_is_provisional=cost_policy.provisional,
        window_end_policy=plan.window_end_policy,
        ranked=ranked,
        rejected=rejected,
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.stop_id, item.code, item.candidate_stop_id or ""),
            )
        ),
        disabled_stop_ids=disabled_stop_ids,
        candidates_evaluated=len(candidates),
        optimizer_runs=len(active_stops),
        cache_stats=base_problem.cache_stats,
        resolved_at=plan.departure_time,
    )


def _build_candidate(
    *,
    evaluation: RouteEvaluation,
    stop_id: StopId,
    objective: float,
) -> FirstStopCandidate:
    """One candidate from the authoritative evaluation of its complete route (v2 section 12).

    The first-stop metrics come from the first timeline - the same object a real route leg uses,
    so a candidate's first-leg numbers cannot drift from the timeline - and every complete-route
    figure comes from the route's own metrics, FINISH leg included. The complete-route figures and
    the scored breakdown are built from that one evaluation, so a candidate can never carry two
    versions of the same route (see ``FirstStopCandidate``).
    """
    if not evaluation.timelines or evaluation.timelines[0].stop_id != stop_id:
        raise InvalidRoutePlanError(
            f"candidate {stop_id!r} must head its own complete route (I3, D32); got "
            f"{evaluation.order!r}"
        )

    first = evaluation.timelines[0]
    metrics = evaluation.metrics
    violating_stop_ids = _violated_stop_ids(evaluation)
    # One invariant in one place: the route's elapsed duration is exactly its measured parts, and
    # the finish instant is the first stop's departure plus the rest of the route.
    if metrics.duration_sec != metrics.travel_sec + metrics.waiting_sec + metrics.service_sec:
        raise InvalidRoutePlanError(
            "the complete route duration must equal travel + waiting + service over the whole "
            "route, FINISH leg included (v2 section 15)"
        )
    expected_finish = first.estimated_departure + timedelta(
        seconds=metrics.duration_sec
        - first.travel_time
        - first.waiting_time
        - first.service_duration
    )
    if metrics.finish_arrival != expected_finish:
        raise InvalidRoutePlanError(
            "estimated final arrival must follow from the first stop's departure and the rest of "
            "the route (v2 section 12)"
        )

    measured = metrics_of(evaluation)
    return FirstStopCandidate(
        stop_id=stop_id,
        # First-leg metrics (v2 section 12) - reported, never the ranking criterion.
        travel_time=first.travel_time,
        estimated_arrival=first.estimated_arrival,
        waiting_time=first.waiting_time,
        lateness=first.lateness,
        estimated_complete_route_duration=metrics.duration_sec,
        # Complete-route feasibility (v2 section 14, D32).
        feasible=not violating_stop_ids,
        service_window_start=first.service_window_start,
        score=objective,
        complete_travel_time=metrics.travel_sec,
        complete_waiting_time=metrics.waiting_sec,
        total_service_time=metrics.service_sec,
        estimated_finish=metrics.finish_arrival,
        estimated_service_start=first.service_start,
        violating_stop_ids=violating_stop_ids,
        max_lateness=max((timeline.lateness for timeline in evaluation.timelines), default=0),
        metrics=measured,
        explanation=(
            (CostComponent.TRAVEL_TIME.value, float(measured.travel_sec)),
            (CostComponent.WAITING_TIME.value, float(measured.waiting_sec)),
            (CostComponent.DISTANCE.value, float(measured.distance_m)),
        ),
    )
