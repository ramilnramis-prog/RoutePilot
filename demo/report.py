"""Complete-route demo report (Stage 2 unit U5; v2 sections 12-16, 20, 25, 30, 33, 35).

**DEMO / SYNTHETIC DATA.** Everything printed here comes from the invented stops of
:mod:`demo.dataset` and the synthetic travel matrix of :mod:`demo.synthetic_matrix`. It is not
real addresses, not real opening hours and not road routing, and it is labelled as such everywhere.

What the report shows, in the order the spec asks for it:

* the plan (departure, START, FINISH, enabled/disabled stops) and the engine's **status**;
* the **recommended first stop** with the complete-route metrics of v2 section 12;
* a **top-5 ranking by complete route outcome** - ``START -> candidate -> optimized remaining
  stops -> FINISH``, with the FINISH leg included (v2 section 15);
* the **complete route** the recommended candidate would produce, stop by stop;
* the **nearest** and the **farthest** candidate with their complete outcomes and their rank, and
  an explicit explanation of why the recommendation wins (v2 section 33);
* the **USER** baseline against the **OPTIMIZED** route, and the internal **ALGORITHM** baseline
  (v2 section 30, D22);
* the **departure-time sweep** 04:00-08:00, which shows that the recommendation changes;
* the **OBJECTIVE-ALIGNMENT** table (D35): per departure hour, the previous D31 provisional
  recommendation against the new elapsed-duration recommendation, with the new one's FINISH,
  complete travel, waiting, service and feasibility - the audit trail of the objective change;
* the **non-default sensitivity study** of a *non-zero* waiting preference (D31), computed over the
  same complete-route outcomes, including the degenerate 1:1 case;
* the **rejected candidates** with their violating stop ids (v2 section 14, D9);
* the **route fingerprint** and the **recommendation fingerprint**, and the difference between
  them once a first stop is selected (v2 section 7, D33);
* the deterministic work counters (candidates evaluated, optimizer runs, leg-cache hits/misses/
  entries), the measured ~30-stop evaluation runtime, and the **scale/performance block**: the
  ~30-stop demo plan, the **~50-enabled-stop portfolio fixture** (the primary MVP scale target of
  D36) and the **~100-stop stress reference**, which D36 declares future scale / not
  performance-qualified (v2 section 20, D34, D36).

The default objective is the **complete elapsed route duration** (D35): ``travel_time`` and
``waiting_time`` at 1:1 over the complete route, with a waiting preference of zero. Service time is
constant across the candidates of one plan and is reported, never scored, so the score equals
complete travel + waiting and equals the complete duration minus that constant. The historical
``demo_provisional_v1`` weights (travel 1, waiting 2) are **not** the default any more and are
printed only inside the labelled sensitivity study.

Two things are deliberate and worth stating up front:

1. **Nothing here is applied.** The engine *recommends*; the driver decides (D4/D32/I5). The plan
   stays in ``awaiting_first_stop_choice``, and the complete routes shown for a single candidate
   (the recommendation, the baselines, the fingerprint comparison) are previews computed from
   **copies** of the plan that carry a first-stop selection. The demo plan itself is never changed.
2. **The report is deterministic.** Everything except the two measured wall-clock lines is a pure
   function of the plan, the synthetic matrix and the configured cost policy, and identical inputs
   print an identical report. The wall-clock lines are only present when a caller measures them
   (:func:`main` does) and are marked as machine-dependent.

Run: ``python -m demo.report``
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core.engine.first_stop.evaluation import (
    FirstStopEvaluationReport,
    evaluate_first_stop_candidates,
)
from core.engine.optimizer.optimize import optimize
from core.engine.optimizer.route_evaluation import RouteEvaluation, evaluate_order
from core.engine.optimizer.route_fingerprint import route_fingerprint
from core.engine.optimizer.route_problem import build_problem
from core.model.cost_policy import (
    DEMO_TRAVEL_TIME_WEIGHT,
    DEMO_WAITING_TIME_WEIGHT,
    SMART_ROUTE_ELAPSED_POLICY_NAME,
    CostComponent,
    RouteCostPolicy,
    demo_provisional_policy,
    smart_route_elapsed_policy,
)
from core.model.first_stop import FirstStopCandidate, FirstStopIntent
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.time import tz, tzdata
from core.validation.errors import TZDATA_INSTALL_COMMAND
from demo.dataset import (
    DEMO_DEFAULT_SERVICE_DURATION,
    DEMO_SERVICE_DATE,
    DEMO_TIMEZONE,
    DEMO_WARNING,
    HEADLINE_STOP_IDS,
    build_demo_plan,
    demo_departure_time_at,
)
from demo.scale_dataset import (
    PORTFOLIO_DISABLED_STOP_COUNT,
    PORTFOLIO_ENABLED_STOP_COUNT,
    PORTFOLIO_STOP_COUNT,
    SCALE_DEFAULT_STOP_COUNT,
    SCALE_WARNING,
    build_portfolio_plan,
)
from demo.synthetic_matrix import DEMO_MATRIX_DISCLAIMER, demo_matrix

__all__ = [
    "BENCHMARK_COMMAND",
    "DEMO_EVALUATION_RUNTIME_BUDGET_SEC",
    "DEPARTURE_SWEEP_HOURS",
    "OWNER_SCALE_STATEMENT",
    "PORTFOLIO_ACCEPTABLE_BUDGET_SEC",
    "PORTFOLIO_BENCHMARK_COMMAND",
    "PORTFOLIO_PREFERRED_BUDGET_SEC",
    "PREVIOUS_DEFAULT_WAITING_WEIGHT",
    "RECORDED_BENCHMARK",
    "WEIGHT_SENSITIVITY_RATIOS",
    "BaselineComparison",
    "DepartureOutcome",
    "EvaluationTimings",
    "ObjectiveAlignmentRow",
    "RecordedBenchmark",
    "WeightSensitivityRow",
    "baseline_comparison",
    "build_report",
    "complete_travel_rank",
    "demo_evaluation",
    "departure_sweep",
    "fewest_driving_ids",
    "format_duration",
    "main",
    "objective_alignment",
    "objective_winner_sentence",
    "performance_scale_lines",
    "portfolio_evaluation",
    "previous_default_recommendation",
    "recommendation_preview",
    "rejected_candidate_lines",
    "weight_sensitivity",
]

#: Local departure hours shown in the departure-time sweep (v2 section 33).
DEPARTURE_SWEEP_HOURS = (4, 5, 6, 7, 8)

#: How many alternatives the ranking section shows (v2 section 13: "approximately 3-5").
TOP_K = 5

#: The waiting:travel ratios the **non-default** sensitivity study evaluates over the complete-route
#: objective (D31, superseded for the default objective by D35: the shipped default has a waiting
#: preference of zero). The 1:1 case is included because it is numerically the default objective
#: and shows how the deterministic ranking key - not the objective - decides there.
WEIGHT_SENSITIVITY_RATIOS = (1.0, 1.5, 2.0, 3.0)

#: The waiting weight of the **previous** D31 default (travel 1, waiting 2), kept so the
#: objective-alignment table can name exactly what it compares against.
PREVIOUS_DEFAULT_WAITING_WEIGHT = DEMO_WAITING_TIME_WEIGHT

#: v2 section 20's "acceptable for the early product" target for ~100 stops is 5 s. The demo plan has
#: 31 enabled stops and its exhaustive evaluation is measured around that boundary (see the printed
#: runtime); the runtime is **printed**, never asserted as a correctness rule (v2 section 20 calls
#: these engineering targets, not correctness rules).
DEMO_EVALUATION_RUNTIME_BUDGET_SEC = 5.0

#: The exact command that produces the ~100-stop stress figures below.
BENCHMARK_COMMAND = "python tools/benchmark_optimizer.py --stop-count 100"

#: The owner's Stage 2.1 scale decision, verbatim (D36), printed with the scale/performance block so
#: the numbers are read under the target that actually applies to them.
OWNER_SCALE_STATEMENT = (
    "Portfolio MVP performance target: ~50 stops. 100-stop exhaustive optimization is supported as "
    "an engineering stress scenario but is not yet performance-optimized."
)

#: The ~50-enabled-stop portfolio fixture is the **primary MVP scale target** (D36), so its
#: preferred / acceptable targets are the v2 section 20 pair: preferred <= ~3 s, acceptable <= ~5 s.
#: They are **reported**, never asserted as a correctness rule (v2 section 20 calls them engineering
#: targets), and the benchmark tool guards the measurement with a generous bound instead.
PORTFOLIO_PREFERRED_BUDGET_SEC = 3.0
PORTFOLIO_ACCEPTABLE_BUDGET_SEC = 5.0

#: The exact command that reproduces the scale/performance block's live portfolio measurement.
PORTFOLIO_BENCHMARK_COMMAND = "python tools/benchmark_optimizer.py"

_SEPARATOR = "=" * 112
_SUBSEPARATOR = "-" * 112


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def format_duration(seconds: int) -> str:
    """``14100`` -> ``3h55m``, ``2700`` -> ``45m``."""
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{sign}{hours}h{minutes:02d}m"
    return f"{sign}{minutes}m"


def _clock(instant: datetime | None, zone: ZoneInfo) -> str:
    if instant is None:
        return "-"
    return tz.to_local(instant, zone).strftime("%H:%M")


def _window_text(stop: RouteStop) -> str:
    window = stop.service_window
    if window.is_fixed:
        return window.describe()
    if window.window_kind.value == "unrestricted":
        return "always open"
    return "hours unknown"


def _policy_text(policy: RouteCostPolicy) -> str:
    weights = ", ".join(
        f"{component.value}={weight:g}"
        for component, weight in sorted(policy.weights.items(), key=lambda item: item[0].value)
    )
    marker = " - PROVISIONAL DEMO WEIGHTS, not product truth" if policy.provisional else ""
    return f"{policy.name} ({weights}){marker}"


def _weighted_sum_text(policy: RouteCostPolicy) -> str:
    """``travel_time x 1 + waiting_time x 1`` for the policy's own weighted components."""
    weighted = [
        f"{component.value} x {weight:g}"
        for component, weight in sorted(policy.weights.items(), key=lambda item: item[0].value)
    ]
    return " + ".join(weighted) if weighted else "no weights configured"


def _objective_note(policy: RouteCostPolicy) -> str:
    """What the configured objective is, said truthfully for the policy actually in force.

    The default (D35) is the complete elapsed route duration with a waiting preference of zero.
    A ``provisional`` policy is the non-default D31 sensitivity study and is labelled as such
    instead of being described as the shipped objective.
    """
    if policy.provisional:
        return (
            "NON-DEFAULT sensitivity policy, not the shipped objective (D31): the default "
            f"SMART_ROUTE objective is {SMART_ROUTE_ELAPSED_POLICY_NAME} - complete elapsed "
            "duration with a zero waiting preference (D35)"
        )
    return (
        "the default SMART_ROUTE objective (D35): the complete elapsed route duration. Service "
        "time is constant across the candidates of one plan and is reported, never scored, so at "
        "1:1 weights this score equals complete travel + waiting and equals the complete duration "
        "minus that constant - the elapsed-duration objective, not a hidden weight"
    )


def _distance_km(metres: float) -> str:
    return f"{metres / 1000.0:.1f}km"


def _performance_stop_count_line(
    active: tuple[RouteStop, ...], disabled: tuple[RouteStop, ...], plan: RoutePlan
) -> str:
    """The scale label above the performance counters: the **enabled** count and the total.

    Only enabled stops are candidates (D20), so a "31-stop demo plan" label on a plan that holds 32
    stops was wrong: the counters below it measure the 31 enabled stops. Both counts are printed,
    taken from the plan itself, so the label cannot disagree with the counters or with the plan
    shape printed above it. The counts are passed in rather than imported, so the helper states the
    shape of whatever plan the report was built for.
    """
    return f"  {len(active)} enabled stops ({len(plan.stops)} stops, {len(disabled)} disabled)"


@dataclass(frozen=True)
class CompositeDriving:
    """Where a candidate's **complete** driving sits among the candidates actually compared.

    ``rank`` counts the candidates whose complete route drives **strictly less**, so a tie is
    reported as the joint position it is ("2 candidates drive the same or less") instead of a
    fabricated sole rank. ``of`` is the size of the compared set and ``minimum``/``maximum`` are
    its extremes, so a sentence such as "least of the driving compared" is printed only when it is
    true of this run (reviewer finding: a stale superlative is a false claim).
    """

    rank: int
    of: int
    minimum: int
    maximum: int
    tied_ids: tuple[StopId, ...]


def _component(candidate: FirstStopCandidate, component: CostComponent) -> float:
    """The measured value of one objective component of a candidate (0.0 when not carried)."""
    for name, value in candidate.explanation:
        if name == component.value:
            return float(value)
    return 0.0


def _fewest(driving: CompositeDriving) -> str:
    """``1st``/``2nd``/``3rd``/``4th``... for a 1-based rank of the complete-driving comparison."""
    within_teens = driving.rank < 20
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(
        driving.rank if within_teens else driving.rank % 10, "th"
    )
    return f"{driving.rank}{suffix}"


def _driving_of(
    candidate: FirstStopCandidate, compared: Sequence[FirstStopCandidate]
) -> CompositeDriving:
    own = candidate.complete_travel_time
    less = [other for other in compared if other.complete_travel_time < own]
    tied = tuple(
        other.stop_id for other in compared if other.complete_travel_time == own
    )
    travels = [other.complete_travel_time for other in compared] or [own]
    return CompositeDriving(
        rank=len(less) + 1,
        of=len(compared),
        minimum=min(travels),
        maximum=max(travels),
        tied_ids=tied,
    )


def complete_travel_rank(
    report: FirstStopEvaluationReport, stop_id: StopId
) -> CompositeDriving | None:
    """Where one candidate's complete driving sits among the candidates the ranking compares.

    The compared set is the **ranked** candidates when there is a ranking - a rejected candidate has
    no complete route to compare fairly - and every evaluated candidate otherwise. The result is
    derived from the report's own numbers, so a printed superlative about "least driving" cannot go
    stale: it is computed, not remembered.
    """
    candidate = report.find(stop_id)
    if candidate is None:
        return None
    compared = report.ranked or tuple(report.rejected)
    if not compared:
        return None
    return _driving_of(candidate, compared)


def fewest_driving_ids(report: FirstStopEvaluationReport) -> tuple[StopId, ...]:
    """The ids of the candidates whose complete route drives the least (ties included), ascending."""
    compared = report.ranked or tuple(report.rejected)
    if not compared:
        return ()
    fewest = min(candidate.complete_travel_time for candidate in compared)
    return tuple(
        sorted(
            candidate.stop_id
            for candidate in compared
            if candidate.complete_travel_time == fewest
        )
    )


# --------------------------------------------------------------------------- #
# evaluation (memoized: the exhaustive loop costs seconds, and the report is a pure
# function of the plan, the matrix and the policy)
# --------------------------------------------------------------------------- #
_EVALUATION_CACHE: dict[str, FirstStopEvaluationReport] = {}
_SWEEP_CACHE: dict[tuple[str, tuple[int, ...]], tuple["DepartureOutcome", ...]] = {}
_PREVIEW_CACHE: dict[tuple[str, str], "RecommendationPreview"] = {}


def _plan_key(plan: RoutePlan) -> str:
    """The memo key of a plan: its recommendation fingerprint (v2 section 7).

    ``inputs_fingerprint`` covers everything a recommendation depends on - departure time and
    location, the stop set with its windows and durations, priorities, finish, cost policy and the
    timezone-data version - so two plans that differ in any way that can change an answer can never
    share a memo entry, and a variant plan used by a test can never receive the demo plan's result.
    """
    return plan.inputs_fingerprint()


def demo_evaluation(plan: RoutePlan) -> FirstStopEvaluationReport:
    """The exhaustive complete-route evaluation of one plan, memoized.

    The exhaustive loop of v2 section 20 costs several seconds at ~30 stops, so the report (and the
    tests that read it) evaluate each distinct plan once. The result is a pure function of the plan,
    the synthetic matrix and the configured policy, so memoizing it cannot change an answer.
    """
    key = _plan_key(plan)
    cached = _EVALUATION_CACHE.get(key)
    if cached is None:
        cached = evaluate_first_stop_candidates(plan=plan, travel_matrix=demo_matrix())
        _EVALUATION_CACHE[key] = cached
    return cached


def portfolio_evaluation() -> FirstStopEvaluationReport:
    """The exhaustive complete-route evaluation of the ~50-enabled-stop portfolio fixture (D36).

    Memoized through :func:`demo_evaluation`, so the report, the scale block and the tests share one
    evaluation and a repeated read is free. The plan comes from
    :func:`demo.scale_dataset.build_portfolio_plan`, which asserts on every call that the fixture
    still holds exactly :data:`demo.scale_dataset.PORTFOLIO_ENABLED_STOP_COUNT` enabled stops, so a
    "~50-stop" label can never outlive the plan it names.
    """
    return demo_evaluation(build_portfolio_plan())


# --------------------------------------------------------------------------- #
# the departure-time sweep
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DepartureOutcome:
    """One departure hour of the sweep: what the engine recommends, and its complete route."""

    local_hour: int
    departure: datetime
    status: str
    recommended_id: StopId | None
    first_leg: int | None
    complete_duration: int | None
    complete_waiting: int | None
    ranked: int
    rejected: int
    note: str = ""


def departure_sweep(
    *, plan: RoutePlan | None = None, hours: tuple[int, ...] = DEPARTURE_SWEEP_HOURS
) -> tuple[DepartureOutcome, ...]:
    """Evaluate one plan at several local departure hours (v2 section 33).

    Nothing is selected at any hour: each row is a **recommendation** for that departure time, and
    the row says so when the recommendation differs from the 04:00 one. ``plan`` defaults to the
    demo plan. Memoized per plan and hour tuple: the sweep costs one exhaustive evaluation per hour,
    and it is a pure function of the plan.
    """
    base_plan = build_demo_plan() if plan is None else plan
    key = (_plan_key(base_plan), hours)
    cached = _SWEEP_CACHE.get(key)
    if cached is not None:
        return cached
    outcomes = _run_sweep(base_plan, hours)
    _SWEEP_CACHE[key] = outcomes
    return outcomes


def _run_sweep(
    base_plan: RoutePlan, hours: tuple[int, ...]
) -> tuple[DepartureOutcome, ...]:
    outcomes: list[DepartureOutcome] = []
    baseline_report = demo_evaluation(
        dataclasses.replace(base_plan, departure_time=demo_departure_time_at(hours[0]))
    )
    baseline = baseline_report.recommended_stop_id
    previous: StopId | None = baseline
    previous_hour = hours[0]
    for hour in hours:
        plan = dataclasses.replace(
            base_plan, departure_time=demo_departure_time_at(hour)
        )
        report = demo_evaluation(plan)
        recommended = report.recommended()
        note = ""
        if recommended is None:
            note = "no fully feasible complete route at this departure time"
        elif hour != hours[0] and recommended.stop_id != previous:
            note = f"changed from {previous} at {previous_hour:02d}:00"
        outcomes.append(
            DepartureOutcome(
                local_hour=hour,
                departure=plan.departure_time,
                status=report.status.value,
                recommended_id=recommended.stop_id if recommended else None,
                first_leg=recommended.travel_time if recommended else None,
                complete_duration=(
                    recommended.estimated_complete_route_duration if recommended else None
                ),
                complete_waiting=(
                    recommended.complete_waiting_time if recommended else None
                ),
                ranked=len(report.ranked),
                rejected=len(report.rejected),
                note=note,
            )
        )
        if recommended is not None:
            previous = recommended.stop_id
            previous_hour = hour
    return tuple(outcomes)


# --------------------------------------------------------------------------- #
# the objective-alignment audit (D35)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ObjectiveAlignmentRow:
    """One departure hour of the objective-alignment audit (D35).

    ``previous_*`` is the recommendation under the **previous** default, the non-default
    provisional D31 policy (``travel_time = 1``, ``waiting_time = 2``); ``new_*`` is the
    recommendation under the new default, the complete elapsed route duration. A row therefore
    records exactly what the objective change did, and where it changed the answer it reports that
    instead of hiding it.
    """

    local_hour: int
    previous_recommended_id: StopId | None
    new_recommended_id: StopId | None
    finish: datetime | None
    complete_travel: int | None
    complete_waiting: int | None
    complete_service: int | None
    feasible: bool | None

    @property
    def changed(self) -> bool:
        return (
            self.previous_recommended_id is not None
            and self.new_recommended_id is not None
            and self.previous_recommended_id != self.new_recommended_id
        )


def previous_default_recommendation(
    report: FirstStopEvaluationReport, plan: RoutePlan
) -> FirstStopCandidate | None:
    """The recommendation the **pre-D35** default produced, reconstructed for the audit trail.

    Before D35 the ranking key was ``(score, complete duration, input_position, stop_id)`` and the
    default policy was the D31 provisional ``demo_provisional_v1`` (travel 1, waiting 2). This
    helper applies exactly that historical key to the provisional policy's own ranked candidates,
    so the objective-alignment table compares like with like: what the previous default would have
    recommended at that departure hour against what the new default recommends now.

    It ranks nothing the product ships - the shipped ranking is
    :func:`core.engine.first_stop.evaluation.ranking_key` (D35) - and it exists only so the audit
    table is not a comparison of the new key against itself. Rejected candidates are already absent
    from ``report.ranked``, so the reconstruction can never rank an infeasible route.
    """
    if not report.ranked:
        return None
    return min(
        report.ranked,
        key=lambda candidate: (
            candidate.score,
            candidate.estimated_complete_route_duration,
            plan.stop_by_id(candidate.stop_id).input_position,
            candidate.stop_id,
        ),
    )


def objective_alignment(
    *, plan: RoutePlan | None = None, hours: tuple[int, ...] = DEPARTURE_SWEEP_HOURS
) -> tuple[ObjectiveAlignmentRow, ...]:
    """The audit trail of the D35 objective change, per departure hour.

    For every hour the **same** exhaustive candidate set is evaluated twice: once under the
    previous D31 default (``demo_provisional_policy()``, travel 1 / waiting 2, ranked with the
    pre-D35 key via :func:`previous_default_recommendation`) and once under the new default
    :func:`core.model.cost_policy.smart_route_elapsed_policy` (complete elapsed route duration,
    ranked with the D35 key). The new row carries its FINISH instant, complete travel, complete
    waiting, total service and feasibility, so the change is auditable rather than asserted.

    Nothing is tuned and nothing is preserved: the new winner may legitimately differ from the
    previous one, and the table reports that. Both evaluations are memoized like every other
    evaluation of the module, so the table is a pure function of the plan and the two policies.
    """
    base_plan = build_demo_plan() if plan is None else plan
    rows: list[ObjectiveAlignmentRow] = []
    for hour in hours:
        departure = demo_departure_time_at(hour)
        previous = previous_default_recommendation(
            demo_evaluation(
                dataclasses.replace(
                    base_plan,
                    departure_time=departure,
                    cost_policy=demo_provisional_policy(),
                )
            ),
            base_plan,
        )
        current = demo_evaluation(
            dataclasses.replace(
                base_plan,
                departure_time=departure,
                cost_policy=smart_route_elapsed_policy(),
            )
        ).recommended()
        rows.append(
            ObjectiveAlignmentRow(
                local_hour=hour,
                previous_recommended_id=previous.stop_id if previous is not None else None,
                new_recommended_id=current.stop_id if current is not None else None,
                finish=current.estimated_finish if current is not None else None,
                complete_travel=current.complete_travel_time if current is not None else None,
                complete_waiting=current.complete_waiting_time if current is not None else None,
                complete_service=current.total_service_time if current is not None else None,
                feasible=current.feasible if current is not None else None,
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------- #
# the recommendation's own complete route and the baselines (preview only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecommendationPreview:
    """The complete route the recommended candidate would produce, plus the two baselines.

    Everything here is computed from a **copy** of the demo plan that carries a first-stop
    selection, so the committed-route machinery (which requires the driver's decision, I4/D32) can
    be exercised without the demo applying anything. The demo plan itself is never changed.
    """

    selected_plan: RoutePlan
    stop_id: StopId
    evaluation: RouteEvaluation
    algorithm_evaluation: RouteEvaluation
    user_evaluation: RouteEvaluation
    order: tuple[StopId, ...]
    seed_objective: int
    final_objective: int
    accepted_moves: int
    search_evaluations: int
    screened_moves: int
    budget_exhausted: bool
    route_fingerprint: str


@dataclass(frozen=True)
class BaselineComparison:
    """USER vs OPTIMIZED vs ALGORITHM, with what the optimization saved (v2 section 30)."""

    user: RouteEvaluation
    optimized: RouteEvaluation
    algorithm: RouteEvaluation

    @property
    def saved_time(self) -> int:
        return self.user.metrics.duration_sec - self.optimized.metrics.duration_sec

    @property
    def saved_distance(self) -> float:
        return self.user.metrics.distance_m - self.optimized.metrics.distance_m

    @property
    def saved_waiting(self) -> int:
        return self.user.metrics.waiting_sec - self.optimized.metrics.waiting_sec


def recommendation_preview(plan: RoutePlan, stop_id: StopId) -> RecommendationPreview:
    """Preview the complete route of one candidate, with both baselines, on a plan copy.

    Memoized: the preview runs the optimizer once, and it is a pure function of the plan and the
    chosen candidate (the demo plan is never mutated).
    """
    key = (_plan_key(plan), stop_id)
    cached = _PREVIEW_CACHE.get(key)
    if cached is not None:
        return cached
    preview = _build_preview(plan, stop_id)
    _PREVIEW_CACHE[key] = preview
    return preview


def _build_preview(plan: RoutePlan, stop_id: StopId) -> RecommendationPreview:
    selected_plan = dataclasses.replace(
        plan, first_service_stop=FirstStopIntent.manual_choice(stop_id)
    )
    problem = build_problem(
        plan=selected_plan, travel_matrix=demo_matrix(), first_stop_id=stop_id
    )
    optimized = optimize(problem)
    matrix = problem.legs
    user_evaluation = evaluate_order(
        plan=selected_plan,
        travel_matrix=matrix,
        order=selected_plan.user_baseline_order(),
    )
    return RecommendationPreview(
        selected_plan=selected_plan,
        stop_id=stop_id,
        evaluation=optimized.evaluation,
        algorithm_evaluation=optimized.algorithm_evaluation,
        user_evaluation=user_evaluation,
        order=optimized.order,
        seed_objective=optimized.local_search.seed_objective,
        final_objective=optimized.local_search.final_objective,
        accepted_moves=len(optimized.local_search.accepted_moves),
        search_evaluations=optimized.local_search.evaluations,
        screened_moves=optimized.local_search.screened_moves,
        budget_exhausted=optimized.local_search.budget_exhausted,
        route_fingerprint=route_fingerprint(selected_plan, optimized.order),
    )


def baseline_comparison(preview: RecommendationPreview) -> BaselineComparison:
    """The three baselines of one previewed route (v2 section 30, D22)."""
    return BaselineComparison(
        user=preview.user_evaluation,
        optimized=preview.evaluation,
        algorithm=preview.algorithm_evaluation,
    )


# --------------------------------------------------------------------------- #
# sensitivity study of a NON-ZERO waiting preference (D31, non-default since D35)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WeightSensitivityRow:
    """What the engine recommends at 04:00 under one waiting weight (D31, non-default).

    The row is computed over the **complete-route** outcomes of that policy, so it shows the
    sensitivity of the objective to its waiting weight - not a leftover first-leg view. Since D35
    the default objective has a waiting preference of zero, so this is a labelled non-default
    study; ``recommended_id`` is the shipped engine's answer under that policy, i.e. ranked with
    the D35 key (complete elapsed duration first), and ``note`` says when the weighted objective
    alone would have picked a different stop.
    """

    waiting_weight: float
    travel_weight: float
    recommended_id: StopId | None
    recommended_score: float
    complete_duration: int
    complete_waiting: int
    tied_with_recommendation: int
    note: str


def weight_sensitivity(
    *,
    plan: RoutePlan | None = None,
    ratios: tuple[float, ...] = WEIGHT_SENSITIVITY_RATIOS,
    travel_weight: float = DEMO_TRAVEL_TIME_WEIGHT,
) -> tuple[WeightSensitivityRow, ...]:
    """The recommended first stop at 04:00 for several waiting weights (D31, non-default).

    ``demo_provisional_policy`` is **not** the shipped default any more (D35 makes complete elapsed
    duration the default with a waiting preference of zero), so this is a labelled non-default
    study: each ratio re-evaluates the **same** complete candidate set under a policy
    ``travel_weight = 1``, ``waiting_time = ratio``. The 1:1 case is included; its row is
    numerically the default objective, and it says how many candidates tie at the winning score and
    which deterministic ranking key broke the tie, because the tie-break - not the objective -
    decides the answer there.

    ``recommended_id`` is the shipped engine's answer under that policy, so it is ordered by the
    D35 key (complete elapsed duration first). Where the weighted objective alone would have
    preferred another stop, the row's ``note`` says so explicitly: that is exactly what a non-zero
    waiting preference would do, and it is reported rather than hidden. Nothing is tuned: the
    ratios are fixed inputs of the report, and the payload plan is never mutated.
    """
    base_plan = build_demo_plan() if plan is None else plan
    rows: list[WeightSensitivityRow] = []
    for ratio in ratios:
        policy = demo_provisional_policy(
            travel_time_weight=travel_weight, waiting_time_weight=ratio
        )
        evaluated = demo_evaluation(dataclasses.replace(base_plan, cost_policy=policy))
        recommended = evaluated.recommended()
        ties = (
            [
                candidate
                for candidate in evaluated.ranked
                if abs(candidate.score - recommended.score) < 1e-9
            ]
            if recommended is not None
            else []
        )
        objective_best = (
            min(
                evaluated.ranked,
                key=lambda candidate: (
                    candidate.score,
                    candidate.estimated_complete_route_duration,
                    candidate.stop_id,
                ),
            )
            if evaluated.ranked
            else None
        )
        if recommended is None:
            note = "no fully feasible complete route under this weight"
        elif len(ties) > 1:
            note = (
                f"DEGENERATE {ratio:g}:1 case: {len(ties)} candidates tie at "
                f"{recommended.score:.0f}; the deterministic ranking key (complete elapsed "
                "duration, complete travel time, complete waiting time, input_position, stop_id) "
                "breaks the tie (D35)"
            )
        elif objective_best is not None and objective_best.stop_id != recommended.stop_id:
            note = (
                f"a NON-ZERO waiting preference would make the objective pick "
                f"{objective_best.stop_id} ({objective_best.score:.0f} vs "
                f"{recommended.score:.0f}), but the shipped ranking key puts complete elapsed "
                f"duration first (D35), so the recommendation stays {recommended.stop_id}"
            )
        else:
            note = (
                "the weighted objective and the shipped ranking key pick the same stop under this "
                "preference"
            )
        rows.append(
            WeightSensitivityRow(
                waiting_weight=ratio,
                travel_weight=travel_weight,
                recommended_id=recommended.stop_id if recommended else None,
                recommended_score=recommended.score if recommended else 0.0,
                complete_duration=(
                    recommended.estimated_complete_route_duration if recommended else 0
                ),
                complete_waiting=recommended.complete_waiting_time if recommended else 0,
                tied_with_recommendation=len(ties),
                note=note,
            )
        )
    return tuple(rows)


# --------------------------------------------------------------------------- #
# rejected candidates (v2 section 14, D9)
# --------------------------------------------------------------------------- #
def rejected_candidate_lines(report: FirstStopEvaluationReport) -> tuple[str, ...]:
    """One line per rejected candidate: its own id and the stops that violate its complete route.

    Grouped **by candidate**, because that is the question a driver asks ("what went wrong when I
    start there?"). Each rejected candidate carries its violating stop ids explicitly, so an
    infeasible candidate is never presented as a valid route (v2 section 14).
    """
    lines: list[str] = []
    for candidate in report.rejected:
        reasons = report.reasons_for(candidate.stop_id)
        violating = ", ".join(candidate.violating_stop_ids) or "-"
        detail = "; ".join(reason.reason for reason in reasons) or (
            "complete route violates a hard service window"
        )
        lines.append(
            f"REJECTED {candidate.stop_id:<22} complete duration "
            f"{format_duration(candidate.estimated_complete_route_duration):>7}  "
            f"violating stops: {violating}\n"
            f"         {detail}"
        )
    return tuple(lines)


# --------------------------------------------------------------------------- #
# the objective superlative: it must name the ranked set it is true of
# --------------------------------------------------------------------------- #
def objective_winner_sentence(report: FirstStopEvaluationReport) -> str:
    """The "why the recommendation wins" claim, computed from the ranking it is about.

    The primary criterion is the **minimum complete elapsed duration** (D35): the default
    SMART_ROUTE objective is the complete elapsed route duration (travel + waiting + service,
    equivalently the FINISH arrival time for a fixed departure), so the sentence states that
    duration first and shows the complete travel, the complete waiting and the total service time
    as the measured decomposition that produces it. The weighted **objective** is printed last as
    the supporting consequence it is, not as the reason: with the shipped 1:1 weights it equals
    travel + waiting, i.e. the complete duration minus the constant service time (D35).

    The objective is only defined over the **fully feasible ranked** candidates: a rejected
    candidate is never ranked and its infeasible score is not comparable with a feasible one (v2
    section 14, D9). So the sentence names that ranked set and the rejected count explicitly, and
    it is derived from the report's own ``ranked``/``rejected`` sets - it cannot be read as ranking
    an infeasible route, and it cannot go stale with the fixture.

    The claimed minimum is **printed**, not merely asserted in prose: it is the recommended
    candidate's complete elapsed duration and the ``min`` of the ranked candidates' scores, so a
    reader can check the claim against the ranking the sentence names instead of having to take the
    word "lowest" on trust.
    """
    recommended = report.recommended()
    lowest_ranked_score = min(candidate.score for candidate in report.ranked)
    if recommended is None:  # pragma: no cover - a ranking exists whenever this sentence is printed
        return (
            f"- the minimum complete elapsed duration over the {len(report.ranked)} ranked fully "
            f"feasible candidates is undefined, and the lowest ranked objective is "
            f"{lowest_ranked_score:.0f} "
            f"({len(report.rejected)} of the {report.candidates_evaluated} evaluated are REJECTED, "
            "never ranked and carry no comparable score)."
        )
    return (
        f"- the minimum complete elapsed duration of the {len(report.ranked)} ranked fully feasible "
        f"candidates is {format_duration(recommended.estimated_complete_route_duration)} "
        f"({recommended.estimated_complete_route_duration}s) - complete travel "
        f"{format_duration(recommended.complete_travel_time)}, complete waiting "
        f"{format_duration(recommended.complete_waiting_time)} and service "
        f"{format_duration(recommended.total_service_time)} - which with the shipped 1:1 weights is "
        f"the lowest objective of {lowest_ranked_score:.0f} "
        f"({len(report.rejected)} of the {report.candidates_evaluated} evaluated are REJECTED, "
        "never ranked and carry no comparable score)."
    )


# --------------------------------------------------------------------------- #
# the recorded ~100-stop benchmark
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecordedBenchmark:
    """A previous, clearly labelled measurement of the ~100-stop stress fixture.

    The report does **not** re-run the ~100-stop benchmark: it takes minutes, and pretending a
    recorded figure is a live measurement would be exactly the kind of claim this project refuses.
    The command is printed next to the numbers, with the machine note, so anyone can reproduce them
    (v2 section 20, D34).

    Since D36 this is the **engineering stress reference**, not a performance-qualified MVP scale: the
    recorded figure keeps reporting its honest measured number and the owner-accepted bound, and the
    section is labelled as future scale / not performance-qualified rather than as a missed MVP gate.
    """

    measured_on: str
    stop_count: int
    candidates: int
    total_seconds: float
    seconds_per_candidate: float
    route_evaluations: int
    accepted_moves: int
    cache_hits: int
    cache_misses: int
    cache_entries: int
    candidates_at_ceiling: int
    accepted_interim_limit_sec: float
    spec_target_met: bool


#: The last measurement taken on this development machine with
#: ``python tools/benchmark_optimizer.py`` (2026-09-11, after Stage 2.2 U7). Wall-clock figures are
#: machine-dependent; every other number is deterministic (the benchmark proves it by repeating
#: the loop and comparing the counters). Under D36 this fixture is the stress reference: the figure
#: is reported honestly and is **not** an MVP performance gate. U7's exact incremental evaluator
#: took the figure this fixture recorded before it (~74.8 s warm, measured on the same machine with
#: the reference full-evaluation path) down by about 2.5x at this ~100-stop stress scale; the speedup
#: is not flat across scales - the portfolio scale measured about 2.5x and the ~30-stop demo plan
#: about 2.2x (D37).
RECORDED_BENCHMARK = RecordedBenchmark(
    measured_on="2026-09-11 (this development machine, after U7, `tools/benchmark_optimizer.py`)",
    stop_count=97,
    candidates=97,
    total_seconds=29.88,
    seconds_per_candidate=0.308,
    route_evaluations=1_940_000,
    accepted_moves=97,
    cache_hits=1_957_848,
    cache_misses=0,
    cache_entries=9_801,
    candidates_at_ceiling=97,
    accepted_interim_limit_sec=150.0,
    spec_target_met=False,
)


# --------------------------------------------------------------------------- #
# the measured runtimes of this report
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvaluationTimings:
    """Wall-clock measurements taken by :func:`main` (machine-dependent, never part of the report).

    ``demo_evaluation``, each sweep row and the portfolio evaluation are memoized, so a timing is
    recorded only when that evaluation was actually computed in this process. ``portfolio_seconds``
    is the ~50-enabled-stop portfolio fixture's exhaustive loop - the primary MVP scale target
    (D36) - measured by :func:`main` itself; it is ``None`` in a call that did not measure it, and
    the scale block then says so instead of inventing a number.
    """

    demo_seconds: float | None
    sweep_seconds: float | None
    portfolio_seconds: float | None = None


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
def _candidate_row(
    position: int | str,
    candidate: FirstStopCandidate,
    zone: ZoneInfo,
    stop: RouteStop,
) -> str:
    return (
        f"{position:>3}  {candidate.stop_id:<22} "
        f"{format_duration(candidate.travel_time):>7}  "
        f"{_clock(candidate.estimated_arrival, zone):>5}  "
        f"{_window_text(stop):<15}  "
        f"{format_duration(candidate.waiting_time):>7}  "
        f"{_clock(candidate.estimated_service_start, zone):>5}  "
        f"{format_duration(candidate.complete_travel_time):>7}  "
        f"{format_duration(candidate.complete_waiting_time):>7}  "
        f"{format_duration(candidate.total_service_time):>7}  "
        f"{format_duration(candidate.estimated_complete_route_duration):>7}  "
        f"{_clock(candidate.estimated_finish, zone):>6}  "
        f"{_component(candidate, CostComponent.TRAVEL_TIME):>9.0f}  "
        f"{_component(candidate, CostComponent.WAITING_TIME):>8.0f}  "
        f"{candidate.score:>9.0f}"
    )


def _candidate_header() -> str:
    return (
        f"{'#':>3}  {'stop':<22} {'1st leg':>7}  {'ETA':>5}  {'window':<15}  "
        f"{'wait':>7}  {'start':>5}  {'c.trav':>7}  {'c.wait':>7}  {'c.svc':>7}  "
        f"{'c.dur':>7}  {'FINISH':>6}  {'travel*w':>9}  {'wait*w':>8}  {'score':>9}"
    )


def _role_candidate(
    report: FirstStopEvaluationReport, role: str
) -> FirstStopCandidate | None:
    return report.find(HEADLINE_STOP_IDS[role])


def _candidate_complete_summary(
    candidate: FirstStopCandidate, zone: ZoneInfo, rank: int | None, ranked_total: int
) -> str:
    rank_text = f"rank #{rank} of {ranked_total}" if rank else "REJECTED"
    return (
        f"{candidate.stop_id:<22} {rank_text:<15} "
        f"first leg {format_duration(candidate.travel_time):>7} "
        f"wait {format_duration(candidate.waiting_time):>7} | "
        f"complete: travel {format_duration(candidate.complete_travel_time):>7} "
        f"waiting {format_duration(candidate.complete_waiting_time):>7} "
        f"service {format_duration(candidate.total_service_time):>7} "
        f"duration {format_duration(candidate.estimated_complete_route_duration):>7} "
        f"FINISH {_clock(candidate.estimated_finish, zone):>5} "
        f"score {candidate.score:>9.0f}"
    )


def build_report(
    *, plan: RoutePlan | None = None, timings: EvaluationTimings | None = None
) -> str:
    """The whole deterministic demo report as text.

    ``plan`` defaults to the demo plan; passing another plan renders the same sections for it (which
    is how the fully infeasible case is covered). ``timings`` only adds the two measured wall-clock
    lines. Leaving it out keeps the output a pure function of the inputs, which is what the
    determinism test compares.
    """
    zone = tzdata.load_timezone(DEMO_TIMEZONE)
    plan = build_demo_plan() if plan is None else plan
    report = demo_evaluation(plan)
    active = plan.active_stops()
    disabled = plan.disabled_stops()
    fixed_08 = [
        stop
        for stop in active
        if stop.service_window.is_fixed
        and stop.service_window.describe().startswith("08:00")
    ]

    lines: list[str] = []
    lines.append(_SEPARATOR)
    lines.append("RoutePilot demo - COMPLETE-ROUTE first-stop recommendation - DEMO / SYNTHETIC DATA")
    lines.append(DEMO_WARNING)
    lines.append(DEMO_MATRIX_DISCLAIMER)
    lines.append(
        "Contract: PRODUCT_SPEC_v2 sections 12-15 (a complete route is START -> candidate -> "
        "optimized remaining stops -> FINISH, FINISH leg included), 20 (exhaustive candidates), "
        "25/30 (baselines), 33 (what the demo must prove), 35 (fingerprints); decisions D4/D32 "
        "(recommendation is not selection), D22 (baselines), D29 (window end policy), D34 "
        "(interim ~100-stop latency), D35 (the default objective is complete elapsed duration and "
        "the ranking key is the owner's 5-tuple)."
    )
    lines.append(_SEPARATOR)
    lines.append("")

    # ---- plan ----------------------------------------------------------- #
    lines.append("PLAN")
    lines.append(f"  plan              : {plan.id}")
    lines.append(
        f"  time zone         : {plan.timezone} (service date {DEMO_SERVICE_DATE.isoformat()})"
    )
    lines.append(
        f"  departure         : {_clock(plan.departure_time, zone)} local "
        f"({plan.departure_time.isoformat()}) from {plan.departure.label}"
    )
    lines.append(
        f"  START             : {plan.departure.label} - where driving begins, never a service "
        "stop (I1)"
    )
    lines.append(
        f"  FINISH            : {plan.finish.label} - fixed, never reordered, its leg IS part of "
        "every complete route (I2, v2 section 15)"
    )
    lines.append(
        f"  stops             : {len(plan.stops)} total, {len(active)} enabled, "
        f"{len(disabled)} disabled, {len(fixed_08)} opening at 08:00"
    )
    lines.append(
        f"  disabled stops    : {', '.join(str(stop.id) for stop in disabled) or '-'} "
        "(excluded from optimization, never candidates - D20; they keep their input_position)"
    )
    lines.append(
        f"  default service   : {format_duration(DEMO_DEFAULT_SERVICE_DURATION)} per stop when a "
        "stop has no duration of its own"
    )
    lines.append(
        f"  window end policy : {plan.window_end_policy.value} (plan default, D29) - service must "
        "FINISH before the closing time"
    )
    lines.append(
        f"  first-stop state  : {plan.first_stop_state.value} - a recommendation is not a "
        "selection; nothing is applied automatically (D4/D32/I5)"
    )
    lines.append(f"  cost policy       : {_policy_text(plan.cost_policy)}")
    lines.append(
        f"  objective         : {_weighted_sum_text(plan.cost_policy)} over the COMPLETE route's "
        "measured breakdown (travel, waiting; the distance weight is 0 and service time is "
        "identical for every candidate, so it is reported, never scored) - "
        + _objective_note(plan.cost_policy)
    )
    lines.append(
        "  ranking key       : (1) complete elapsed duration, (2) complete travel time, "
        "(3) complete waiting time, (4) input_position, (5) stop_id - the owner's deterministic "
        "5-tuple (D35); the weighted score is a reported figure, never a ranking key"
    )
    lines.append("")

    # ---- status --------------------------------------------------------- #
    lines.append("STATUS AND WORK")
    lines.append(
        f"  status            : {report.describe_status()}"
    )
    lines.append(
        f"  candidate set     : {report.candidates_evaluated} evaluated = "
        f"{len(report.ranked)} fully feasible (ranked) + {len(report.rejected)} rejected "
        "(exhaustive: one optimizer run per enabled stop, no prefilter, no shortlist - v2 section "
        "20, D34)"
    )
    lines.append(
        f"  optimizer runs    : {report.optimizer_runs} (one complete-route optimization per "
        "candidate)"
    )
    lines.append(
        f"  leg cache         : {report.cache_stats.hits} hits, {report.cache_stats.misses} "
        f"misses, {report.cache_stats.entries} entries "
        f"({report.cache_stats.lookups} leg questions)"
    )
    lines.append(
        f"  fingerprints      : recommendation {report.inputs_fingerprint} (decision-independent, "
        "v2 section 7)"
    )
    if timings is not None:
        demo_seconds = (
            "not measured in this process (result memoized from an earlier call)"
            if timings.demo_seconds is None
            else f"{timings.demo_seconds:.2f}s"
        )
        sweep_seconds = (
            "not measured in this process"
            if timings.sweep_seconds is None
            else f"{timings.sweep_seconds:.2f}s"
        )
        lines.append(
            f"  ~30-stop runtime  : exhaustive evaluation {demo_seconds} (v2 section 20 acceptable "
            f"target <= {DEMO_EVALUATION_RUNTIME_BUDGET_SEC:.0f}s, reported not asserted); "
            f"departure sweep {sweep_seconds}"
        )
        lines.append(
            "                      MEASURED WALL CLOCK, machine-dependent - the only non-"
            "deterministic lines in this report"
        )
    else:
        lines.append(
            "  ~30-stop runtime  : not measured in this call; run "
            "`python -m demo.report` to measure it against the v2 section 20 acceptable target "
            f"(<= {DEMO_EVALUATION_RUNTIME_BUDGET_SEC:.0f}s, reported not asserted)"
        )
    lines.append("")

    # ---- the recommended first stop ------------------------------------- #
    recommended = report.recommended()
    lines.append("RECOMMENDED FIRST STOP")
    if recommended is None:
        lines.append(
            f"  none: no candidate produced a fully feasible complete route "
            f"(status {report.status.value}) - an infeasible candidate is never presented as a "
            "valid route (v2 section 14)"
        )
        lines.append("")
        lines.append("REJECTED / INFEASIBLE CANDIDATES (v2 section 14, D9)")
        lines.extend(
            rejected_candidate_lines(report)
            or ["  none recorded, which cannot happen while the status is not 'recommended'"]
        )
        lines.append("")
        lines.append("SCALE AND PERFORMANCE (v2 section 20, D34, D36)")
        lines.append(
            "  The owner's Stage 2.1 scale decision (D36), verbatim: "
            f"\"{OWNER_SCALE_STATEMENT}\""
        )
        scale_lines, _ = performance_scale_lines(active, disabled, plan, report, timings=timings)
        lines.extend(scale_lines)
        lines.append("")
        return "\n".join(_closing_lines(lines, report))

    recommended_stop = plan.stop_by_id(recommended.stop_id)
    lines.append(
        f"  stop              : "
        f"{_candidate_complete_summary(recommended, zone, 1, len(report.ranked))}"
    )
    lines.append(
        f"  address           : {recommended_stop.raw_address} ({_window_text(recommended_stop)})"
    )
    lines.append(
        f"  first-leg detail  : travel {format_duration(recommended.travel_time)}, ETA "
        f"{_clock(recommended.estimated_arrival, zone)}, opens "
        f"{_clock(recommended.service_window_start, zone)}, waiting "
        f"{format_duration(recommended.waiting_time)}, service start "
        f"{_clock(recommended.estimated_service_start, zone)}"
    )
    lines.append(
        "  objective          : "
        + " + ".join(
            f"{component.value} {_component(recommended, component):.0f} x {weight:g}"
            for component, weight in sorted(
                plan.cost_policy.weights.items(), key=lambda item: item[0].value
            )
        )
        + f" = {recommended.score:.0f} - "
        + _objective_note(plan.cost_policy)
    )
    lines.append(
        "  hard windows      : "
        + (
            "none violated - the complete route is fully feasible (v2 section 14)"
            if recommended.feasible
            else "VIOLATED at " + ", ".join(recommended.violating_stop_ids)
        )
    )
    lines.append("")

    # ---- top-K ---------------------------------------------------------- #
    lines.append(
        f"TOP {TOP_K} CANDIDATES BY COMPLETE ROUTE OUTCOME (v2 sections 12/13; the FINISH leg is "
        "included in every c.* figure)"
    )
    lines.append(_candidate_header())
    for position, candidate in enumerate(report.top(TOP_K), start=1):
        lines.append(
            _candidate_row(position, candidate, zone, plan.stop_by_id(candidate.stop_id))
        )
    lines.append(
        "  ranking key       : (complete elapsed duration, complete travel time, complete waiting "
        "time, input_position, stop_id) - the owner's deterministic 5-tuple from the complete-route "
        "metrics, FINISH leg included, with no weighted score in it (D35); a tie therefore cannot "
        "hand the recommendation to an arbitrary stop (v2 section 30, D33)"
    )
    lines.append("")

    # ---- the recommendation's complete route ---------------------------- #
    preview = recommendation_preview(plan, recommended.stop_id)
    lines.append(
        f"COMPLETE ROUTE THE RECOMMENDATION WOULD PRODUCE - {recommended.stop_id} "
        "(PREVIEW ONLY: nothing is committed, the driver has not chosen - D4/D32)"
    )
    lines.append(
        f"{'#':>3}  {'stop':<22} {'arrive':>6}  {'opens':>5}  {'wait':>7}  {'start':>5}  "
        f"{'svc':>5}  {'depart':>6}  {'window':<15}  {'late':>5}"
    )
    for position, timeline in enumerate(preview.evaluation.timelines, start=1):
        stop = plan.stop_by_id(timeline.stop_id)
        lines.append(
            f"{position:>3}  {timeline.stop_id:<22} "
            f"{_clock(timeline.estimated_arrival, zone):>6}  "
            f"{_clock(timeline.service_window_start, zone):>5}  "
            f"{format_duration(timeline.waiting_time):>7}  "
            f"{_clock(timeline.service_start, zone):>5}  "
            f"{format_duration(timeline.service_duration):>5}  "
            f"{_clock(timeline.estimated_departure, zone):>6}  "
            f"{_window_text(stop):<15}  "
            f"{format_duration(timeline.lateness):>5}"
        )
    lines.append(
        f"    {'FINISH':<22} {_clock(preview.evaluation.metrics.finish_arrival, zone):>6}"
        f"   complete duration "
        f"{format_duration(preview.evaluation.metrics.duration_sec)}"
    )
    lines.append(
        f"  complete route    : {len(preview.order)} stops, distance "
        f"{_distance_km(preview.evaluation.metrics.distance_m)}, violations "
        f"{len(preview.evaluation.violations)}"
    )
    lines.append("")

    # ---- nearest / farthest / why --------------------------------------- #
    nearest = _role_candidate(report, "nearest")
    farthest = _role_candidate(report, "farthest")
    lines.append(
        "NEAREST vs FARTHEST vs THE RECOMMENDATION - COMPLETE OUTCOMES (v2 section 33)"
    )
    for label, candidate in (
        ("nearest      ", nearest),
        ("recommended  ", recommended),
        ("farthest     ", farthest),
    ):
        if candidate is None:  # pragma: no cover - the fixture always contains both roles
            continue
        lines.append(
            f"  {label} "
            f"{_candidate_complete_summary(candidate, zone, report.rank_of(candidate.stop_id), len(report.ranked))}"
        )
    lines.append("")
    if nearest is not None and farthest is not None:
        nearest_driving = complete_travel_rank(report, nearest.stop_id)
        farthest_driving = complete_travel_rank(report, farthest.stop_id)
        assert nearest_driving is not None and farthest_driving is not None
        lines.append(
            f"  why the recommendation wins : {recommended.stop_id} reaches the first customer "
            f"after {format_duration(recommended.travel_time)} of driving and waits "
            f"{format_duration(recommended.waiting_time)}; its complete route drives "
            f"{format_duration(recommended.complete_travel_time)}, waits "
            f"{format_duration(recommended.complete_waiting_time)} and finishes in "
            f"{format_duration(recommended.estimated_complete_route_duration)} "
            f"{objective_winner_sentence(report)}"
        )
        lines.append(
            f"  why the nearest loses      : {nearest.stop_id} is only "
            f"{format_duration(nearest.travel_time)} away and drives "
            f"{format_duration(nearest.complete_travel_time)} on its complete route - the "
            f"{_fewest(nearest_driving)}-least complete driving of the {nearest_driving.of} ranked "
            f"candidates (least {format_duration(nearest_driving.minimum)}, most "
            f"{format_duration(nearest_driving.maximum)}) - but starting there means waiting "
            f"{format_duration(nearest.waiting_time)} before the customer opens, so the complete "
            f"route waits {format_duration(nearest.complete_waiting_time)} and scores "
            f"{nearest.score:.0f}. The nearest first leg is the cheapest and the complete route is "
            "still one of the worst: complete-route quality decides, not the first leg."
        )
        if farthest.feasible:
            lines.append(
                f"  why the farthest loses      : {farthest.stop_id} is "
                f"{format_duration(farthest.travel_time)} away and opens at "
                f"{_clock(farthest.service_window_start, zone)}; arriving there costs "
                f"{format_duration(farthest.waiting_time)} of waiting, its complete route drives "
                f"{format_duration(farthest.complete_travel_time)} and waits "
                f"{format_duration(farthest.complete_waiting_time)}, and it scores "
                f"{farthest.score:.0f} - rank #{report.rank_of(farthest.stop_id)} of "
                f"{len(report.ranked)}. Driving further is not automatically better either."
            )
        else:
            lines.append(
                f"  why the farthest loses      : {farthest.stop_id} is "
                f"{format_duration(farthest.travel_time)} away and opens at "
                f"{_clock(farthest.service_window_start, zone)}; its complete route could not be "
                f"served inside the hard windows at all - it is REJECTED, never ranked, with "
                f"violating stops {', '.join(farthest.violating_stop_ids) or '-'} (v2 section 14). "
                "Driving further is not automatically better, and an infeasible route is never "
                "presented as a valid one."
            )
        lines.append(
            f"  why it is not intermediate : {recommended.stop_id} wins on the complete route it "
            f"produces - {format_duration(recommended.estimated_complete_route_duration)} - not on "
            f"its first leg: {nearest.stop_id} reaches a first customer after only "
            f"{format_duration(nearest.travel_time)} of driving and still ranks "
            f"#{report.rank_of(nearest.stop_id)}."
        )
        farthest_rank_text = (
            f"#{report.rank_of(farthest.stop_id)}" if farthest.feasible else "REJECTED"
        )
        lines.append(
            f"  ranks                       : recommended #1, nearest "
            f"#{report.rank_of(nearest.stop_id)}, farthest "
            f"{farthest_rank_text} of {len(report.ranked)} ranked candidates; farthest is "
            f"{'ranked' if farthest.feasible else 'rejected'}."
        )
    lines.append(
        "  NOTE: these are recommendations only. Nothing is applied and no working route is "
        "committed; the driver chooses the first service stop (D4/D32)."
    )
    lines.append("")

    # ---- baselines ------------------------------------------------------ #
    baselines = baseline_comparison(preview)
    lines.append("BASELINES (v2 section 30, D22)")
    lines.append(
        f"  {'route':<34} {'duration':>9}  {'distance':>9}  {'waiting':>8}  {'travel':>8}  "
        f"{'service':>8}  {'FINISH':>6}  feasible"
    )
    rows = (
        ("USER (input_position order)", baselines.user),
        (f"OPTIMIZED (around {recommended.stop_id})", baselines.optimized),
        ("ALGORITHM (greedy seed, internal)", baselines.algorithm),
    )
    for label, evaluation in rows:
        metrics = evaluation.metrics
        lines.append(
            f"  {label:<34} {format_duration(metrics.duration_sec):>9}  "
            f"{_distance_km(metrics.distance_m):>9}  "
            f"{format_duration(metrics.waiting_sec):>8}  "
            f"{format_duration(metrics.travel_sec):>8}  "
            f"{format_duration(metrics.service_sec):>8}  "
            f"{_clock(metrics.finish_arrival, zone):>6}  "
            f"{'yes' if metrics.feasible else 'NO'}"
        )
    lines.append(
        f"  saved by optimizing : {format_duration(baselines.saved_time)} of working time, "
        f"{_distance_km(baselines.saved_distance)} of driving, "
        f"{format_duration(baselines.saved_waiting)} of waiting (the user's own order vs the "
        f"optimized one - the product's BEFORE/AFTER, D22)"
    )
    lines.append(
        f"  local search       : greedy seed objective "
        f"{format_duration(preview.seed_objective)} -> final "
        f"{format_duration(preview.final_objective)}; {preview.accepted_moves} accepted moves, "
        f"{preview.search_evaluations} complete-route evaluations, "
        f"{preview.screened_moves} moves screened, "
        f"evaluation ceiling reached={preview.budget_exhausted}"
    )
    lines.append(
        "  ALGORITHM is the greedy seed the optimizer started from. It is an internal quality "
        "figure and is never shown as the driver's BEFORE route (D22)."
    )
    lines.append("")

    # ---- departure sweep ------------------------------------------------ #
    lines.append("DEPARTURE-TIME SWEEP - THE RECOMMENDATION CHANGES WITH THE DEPARTURE (v2 section 33)")
    lines.append(
        f"  {'departure':<10} {'status':<12} {'recommended':<22} {'first leg':>9}  "
        f"{'complete':>8}  {'waiting':>8}  {'ranked':>6}  {'rejected':>8}  note"
    )
    sweep_outcomes = departure_sweep(plan=plan)
    for outcome in sweep_outcomes:
        lines.append(
            f"  {outcome.local_hour:02d}:00      {outcome.status:<12} "
            f"{str(outcome.recommended_id or '-'):<22} "
            f"{(format_duration(outcome.first_leg) if outcome.first_leg is not None else '-'):>9}  "
            f"{(format_duration(outcome.complete_duration) if outcome.complete_duration is not None else '-'):>8}  "
            f"{(format_duration(outcome.complete_waiting) if outcome.complete_waiting is not None else '-'):>8}  "
            f"{outcome.ranked:>6}  {outcome.rejected:>8}  {outcome.note}"
        )
    sweep_ids = [str(outcome.recommended_id) for outcome in sweep_outcomes]
    sweep_pairs = ", ".join(
        f"{outcome.local_hour:02d}:00 -> {stop_id}"
        for outcome, stop_id in zip(sweep_outcomes, sweep_ids)
    )
    lines.append(
        f"  The same {len(active)} customers, the same day: leaving later replaces pre-opening "
        "driving with waiting, so the strongest complete route moves to a first stop closer to the "
        f"depot for hours the gap still contains - {sweep_pairs}. Nothing is selected at any hour "
        "(D4/D32)."
    )
    lines.append("")

    # ---- objective alignment (D35) -------------------------------------- #
    lines.append(
        "OBJECTIVE ALIGNMENT - PREVIOUS D31 PROVISIONAL vs THE NEW ELAPSED-DURATION DEFAULT (D35)"
    )
    previous_column = f"previous D31 (travel 1, wait {PREVIOUS_DEFAULT_WAITING_WEIGHT:g})"
    new_column = f"new default {SMART_ROUTE_ELAPSED_POLICY_NAME}"
    lines.append(
        f"  {'departure':<10} {previous_column:<34} {new_column:<36} {'FINISH':>6}  {'c.trav':>7}  "
        f"{'c.wait':>7}  {'c.svc':>7}  {'feasible':>8}  note"
    )
    alignment_rows = objective_alignment(plan=plan)
    for row in alignment_rows:
        lines.append(
            f"  {row.local_hour:02d}:00      "
            f"{str(row.previous_recommended_id or '-'):<34} "
            f"{str(row.new_recommended_id or '-'):<36} "
            f"{_clock(row.finish, zone):>6}  "
            f"{(format_duration(row.complete_travel) if row.complete_travel is not None else '-'):>7}  "
            f"{(format_duration(row.complete_waiting) if row.complete_waiting is not None else '-'):>7}  "
            f"{(format_duration(row.complete_service) if row.complete_service is not None else '-'):>7}  "
            f"{('yes' if row.feasible else 'NO') if row.feasible is not None else '-':>8}  "
            f"{'recommendation CHANGED' if row.changed else 'same recommendation'}"
        )
    lines.append(
        "  This table is the audit trail for the objective change (D35). The default SMART_ROUTE "
        "objective is now the complete elapsed route duration (travel + waiting + service, "
        "equivalently the FINISH arrival time for a fixed departure) with a waiting preference of "
        "zero, and the deterministic ranking key is (complete elapsed duration, complete travel, "
        "complete waiting, input_position, stop_id). The previous column is what the pre-D35 "
        "default produced at that hour - the non-default D31 provisional policy (travel 1, waiting "
        f"{PREVIOUS_DEFAULT_WAITING_WEIGHT:g}) ranked with the pre-D35 key (score, complete "
        "duration, input_position, stop_id) - and it is kept only as the sensitivity study below. "
        "Nothing was tuned to preserve the previous winner: where the two columns differ, the "
        "elapsed-duration recommendation is the shipped answer and the difference is reported "
        "rather than hidden. Every row is a recommendation only; nothing is selected (D4/D32)."
    )
    lines.append("")

    # ---- fingerprints --------------------------------------------------- #
    second_stop_id = next(
        (
            candidate.stop_id
            for candidate in report.ranked
            if candidate.stop_id != recommended.stop_id
        ),
        recommended.stop_id,
    )
    second_preview = recommendation_preview(plan, second_stop_id)
    lines.append("FINGERPRINTS (v2 section 7, v2 section 35, D4/D33)")
    lines.append(
        f"  recommendation fingerprint (plan.inputs_fingerprint)      : "
        f"{report.inputs_fingerprint}"
    )
    lines.append(
        "    unchanged by the driver's choice: it covers the recommendation inputs (departure, "
        "stops, windows, durations, priorities, finish, cost policy, timezone data), never the "
        "selected first stop - so accepting a recommendation cannot make it look stale."
    )
    lines.append(
        f"  same fingerprint after selecting {recommended.stop_id:<22}: "
        f"{preview.selected_plan.inputs_fingerprint()}"
    )
    lines.append(
        f"  route fingerprint, first stop {recommended.stop_id:<22}: {preview.route_fingerprint}"
    )
    lines.append(
        f"  route fingerprint, first stop {second_stop_id:<22}: {second_preview.route_fingerprint}"
    )
    lines.append(
        "  The committed route has its own fingerprint because it depends on the selected first "
        "stop and on the route order; the recommendation fingerprint deliberately does not "
        "(v2 section 7)."
    )
    lines.append("")

    # ---- rejected candidates -------------------------------------------- #
    lines.append("REJECTED / INFEASIBLE CANDIDATES (v2 section 14, D9)")
    rejected_lines = rejected_candidate_lines(report)
    if rejected_lines:
        lines.append(
            f"  {len(report.rejected)} of {report.candidates_evaluated} candidates have an "
            "infeasible complete route. Grouped by candidate, each with the stop ids that violate "
            "and the explicit reason; never ranked and never presented as a valid route (v2 "
            "section 14, D13 amendment)."
        )
        lines.extend(rejected_lines)
    else:
        lines.append(
            f"  none: all {report.candidates_evaluated} enabled stops produced a fully feasible "
            "complete route, so the ranking above is the full ranking."
        )
        lines.append(
            "  Hard infeasibility stays explicit and first-class (D13 amendment): a candidate whose "
            "complete route missed a hard service window would be listed here, grouped by "
            "candidate, with the violating stop ids and the reason, and never ranked or labelled a "
            "valid route."
        )
    lines.append("")

    # ---- weight sensitivity (D31) --------------------------------------- #
    lines.append(
        "SENSITIVITY STUDY (NON-DEFAULT) - WHAT A NON-ZERO WAITING PREFERENCE WOULD DO (D31)"
    )
    lines.append(
        f"  study base         : {_policy_text(demo_provisional_policy())} (only waiting_time "
        "varies per row)"
    )
    lines.append(
        f"  {'wait w':>6}  {'recommended':<22} {'objective':>10}  {'complete':>8}  "
        f"{'waiting':>8}  {'tied':>5}  note"
    )
    for sensitivity_row in weight_sensitivity(plan=plan):
        lines.append(
            f"  {sensitivity_row.waiting_weight:>6.1f}  "
            f"{str(sensitivity_row.recommended_id or '-'):<22} "
            f"{sensitivity_row.recommended_score:>10.0f}  "
            f"{format_duration(sensitivity_row.complete_duration):>8}  "
            f"{format_duration(sensitivity_row.complete_waiting):>8}  "
            f"{sensitivity_row.tied_with_recommendation:>5}  {sensitivity_row.note}"
        )
    lines.append(
        "  NON-DEFAULT STUDY, NOT THE SHIPPED OBJECTIVE: the default SMART_ROUTE objective has a "
        "waiting preference of ZERO (D35) and is the complete elapsed route duration, so the "
        "PROVISIONAL marker belongs to this study alone. Each row re-evaluates the same exhaustive "
        "candidate set under the historical D31 policy (travel_time 1, waiting_time "
        "1.0/1.5/2.0/3.0); the 1:1 row is numerically the default objective. The `recommended` "
        "column is the shipped engine's answer under that policy, i.e. ordered by the D35 key, "
        "which puts complete elapsed duration first - so a row whose note says a non-zero waiting "
        "preference would move the objective shows exactly that: the objective would prefer another "
        "stop and the shipped recommendation does not follow it. No weight was tuned to produce a "
        "winner, and each row is a recommendation only."
    )
    lines.append("")

    # ---- scale and performance (v2 section 20, D34, D36) ---------------- #
    lines.append("SCALE AND PERFORMANCE (v2 section 20, D34, D36)")
    lines.append(
        f"  The owner's Stage 2.1 scale decision (D36), verbatim: \"{OWNER_SCALE_STATEMENT}\""
    )
    scale_lines, _ = performance_scale_lines(active, disabled, plan, report, timings=timings)
    lines.extend(scale_lines)
    lines.append("")

    return "\n".join(_closing_lines(lines, report))


def performance_scale_lines(
    active: tuple[RouteStop, ...],
    disabled: tuple[RouteStop, ...],
    plan: RoutePlan,
    report: FirstStopEvaluationReport,
    *,
    timings: EvaluationTimings | None = None,
) -> tuple[list[str], dict[str, object]]:
    """The scale/performance block: ~30 stops, ~50 stops and the ~100-stop stress reference.

    Returns the printed lines and a small structured payload of the same facts, so a test can pin
    the labels and the enabled counts without parsing prose.

    The three scales it covers, each with its **exact enabled count** (D20: only enabled stops are
    candidates, so a label that names the total would misdescribe the counters):

    * the **~30-stop demo plan** - the product demo scenario, counted from the plan the report was
      built for;
    * the **~50-enabled-stop portfolio fixture** - the primary MVP scale target of D36, measured
      live by :func:`main` over the shipped exact implementation and printed with the v2 section 20
      preferred <= ~3 s / acceptable <= ~5 s targets as *reported* targets. The **measured number is
      printed, never asserted here**: the fast suite only pins the labels, so a machine-dependent
      second can never gate it (the benchmark tool and the opt-in slow module carry the guarded
      bound);
    * the **~100-stop stress reference** - :data:`RECORDED_BENCHMARK`, relabelled by D36 as future
      scale / **not performance-qualified**, keeping its honest measured number and the
      owner-accepted bound of D34.
    """
    lines: list[str] = []
    lines.append(
        "  demo plan          : "
        f"{_performance_stop_count_line(active, disabled, plan)} - "
        f"{report.candidates_evaluated} candidates, {report.optimizer_runs} optimizer runs, "
        f"{report.cache_stats.lookups} leg questions "
        f"({report.cache_stats.hits} hits / {report.cache_stats.misses} misses, "
        f"{report.cache_stats.entries} entries)."
    )
    lines.append(
        "  measured runtime   : printed in the STATUS AND WORK section when the caller measures it "
        f"(`python -m demo.report`). v2 section 20's acceptable target for ~100 stops is "
        f"<= {DEMO_EVALUATION_RUNTIME_BUDGET_SEC:.0f}s; that is an engineering target, not a "
        "correctness rule."
    )

    portfolio_plan = build_portfolio_plan()
    portfolio_active = len(portfolio_plan.active_stops())
    portfolio_total = len(portfolio_plan.stops)
    portfolio_disabled = len(portfolio_plan.disabled_stops())
    lines.append(
        f"  portfolio fixture  : {portfolio_active} enabled stops ({portfolio_total} stops, "
        f"{portfolio_disabled} disabled) - PRIMARY MVP TARGET (D36), approximately 50 enabled "
        "service stops, DEMO/SYNTHETIC."
    )
    lines.append(
        f"    provenance       : built by demo.scale_dataset.build_portfolio_plan "
        f"({PORTFOLIO_STOP_COUNT} stops, deterministic disabled policy of every "
        "demo.scale_dataset.PORTFOLIO_DISABLED_EVERY-th stop, so the enabled count is exactly "
        f"{PORTFOLIO_ENABLED_STOP_COUNT}); synthetic coordinates, windows and durations - never "
        "real addresses or opening hours."
    )
    lines.append(
        "    scale decision   : ~100 stops is no longer a hard MVP performance requirement. The "
        "~100-stop figure below is the engineering stress reference; it is NOT performance-qualified "
        "in this MVP, and failing the old <= "
        f"{PORTFOLIO_ACCEPTABLE_BUDGET_SEC:.0f}s target at 100 stops does NOT block the portfolio "
        "MVP (D36)."
    )
    lines.append(
        f"    targets          : v2 section 20 reported engineering targets at this scale - "
        f"preferred <= ~{PORTFOLIO_PREFERRED_BUDGET_SEC:.0f}s, acceptable "
        f"<= ~{PORTFOLIO_ACCEPTABLE_BUDGET_SEC:.0f}s."
    )
    if timings is not None and timings.portfolio_seconds is not None:
        lines.append(
            f"    measured now     : exhaustive complete-route first-stop loop over all "
            f"{portfolio_active} enabled stops = {timings.portfolio_seconds:.2f}s warm "
            f"({timings.portfolio_seconds / portfolio_active * 1000:.0f}ms per candidate) - "
            "MEASURED WALL CLOCK, machine-dependent, printed not asserted."
        )
        lines.append(
            "    honesty          : the figure above is the shipped exact implementation's measured "
            f"number, reported as it is. It is {'inside' if timings.portfolio_seconds <= PORTFOLIO_ACCEPTABLE_BUDGET_SEC else 'OUTSIDE'} "
            f"the acceptable <= ~{PORTFOLIO_ACCEPTABLE_BUDGET_SEC:.0f}s target. No prefilter, no "
            "approximate ranking, no shortlist and no quality-degrading cut was introduced to move "
            "it, and no number is estimated or borrowed from another scale (D36, v2 section 20)."
        )
    else:
        lines.append(
            "    measured now     : not measured in this call; reproduce it with "
            f"`{PORTFOLIO_BENCHMARK_COMMAND}`, which measures this fixture, the ~100-stop stress "
            "reference and the demo plan in one run."
        )

    benchmark = RECORDED_BENCHMARK
    lines.append(
        f"  stress reference   : {benchmark.stop_count} enabled stops (~{SCALE_DEFAULT_STOP_COUNT} "
        "stops, ENGINEERING STRESS REFERENCE, NOT PERFORMANCE-QUALIFIED - D36), DEMO/SYNTHETIC."
    )
    lines.append(
        f"    ~100-stop record : RECORDED measurement ({benchmark.measured_on}), not a live run of "
        f"this report. Reproduce it with `{BENCHMARK_COMMAND}`."
    )
    lines.append(
        f"    stops {benchmark.stop_count} enabled -> candidates {benchmark.candidates}, "
        f"{benchmark.candidates_at_ceiling} truncated by the per-candidate evaluation ceiling; "
        f"warm exhaustive loop {benchmark.total_seconds:.2f}s "
        f"({benchmark.seconds_per_candidate * 1000:.0f}ms per candidate), "
        f"{benchmark.route_evaluations} route evaluations, {benchmark.accepted_moves} accepted "
        f"moves, leg cache {benchmark.cache_hits} hits / {benchmark.cache_misses} misses / "
        f"{benchmark.cache_entries} entries."
    )
    lines.append(
        f"    The v2 section 20 <= {DEMO_EVALUATION_RUNTIME_BUDGET_SEC:.0f}s target is "
        f"{'met' if benchmark.spec_target_met else 'NOT met'} at that scale; under D36 that is "
        "reported and is NOT an MVP gate. The owner accepted the measured latency as an interim "
        f"limitation (D34) with an asserted regression guard of "
        f"{benchmark.accepted_interim_limit_sec:.0f}s, and the candidate set stays exhaustive - no "
        "prefilter, no weight tuning, no neighbourhood cut. The fixture and its tests are retained."
    )
    lines.append(
        "  scope              : the loop is exhaustive over every enabled candidate at every scale - "
        "no prefilter, no shortlist and no approximation (D34, D36, v2 section 20)."
    )
    lines.append(
        "  architecture       : the domain has no hard 50-stop maximum. The scale decision changes "
        "the performance target, never a validation limit, so the architecture stays able to evolve "
        "beyond 50 stops (D36, D18)."
    )
    payload: dict[str, object] = {
        "demo_enabled": len(active),
        "demo_total": len(plan.stops),
        "demo_disabled": len(disabled),
        "portfolio_enabled": portfolio_active,
        "portfolio_total": portfolio_total,
        "portfolio_disabled": portfolio_disabled,
        "stress_enabled": benchmark.stop_count,
        "stress_total": SCALE_DEFAULT_STOP_COUNT,
        "owner_statement": OWNER_SCALE_STATEMENT,
        "portfolio_seconds": timings.portfolio_seconds if timings is not None else None,
    }
    return lines, payload


def _closing_lines(lines: list[str], report: FirstStopEvaluationReport) -> list[str]:
    lines.append("DETERMINISM")
    lines.append(
        "  Identical plan + synthetic matrix + cost policy print an identical report: no "
        "wall-clock, no randomness and no network enter the numbers. The only machine-dependent "
        "lines are the measured runtimes, which are clearly marked and only present when measured."
    )
    lines.append(
        f"  Ranking is exhaustive and deterministic: {report.ranked_ids()[:3]}... "
        f"({len(report.ranked)} ranked candidates), key (complete elapsed duration, complete "
        "travel time, complete waiting time, input_position, stop_id) - D35."
    )
    lines.append(_SEPARATOR)
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Print the demo report. Returns a process exit code.

    A time zone database is required. When ``tzdata`` cannot be installed (offline machine), the
    standard ``PYTHONTZPATH`` mechanism works, or ``--allow-system-tzdata`` activates a discovered
    system TZif tree explicitly and prints a warning. Nothing falls back silently (D12).
    """
    parser = argparse.ArgumentParser(
        prog="demo.report",
        description=(
            "Print the RoutePilot demo scenario with complete-route first-stop recommendations."
        ),
    )
    parser.add_argument(
        "--allow-system-tzdata",
        action="store_true",
        help=(
            "development only: use a discovered system TZif tree when the tzdata package is "
            "unavailable (prints a warning)"
        ),
    )
    arguments = parser.parse_args(argv)

    if arguments.allow_system_tzdata:
        activated = tzdata.activate_system_tzif_fallback()
        if activated is not None:
            print(
                "[demo] WARNING: the tzdata package is unavailable; using the system TZif tree "
                f"at {activated}. This is a development fallback - install the real dependency "
                f"with: {TZDATA_INSTALL_COMMAND}",
                file=sys.stderr,
            )

    import time as timer

    started = timer.perf_counter()
    demo_evaluation(build_demo_plan())
    demo_seconds = timer.perf_counter() - started

    started = timer.perf_counter()
    departure_sweep()
    sweep_seconds = timer.perf_counter() - started

    # The ~50-enabled-stop portfolio fixture is the primary MVP scale target (D36), so the report
    # measures the shipped exhaustive loop over it for real instead of quoting a number from another
    # scale. It runs once, after the body, and only its measurement is printed in the scale block.
    started = timer.perf_counter()
    portfolio_evaluation()
    portfolio_seconds = timer.perf_counter() - started

    print(
        build_report(
            timings=EvaluationTimings(
                demo_seconds=demo_seconds,
                sweep_seconds=sweep_seconds,
                portfolio_seconds=portfolio_seconds,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
