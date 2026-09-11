"""Numeric demo report (spec sections 1, 24, 25; Stage 2 U4 wiring).

Prints, deterministically:

* the demo plan and its provenance warning;
* the input route (the order as supplied by the user = the product's BEFORE baseline);
* first-leg timelines for the notable stops;
* candidate first legs for every possible first stop, plus the top 5;
* why the first-leg view is neither the nearest nor the farthest stop;
* the effect of changing the departure time;
* sensitivity to the provisional waiting weight;
* the window-end policy (D29) and what it changes;
* the data-quality caveat for stops whose business hours are unknown.

**Interim, and stated rather than hidden (U4/U5):** the recommendation engine now ranks
**complete-route** outcomes (``core.engine.first_stop.evaluation``, v2 sections 12-14). This
report still narrates the Stage 1 *first-leg* view, because that narrative is what the demo's
A-G acceptance criteria pin; the demo narrative refresh is a later unit (U5). The first-leg
numbers here are produced by the same timeline arithmetic and the same cost policy the engine
uses - :func:`candidate_first_leg_view` - so they cannot drift from the engine's own per-leg
figures.

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

from core.engine.cost import breakdown_as_tuple, score_breakdown
from core.engine.providers import TravelMatrix
from core.model.cost_policy import (
    DEMO_TRAVEL_TIME_WEIGHT,
    CostComponent,
    RouteCostPolicy,
    demo_provisional_policy,
)
from core.model.ids import PlanId, StopId
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.solution import StopTimeline, Violation
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.time import timeline as timeline_engine
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
from demo.synthetic_matrix import DEMO_MATRIX_DISCLAIMER, demo_matrix

__all__ = [
    "DEPARTURE_SWEEP_HOURS",
    "WEIGHT_SENSITIVITY_RATIOS",
    "DepartureOutcome",
    "FirstLegCandidate",
    "FirstLegReport",
    "UnknownHoursDistortion",
    "UnknownHoursRow",
    "WeightSensitivityRow",
    "build_report",
    "candidate_first_leg_view",
    "departure_sweep",
    "format_duration",
    "main",
    "stops_without_fixed_window",
    "unknown_hours_distortion",
    "weight_sensitivity",
    "window_end_policy_comparison",
]

#: Local departure hours shown in the departure-time sweep.
DEPARTURE_SWEEP_HOURS = (4, 5, 6, 7, 8)

#: waiting:travel ratios shown in the sensitivity section (1.0 demonstrates the degeneracy).
WEIGHT_SENSITIVITY_RATIOS = (1.0, 1.5, 2.0, 3.0)

_SEPARATOR = "=" * 100
_SUBSEPARATOR = "-" * 100


# --------------------------------------------------------------------------- #
# formatting
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
        for component, weight in sorted(policy.weights.items(), key=lambda i: i[0].value)
    )
    marker = " - PROVISIONAL DEMO WEIGHTS, not product truth" if policy.provisional else ""
    return f"{policy.name} ({weights}){marker}"


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DepartureOutcome:
    """Result of evaluating the demo plan at one departure time.

    ``recommended_*`` describes what RoutePilot would propose - never a selection. The driver makes
    the first-stop decision (D4/D32).
    """

    local_hour: int
    departure: datetime
    recommended_id: str | None
    recommended_score: float
    recommended_travel: int
    recommended_wait: int
    runner_up_id: str | None
    runner_up_score: float | None
    recommended_note: str = ""


@dataclass(frozen=True)
class WeightSensitivityRow:
    """Recommendation under one waiting:travel ratio."""

    waiting_weight: float
    recommended_id: str | None
    recommended_score: float
    note: str


def evaluate(
    *,
    plan: RoutePlan,
    policy: RouteCostPolicy | None = None,
) -> FirstLegReport:
    """The demo's first-leg view of every candidate of one plan (interim, see the module docstring)."""
    return candidate_first_leg_view(
        plan=plan, travel_matrix=demo_matrix(), policy=policy
    )


# --------------------------------------------------------------------------- #
# interim first-leg view of the candidates (the engine ranks complete routes)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FirstLegCandidate:
    """One candidate timed and priced on its **first leg** only (the Stage 1 view, D31).

    Interim and local to the demo: the recommendation engine ranks complete-route outcomes
    (v2 sections 12-14). The fields are exactly the timeline's, so they cannot drift from the
    engine's own per-leg arithmetic, and ``violation`` is the timeline's explicit violation.
    """

    stop_id: StopId
    timeline: StopTimeline
    distance_m: float
    cost_breakdown: tuple[tuple[CostComponent, float], ...]
    score: float
    violation: Violation | None = None

    # ---- delegated timeline facts (single source of truth) ------------- #
    @property
    def feasible(self) -> bool:
        return not self.timeline.is_infeasible

    @property
    def travel_time(self) -> int:
        return self.timeline.travel_time

    @property
    def waiting_time(self) -> int:
        return self.timeline.waiting_time

    @property
    def estimated_arrival(self) -> datetime:
        return self.timeline.estimated_arrival

    @property
    def service_start(self) -> datetime:
        return self.timeline.service_start

    @property
    def estimated_departure(self) -> datetime:
        return self.timeline.estimated_departure

    @property
    def service_window_start(self) -> datetime | None:
        return self.timeline.service_window_start

    @property
    def service_window_end(self) -> datetime | None:
        return self.timeline.service_window_end

    @property
    def window_end_policy(self) -> WindowEndPolicy | None:
        return self.timeline.window_end_policy

    @property
    def lateness(self) -> int:
        return self.timeline.lateness

    @property
    def finish_overtime(self) -> int:
        return self.timeline.finish_overtime

    def component(self, component: CostComponent) -> float:
        """Measured value of one cost component (0.0 when not part of the breakdown)."""
        for candidate_component, value in self.cost_breakdown:
            if candidate_component is component:
                return value
        return 0.0


@dataclass(frozen=True)
class FirstLegReport:
    """Every candidate's first leg: feasible candidates ranked, infeasible ones separated.

    A local, interim view (see the module docstring). ``ranked`` orders by
    ``(score, travel_time, stop_id)`` - the Stage 1 tie-break - and ``infeasible`` holds the
    candidates whose first leg misses a hard window.
    """

    plan_id: PlanId
    inputs_fingerprint: str
    ranked: tuple[FirstLegCandidate, ...]
    infeasible: tuple[FirstLegCandidate, ...]
    disabled_stop_ids: tuple[StopId, ...]

    def recommended(self) -> FirstLegCandidate | None:
        return self.ranked[0] if self.ranked else None

    @property
    def recommended_stop_id(self) -> StopId | None:
        recommended = self.recommended()
        return recommended.stop_id if recommended is not None else None

    def nearest(self) -> FirstLegCandidate | None:
        if not self.ranked:
            return None
        return min(self.ranked, key=lambda candidate: (candidate.travel_time, candidate.stop_id))

    def farthest(self) -> FirstLegCandidate | None:
        if not self.ranked:
            return None
        return min(self.ranked, key=lambda candidate: (-candidate.travel_time, candidate.stop_id))

    def find(self, stop_id: StopId) -> FirstLegCandidate | None:
        for candidate in self.ranked:
            if candidate.stop_id == stop_id:
                return candidate
        for candidate in self.infeasible:
            if candidate.stop_id == stop_id:
                return candidate
        return None

    def rank_of(self, stop_id: StopId) -> int | None:
        for position, candidate in enumerate(self.ranked, start=1):
            if candidate.stop_id == stop_id:
                return position
        return None

    def top(self, count: int) -> tuple[FirstLegCandidate, ...]:
        return self.ranked[:count]


def candidate_first_leg_view(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    policy: RouteCostPolicy | None = None,
) -> FirstLegReport:
    """Time and price every candidate's **first leg** only.

    Interim demo view, kept because the demo's A-G narrative is about the first-leg degeneracy
    (Stage 1, D31); the engine itself ranks complete routes. The timing is
    :func:`core.time.timeline.compute_stop_timeline` and the pricing is
    :func:`core.engine.cost.score_breakdown` with the configured policy, so neither number is a
    second model of the engine's arithmetic.
    """
    cost_policy = policy if policy is not None else plan.cost_policy
    tzinfo = plan.load_timezone()
    candidates: list[FirstLegCandidate] = []

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
        candidates.append(
            FirstLegCandidate(
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
            (candidate for candidate in candidates if candidate.feasible),
            key=lambda candidate: (candidate.score, candidate.travel_time, candidate.stop_id),
        )
    )
    infeasible = tuple(
        sorted(
            (candidate for candidate in candidates if not candidate.feasible),
            key=lambda candidate: (candidate.travel_time, candidate.stop_id),
        )
    )
    return FirstLegReport(
        plan_id=plan.id,
        inputs_fingerprint=plan.inputs_fingerprint(),
        ranked=ranked,
        infeasible=infeasible,
        disabled_stop_ids=tuple(stop.id for stop in plan.disabled_stops()),
    )


def departure_sweep(
    *,
    hours: tuple[int, ...] = DEPARTURE_SWEEP_HOURS,
    policy: RouteCostPolicy | None = None,
) -> tuple[DepartureOutcome, ...]:
    """Evaluate the demo plan at several local departure hours."""
    outcomes: list[DepartureOutcome] = []
    for hour in hours:
        plan = build_demo_plan(departure_time=demo_departure_time_at(hour))
        report = evaluate(plan=plan, policy=policy)
        recommended = report.recommended()
        if recommended is None:
            outcomes.append(
                DepartureOutcome(hour, plan.departure_time, None, 0.0, 0, 0, None, None)
            )
            continue
        runner_up = report.ranked[1] if len(report.ranked) > 1 else None
        note = ""
        stop = plan.stop_by_id(recommended.stop_id)
        if stop.service_window.window_kind.value == "unknown":
            note = "hours unknown: no opening constraint applies"
        elif recommended.waiting_time == 0:
            note = "arrives at or after opening: no waiting"
        outcomes.append(
            DepartureOutcome(
                local_hour=hour,
                departure=plan.departure_time,
                recommended_id=recommended.stop_id,
                recommended_score=recommended.score,
                recommended_travel=recommended.travel_time,
                recommended_wait=recommended.waiting_time,
                runner_up_id=runner_up.stop_id if runner_up else None,
                runner_up_score=runner_up.score if runner_up else None,
                recommended_note=note,
            )
        )
    return tuple(outcomes)


def weight_sensitivity(
    *,
    ratios: tuple[float, ...] = WEIGHT_SENSITIVITY_RATIOS,
    travel_weight: float = DEMO_TRAVEL_TIME_WEIGHT,
) -> tuple[WeightSensitivityRow, ...]:
    """Recommended stop at 04:00 for several waiting:travel ratios (the demo weights are provisional)."""
    rows: list[WeightSensitivityRow] = []
    for ratio in ratios:
        policy = demo_provisional_policy(
            travel_time_weight=travel_weight, waiting_time_weight=ratio
        )
        report = evaluate(plan=build_demo_plan(cost_policy=policy), policy=policy)
        recommended = report.recommended()
        note = ""
        if recommended is not None and ratio == travel_weight:
            ties = [
                evaluation
                for evaluation in report.ranked
                if abs(evaluation.score - recommended.score) < 1e-9
            ]
            note = (
                f"degenerate: {len(ties)} candidates tie at {recommended.score:.0f}; the tie-break "
                "(shortest first leg) hands the recommendation to the nearest stop"
            )
        elif recommended is not None:
            note = "waiting is penalised, so arriving close to opening is recommended"
        rows.append(
            WeightSensitivityRow(
                waiting_weight=ratio,
                recommended_id=recommended.stop_id if recommended else None,
                recommended_score=recommended.score if recommended else 0.0,
                note=note,
            )
        )
    return tuple(rows)


def window_end_policy_comparison() -> tuple[tuple[WindowEndPolicy, str, bool, str], ...]:
    """The same stop, same window, both end policies (D29).

    Returns rows of ``(policy, summary, feasible, violation_message)``.
    """
    zone = tzdata.load_timezone(DEMO_TIMEZONE)
    rows: list[tuple[WindowEndPolicy, str, bool, str]] = []
    for policy in (
        WindowEndPolicy.SERVICE_FINISH_BEFORE_END,
        WindowEndPolicy.SERVICE_START_BEFORE_END,
    ):
        plan = build_demo_plan(window_end_policy=policy)
        evaluation = evaluate(plan=plan).find(HEADLINE_STOP_IDS["edge_window"])
        assert evaluation is not None
        summary = (
            f"travel {format_duration(evaluation.travel_time)}, "
            f"ETA {_clock(evaluation.estimated_arrival, zone)}, "
            f"window {_clock(evaluation.service_window_start, zone)}-"
            f"{_clock(evaluation.service_window_end, zone)}, "
            f"start {_clock(evaluation.service_start, zone)}, "
            f"finish {_clock(evaluation.estimated_departure, zone)}, "
            f"lateness {format_duration(evaluation.lateness)}, "
            f"finish_overtime {format_duration(evaluation.finish_overtime)}"
        )
        message = evaluation.violation.message if evaluation.violation else ""
        rows.append((policy, summary, evaluation.feasible, message))
    return tuple(rows)


@dataclass(frozen=True)
class UnknownHoursRow:
    """A stop with no fixed window, as travelled."""

    stop_id: str
    window_text: str
    travel: int
    score: float
    rank: int | None


@dataclass(frozen=True)
class UnknownHoursDistortion:
    """The same stop, same distance, with known hours vs with its hours unknown."""

    stop_id: str
    window_text: str
    score_known: float
    rank_known: int | None
    travel: int
    score_if_unknown: float
    rank_if_unknown: int | None


def stops_without_fixed_window() -> tuple[UnknownHoursRow, ...]:
    """Every stop with unknown or unrestricted hours, with its cost and rank as travelled."""
    plan = build_demo_plan()
    report = evaluate(plan=plan)
    rows: list[UnknownHoursRow] = []
    for stop in plan.active_stops():
        if stop.service_window.is_fixed:
            continue
        evaluation = report.find(stop.id)
        assert evaluation is not None
        rows.append(
            UnknownHoursRow(
                stop_id=stop.id,
                window_text=stop.service_window.describe(),
                travel=evaluation.travel_time,
                score=evaluation.score,
                rank=report.rank_of(stop.id),
            )
        )
    return tuple(rows)


def unknown_hours_distortion(*, stop_key: str = "nearest") -> UnknownHoursDistortion:
    """Cost and rank of a stop with known hours vs the same stop with unknown hours.

    Removing the opening hours removes every reason to wait, so a nearby stop becomes the cheapest
    candidate purely because nothing is known about it. This is the distortion the demo makes
    visible: unknown hours must be resolved before a route is trusted (spec sections 7 and 16).
    """
    plan = build_demo_plan()
    report = evaluate(plan=plan)
    stop_id = HEADLINE_STOP_IDS[stop_key]
    known = report.find(stop_id)
    assert known is not None

    unknown_stop = dataclasses.replace(
        plan.stop_by_id(stop_id), service_window=ServiceWindow.unknown()
    )
    unknown_plan = dataclasses.replace(
        plan,
        stops=tuple(
            unknown_stop if existing.id == stop_id else existing for existing in plan.stops
        ),
    )
    unknown_report = evaluate(plan=unknown_plan)
    unknown = unknown_report.find(stop_id)
    assert unknown is not None

    return UnknownHoursDistortion(
        stop_id=stop_id,
        window_text=plan.stop_by_id(stop_id).service_window.describe(),
        score_known=known.score,
        rank_known=report.rank_of(stop_id),
        travel=known.travel_time,
        score_if_unknown=unknown.score,
        rank_if_unknown=unknown_report.rank_of(stop_id),
    )


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _candidate_row(
    position: int | str,
    evaluation: FirstLegCandidate,
    zone: ZoneInfo,
    stop: RouteStop,
) -> str:
    rank = f"{position:>3}"
    return (
        f"{rank}  {evaluation.stop_id:<22} "
        f"{format_duration(evaluation.travel_time):>7}  "
        f"{_clock(evaluation.estimated_arrival, zone):>5}  "
        f"{_window_text(stop):<14}  "
        f"{format_duration(evaluation.waiting_time):>7}  "
        f"{_clock(evaluation.service_start, zone):>5}  "
        f"{_clock(evaluation.estimated_departure, zone):>5}  "
        f"{'yes' if evaluation.feasible else 'NO':>3}  "
        f"{evaluation.score:>10.0f}  "
        f"{evaluation.component(CostComponent.TRAVEL_TIME):>9.0f}  "
        f"{evaluation.component(CostComponent.WAITING_TIME):>9.0f}"
    )


def _candidate_header() -> str:
    return (
        f"{'#':>3}  {'stop':<22} {'travel':>7}  {'ETA':>5}  {'window':<14}  "
        f"{'wait':>7}  {'start':>5}  {'finish':>5}  {'fea':>3}  {'score':>10}  "
        f"{'travel_c':>9}  {'wait_c':>9}"
    )


def build_report() -> str:
    """The complete deterministic demo report as text."""
    zone = tzdata.load_timezone(DEMO_TIMEZONE)
    plan = build_demo_plan()
    matrix = demo_matrix()
    report = evaluate(plan=plan)
    lines: list[str] = []

    active = plan.active_stops()
    disabled = plan.disabled_stops()
    fixed_08 = [
        stop for stop in active if stop.service_window.is_fixed and stop.service_window.describe().startswith("08:00")
    ]

    lines.append(_SEPARATOR)
    lines.append("RoutePilot demo scenario - DEMO / SYNTHETIC DATA")
    lines.append(DEMO_WARNING)
    lines.append(DEMO_MATRIX_DISCLAIMER)
    lines.append(_SEPARATOR)
    lines.append("")
    lines.append(f"plan              : {plan.id}")
    lines.append(f"time zone         : {plan.timezone} (service date {DEMO_SERVICE_DATE.isoformat()})")
    lines.append(
        f"departure         : {_clock(plan.departure_time, zone)} local "
        f"({plan.departure_time.isoformat()}) from {plan.departure.label}"
    )
    lines.append(f"finish            : {plan.finish.label} (fixed, never reordered)")
    lines.append(
        f"stops             : {len(plan.stops)} total, {len(active)} enabled, "
        f"{len(disabled)} disabled, {len(fixed_08)} opening at 08:00"
    )
    lines.append(
        f"default service   : {format_duration(DEMO_DEFAULT_SERVICE_DURATION)} "
        "(used when a stop's duration is unknown)"
    )
    lines.append(f"window end policy : {plan.window_end_policy.value} (plan default, D29)")
    lines.append(
        f"first stop        : {plan.first_stop_state.value} - the driver decides; nothing is "
        "applied automatically (D4/D32)"
    )
    lines.append(f"cost policy       : {_policy_text(plan.cost_policy)}")
    lines.append("")

    # ---- input route (the user's BEFORE order) ------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append("INPUT ROUTE - the order exactly as supplied by the user (the product's BEFORE)")
    lines.append(_SUBSEPARATOR)
    lines.append(
        f"{'pos':>3}  {'stop':<22} {'travel from depot':>17}  {'window':<14}  "
        f"{'service':>7}  {'prio':>4}  {'state':<8}  address"
    )
    for stop in plan.stops:
        # `pos` is the stop's immutable input_position: the BEFORE baseline order (v2 section 30).
        evaluation = report.find(stop.id)
        travel = format_duration(evaluation.travel_time) if evaluation else "-"
        duration = (
            format_duration(stop.service_duration)
            if stop.service_duration is not None
            else f"default {format_duration(DEMO_DEFAULT_SERVICE_DURATION)}"
        )
        state = "enabled" if stop.enabled else "DISABLED"
        lines.append(
            f"{stop.input_position:>3}  {stop.id:<22} {travel:>17}  {_window_text(stop):<14}  "
            f"{duration:>7}  "
            f"{(stop.priority if stop.priority is not None else '-'):>4}  {state:<8}  "
            f"{stop.raw_address}"
        )
    lines.append("")

    # ---- notable timelines --------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append("FIRST-LEG TIMELINE for notable stops (spec section 7 fields)")
    lines.append(_SUBSEPARATOR)
    lines.append(
        f"{'stop':<22} {'role':<22} {'travel':>7}  {'ETA':>5}  {'opens':>5}  "
        f"{'wait':>7}  {'start':>5}  {'finish':>5}  {'feasible':<9} policy"
    )
    roles = {
        HEADLINE_STOP_IDS["nearest"]: "nearest",
        HEADLINE_STOP_IDS["far_before_opening"]: "far, arrives early",
        HEADLINE_STOP_IDS["on_opening"]: "arrives at opening",
        HEADLINE_STOP_IDS["farthest"]: "farthest",
        HEADLINE_STOP_IDS["tight_window"]: "tight window",
        HEADLINE_STOP_IDS["edge_window"]: "end-policy edge case",
        HEADLINE_STOP_IDS["unknown_hours"]: "hours unknown",
    }
    for stop_id, role in roles.items():
        evaluation = report.find(stop_id)
        if evaluation is None:
            continue
        policy_label = (
            evaluation.window_end_policy.value if evaluation.window_end_policy else "-"
        )
        lines.append(
            f"{stop_id:<22} {role:<22} {format_duration(evaluation.travel_time):>7}  "
            f"{_clock(evaluation.estimated_arrival, zone):>5}  "
            f"{_clock(evaluation.service_window_start, zone):>5}  "
            f"{format_duration(evaluation.waiting_time):>7}  "
            f"{_clock(evaluation.service_start, zone):>5}  "
            f"{_clock(evaluation.estimated_departure, zone):>5}  "
            f"{'yes' if evaluation.feasible else 'NO':<9} {policy_label}"
        )
    lines.append("")

    # ---- all candidates ------------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append(
        f"ALL CANDIDATE FIRST STOPS by cost ({len(report.ranked)} feasible, "
        f"{len(report.infeasible)} infeasible; costs in seconds x weight)"
    )
    lines.append(_SUBSEPARATOR)
    lines.append(_candidate_header())
    for position, evaluation in enumerate(report.ranked, start=1):
        lines.append(
            _candidate_row(position, evaluation, zone, plan.stop_by_id(evaluation.stop_id))
        )
    for evaluation in report.infeasible:
        stop = plan.stop_by_id(evaluation.stop_id)
        lines.append(_candidate_row("--", evaluation, zone, stop))
    lines.append("")
    for evaluation in report.infeasible:
        lines.append(f"INFEASIBLE  {evaluation.stop_id}: {evaluation.violation.message}")
    if report.disabled_stop_ids:
        lines.append(
            "EXCLUDED    disabled stops never become candidates: "
            + ", ".join(report.disabled_stop_ids)
        )
    lines.append("")

    # ---- top 5 ---------------------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append("TOP 5 CANDIDATES (the answer to 'what are the candidate costs?')")
    lines.append(_SUBSEPARATOR)
    lines.append(_candidate_header())
    for position, evaluation in enumerate(report.top(5), start=1):
        lines.append(
            _candidate_row(position, evaluation, zone, plan.stop_by_id(evaluation.stop_id))
        )
    lines.append("")

    # ---- nearest / farthest / recommendation ---------------------------- #
    recommended = report.recommended()
    nearest = report.nearest()
    farthest = report.farthest()
    assert recommended is not None and nearest is not None and farthest is not None
    lines.append(_SUBSEPARATOR)
    lines.append("WHY THE RECOMMENDATION IS NEITHER THE NEAREST NOR THE FARTHEST")
    lines.append(_SUBSEPARATOR)
    for label, evaluation in (
        ("nearest      ", nearest),
        ("recommended  ", recommended),
        ("farthest     ", farthest),
    ):
        rank = report.rank_of(evaluation.stop_id)
        lines.append(
            f"{label} {evaluation.stop_id:<22} rank #{rank:<3} "
            f"travel {format_duration(evaluation.travel_time):>7}  "
            f"wait {format_duration(evaluation.waiting_time):>7}  "
            f"score {evaluation.score:>10.0f}"
        )
    lines.append("")
    lines.append(
        f"why it is recommended : it reaches the opening time with the least driving - "
        f"{format_duration(recommended.travel_time)} of driving and "
        f"{format_duration(recommended.waiting_time)} of waiting, total {recommended.score:.0f}."
    )
    lines.append(
        f"why nearest loses     : the {format_duration(nearest.travel_time)} leg is cheap, but "
        f"{format_duration(nearest.waiting_time)} of dead waiting costs "
        f"{nearest.component(CostComponent.WAITING_TIME):.0f} units, so it totals "
        f"{nearest.score:.0f} and ranks #{report.rank_of(nearest.stop_id)}."
    )
    lines.append(
        f"why farthest loses    : {format_duration(farthest.travel_time)} of driving saves no "
        f"waiting at all (it already arrives after opening), so it totals {farthest.score:.0f} - "
        f"{farthest.score - recommended.score:.0f} more than the recommendation."
    )
    lines.append("")
    lines.append(
        "NOTE: these are recommendations only. Nothing is applied and no working route is "
        "committed; the driver chooses the first service stop (D4/D32)."
    )
    lines.append("")
    groups: dict[str, list[int]] = {}
    for evaluation in report.ranked:
        if evaluation.waiting_time <= 0:
            continue
        stop = plan.stop_by_id(evaluation.stop_id)
        if not stop.service_window.is_fixed:
            continue
        opening = _window_text(stop).split("-", 1)[0]
        groups.setdefault(opening, []).append(evaluation.travel_time + evaluation.waiting_time)
    if groups:
        lines.append(
            "structural note: a candidate that arrives before its opening has travel + waiting "
            "fixed by that opening:"
        )
        for opening in sorted(groups):
            totals = ", ".join(
                format_duration(total) for total in sorted(set(groups[opening]))
            )
            lines.append(
                f"  opening {opening}: {len(groups[opening])} candidate(s), "
                f"travel + waiting = {totals}"
            )
        lines.append(
            "So within one opening time the first leg alone cannot rank candidates at all, and at a "
            "1:1 weight ratio they tie exactly."
        )
        lines.append(
            "That is why spec section 8 requires candidate quality to include the route AFTER the "
            "candidate, and why the demo weights are marked provisional."
        )
    lines.append("")

    # ---- departure sweep ------------------------------------------------ #
    lines.append(_SUBSEPARATOR)
    lines.append("EFFECT OF CHANGING DEPARTURE TIME")
    lines.append(_SUBSEPARATOR)
    lines.append(
        f"{'departure':<10} {'recommended':<22} {'travel':>7}  {'wait':>7}  {'score':>10}  "
        f"{'runner-up':<22} {'score':>10}  note"
    )
    for outcome in departure_sweep():
        lines.append(
            f"{outcome.local_hour:02d}:00      {str(outcome.recommended_id):<22} "
            f"{format_duration(outcome.recommended_travel):>7}  "
            f"{format_duration(outcome.recommended_wait):>7}  "
            f"{outcome.recommended_score:>10.0f}  "
            f"{str(outcome.runner_up_id):<22} "
            f"{(outcome.runner_up_score if outcome.runner_up_score is not None else 0):>10.0f}  "
            f"{outcome.recommended_note}"
        )
    lines.append("")

    # ---- weight sensitivity --------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append("SENSITIVITY TO THE PROVISIONAL WAITING WEIGHT (departure 04:00)")
    lines.append(_SUBSEPARATOR)
    lines.append(f"{'wait/travel':>11}  {'recommended':<22} {'score':>10}  note")
    for row in weight_sensitivity():
        lines.append(
            f"{row.waiting_weight:>11.2f}  {str(row.recommended_id):<22} "
            f"{row.recommended_score:>10.0f}  {row.note}"
        )
    lines.append("")

    # ---- window end policy ---------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append(
        "WINDOW END POLICY (D29) - same stop, same window, different meaning of 'closes at'"
    )
    lines.append(_SUBSEPARATOR)
    edge_stop = plan.stop_by_id(HEADLINE_STOP_IDS["edge_window"])
    lines.append(f"stop {edge_stop.id}: window {_window_text(edge_stop)}, service 20m")
    for policy, summary, feasible, message in window_end_policy_comparison():
        lines.append(f"  {policy.value:<26} feasible={'yes' if feasible else 'NO':<4} {summary}")
        if message:
            lines.append(f"  {'':<26} {message}")
    lines.append("")

    # ---- unknown hours caveat ------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append("DATA-QUALITY CAVEAT: what unknown business hours do to the objective")
    lines.append(_SUBSEPARATOR)
    lines.append(
        "Stops with no fixed window are candidates too; nothing is invented for them, so they "
        "price as travel only:"
    )
    lines.append(f"{'stop':<24} {'window':<20} {'travel':>7}  {'score':>10}  {'rank':>5}")
    for row in stops_without_fixed_window():
        rank_text = f"#{row.rank}" if row.rank is not None else "infeasible"
        lines.append(
            f"{row.stop_id:<24} {row.window_text:<20} {format_duration(row.travel):>7}  "
            f"{row.score:>10.0f}  {rank_text:>5}"
        )
    distortion = unknown_hours_distortion()
    lines.append("")
    lines.append(
        f"distortion check - the same stop ({distortion.stop_id}, "
        f"{format_duration(distortion.travel)} from the depot), only its hours differ:"
    )
    lines.append(
        f"  hours {distortion.window_text:<16} -> score {distortion.score_known:>10.0f}, "
        f"rank #{distortion.rank_known}"
    )
    lines.append(
        f"  hours {'unknown':<16} -> score {distortion.score_if_unknown:>10.0f}, "
        f"rank #{distortion.rank_if_unknown}"
    )
    lines.append(
        "Knowing nothing about a stop makes it look cheaper than every stop with hours, because "
        "there is no opening to wait for."
    )
    lines.append(
        "Unknown hours must be resolved before a route is trusted (spec sections 7 and 16)."
    )
    lines.append("")

    # ---- determinism ----------------------------------------------------- #
    lines.append(_SUBSEPARATOR)
    lines.append("DETERMINISM")
    lines.append(_SUBSEPARATOR)
    lines.append(f"inputs_fingerprint : {report.inputs_fingerprint}")
    lines.append(
        "Identical inputs give an identical ranking, score and fingerprint; the tie-break is "
        "(score, travel_time, stop_id) on this first-leg view."
    )
    lines.append(
        "NOTE: the recommendation engine itself ranks COMPLETE route outcomes "
        "(START -> candidate -> optimized remaining stops -> FINISH, v2 sections 12-14), and a "
        "candidate whose complete route misses a hard window is never ranked. This report still "
        "narrates the Stage 1 first-leg view; refreshing that narrative is a later unit."
    )
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the demo report. Returns a process exit code.

    A time zone database is required. When ``tzdata`` cannot be installed (offline machine), the
    standard ``PYTHONTZPATH`` mechanism works, or ``--allow-system-tzdata`` activates a discovered
    system TZif tree explicitly and prints a warning. Nothing falls back silently (D12).
    """
    parser = argparse.ArgumentParser(
        prog="demo.report",
        description="Print the RoutePilot demo scenario with candidate costs.",
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

    print(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
