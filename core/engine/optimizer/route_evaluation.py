"""Complete-route evaluation (Stage 2, spec sections 12, 15, 21, 30).

The single place where an order becomes a **complete route**:

    START -> the given enabled stops -> FINISH

Two rules carry the weight:

* every service leg is timed by the authoritative engine arithmetic,
  :func:`core.time.timeline.compute_stop_timeline`, so the optimizer cannot grow a second,
  drifting copy of window/waiting arithmetic;
* the last leg to FINISH is **part** of the route (v2 section 15): its travel time and
  distance are accumulated and ``finish_arrival`` records when the driver is done.

``duration_sec`` therefore always equals ``travel_sec + waiting_sec + service_sec`` over the
whole route - never a partial figure, and never a figure inflated by a numeric penalty. Hard
window misses stay explicit :class:`~core.model.solution.Violation` data (D13 amendment).

No heuristic lives here: this module evaluates an order it is given. The greedy seed and the
local improvement that *produce* an order are Stage 2 work of their own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from core.engine.providers import TravelMatrix
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.solution import (
    BaselineKind,
    RouteMetrics,
    RouteSolution,
    SolutionStatus,
    StopTimeline,
    Violation,
)
from core.model.value_objects import DurationSec, Instant, ensure_utc
from core.time import timeline as timeline_engine
from core.validation.errors import InvalidRoutePlanError

__all__ = [
    "RouteEvaluation",
    "build_solution",
    "evaluate_order",
    "user_baseline_order",
]


@dataclass(frozen=True)
class RouteEvaluation:
    """One evaluated complete route: the order, its timelines, its violations, its metrics."""

    order: tuple[StopId, ...]
    timelines: tuple[StopTimeline, ...]
    violations: tuple[Violation, ...]
    metrics: RouteMetrics
    feasible: bool

    def __post_init__(self) -> None:
        order = tuple(self.order)
        timelines = tuple(self.timelines)
        violations = tuple(self.violations)
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "timelines", timelines)
        object.__setattr__(self, "violations", violations)

        if len(order) != len(timelines):
            raise InvalidRoutePlanError(
                "order and timelines must have the same length: every ordered stop has exactly "
                "one timeline"
            )
        if tuple(timeline.stop_id for timeline in timelines) != order:
            raise InvalidRoutePlanError("timelines must follow the order of the route")

        infeasible = any(timeline.is_infeasible for timeline in timelines)
        if infeasible != bool(violations):
            raise InvalidRoutePlanError(
                "an infeasible timeline carries exactly one explicit violation: infeasibility is "
                "reported as data, never implied (D13 amendment)"
            )
        if self.feasible != self.metrics.feasible:
            raise InvalidRoutePlanError(
                "evaluation feasibility and metrics feasibility must agree: one route has one "
                "answer (v2 section 14)"
            )

    # ---- queries ------------------------------------------------------- #
    @property
    def finishes_at(self) -> Instant:
        """When the driver reaches FINISH, last service leg included (v2 section 15)."""
        return self.metrics.finish_arrival

    @property
    def infeasible_stop_ids(self) -> tuple[StopId, ...]:
        return tuple(
            timeline.stop_id for timeline in self.timelines if timeline.is_infeasible
        )


def evaluate_order(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    order: Sequence[StopId],
) -> RouteEvaluation:
    """Evaluate ``order`` as a complete route, FINISH leg included.

    Every service leg is timed by :func:`core.time.timeline.compute_stop_timeline`; the final
    leg from the last service stop (or from the departure location when ``order`` is empty) to
    ``plan.finish_point`` is added on top and recorded in ``finish_arrival``.

    Raises:
        InvalidOrderError: ``order`` is not exactly the enabled stops, each exactly once, with
            no START, no FINISH and no disabled stop (via :meth:`RoutePlan.validate_order`).
        StopNotGeocodedError: a stop that must be routed has no coordinates.
        MissingServiceDurationError: a stop has no duration and the plan has no default.
    """
    plan.validate_order(order)

    tzinfo = plan.load_timezone()
    previous_point = plan.departure_point
    previous_departure = plan.departure_time

    timelines: list[StopTimeline] = []
    violations: list[Violation] = []
    distance_m = 0.0
    travel_sec = 0
    waiting_sec = 0
    service_sec = 0

    for stop_id in order:
        stop = plan.stop_by_id(stop_id)
        timeline = timeline_engine.compute_stop_timeline(
            plan=plan,
            stop=stop,
            departure_from_previous=previous_departure,
            previous_point=previous_point,
            travel_provider=travel_matrix,
            tzinfo=tzinfo,
        )
        location = stop.location
        assert location is not None  # compute_stop_timeline raises otherwise

        timelines.append(timeline)
        distance_m += float(travel_matrix.distance_meters(previous_point, location))
        travel_sec += timeline.travel_time
        waiting_sec += timeline.waiting_time
        service_sec += timeline.service_duration

        violation = timeline_engine.violation_for(timeline)
        if violation is not None:
            violations.append(violation)

        previous_point = location
        previous_departure = timeline.estimated_departure

    # The FINISH leg is part of the complete route (v2 section 15): both the metrics and the
    # recorded finish arrival must include it.
    finish_travel_sec = travel_matrix.travel_time_seconds(previous_point, plan.finish_point)
    if isinstance(finish_travel_sec, bool) or not isinstance(finish_travel_sec, int):
        raise InvalidRoutePlanError(
            f"travel time provider returned {finish_travel_sec!r}; whole seconds are required"
        )
    if finish_travel_sec < 0:
        raise InvalidRoutePlanError(
            f"travel time provider returned a negative duration ({finish_travel_sec}s)"
        )
    finish_arrival = ensure_utc(
        previous_departure + timedelta(seconds=finish_travel_sec), field_name="finish_arrival"
    )
    travel_sec += finish_travel_sec
    distance_m += float(travel_matrix.distance_meters(previous_point, plan.finish_point))

    feasible = not violations
    metrics = RouteMetrics(
        distance_m=distance_m,
        duration_sec=travel_sec + waiting_sec + service_sec,
        waiting_sec=waiting_sec,
        travel_sec=travel_sec,
        service_sec=service_sec,
        finish_arrival=finish_arrival,
        feasible=feasible,
    )
    return RouteEvaluation(
        order=tuple(order),
        timelines=tuple(timelines),
        violations=tuple(violations),
        metrics=metrics,
        feasible=feasible,
    )


def user_baseline_order(plan: RoutePlan) -> tuple[StopId, ...]:
    """The user-facing BEFORE route order (v2 section 30, D33).

    ``START -> enabled stops in their immutable ``input_position`` order -> FINISH``. Disabled
    stops are omitted without renumbering the rest, and the result never depends on which first
    stop the driver selected: the baseline answers "what would the driver have done anyway?".
    """
    return plan.user_baseline_order()


def algorithm_baseline_order(
    plan: RoutePlan, order: Sequence[StopId]
) -> tuple[StopId, ...]:
    """The internal algorithm baseline: the supplied order, with a fixed first stop honoured.

    The baseline is labelled :attr:`BaselineKind.ALGORITHM_GREEDY` and is never presented as the
    user's "before" route (D22). ``order`` is the greedy seed produced by the solver (later
    unit); when it does not begin with the driver's selected first stop, that stop is moved to
    the front with the rest of the order preserved, so the baseline is a comparison figure for a
    route the optimizer is actually allowed to build (I3).
    """
    sequence = list(order)
    selected = plan.first_service_stop.selected_stop_id
    if selected is None or not sequence or sequence[0] == selected:
        return tuple(sequence)
    if selected not in sequence:
        raise InvalidRoutePlanError(
            f"algorithm baseline order does not contain the selected first stop {selected!r}"
        )
    return (selected, *(stop_id for stop_id in sequence if stop_id != selected))


def build_solution(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    order: Sequence[StopId],
    algorithm_order: Sequence[StopId],
) -> RouteSolution:
    """Commit the completed route of ``order`` together with both comparison baselines.

    ``order`` is the final route the optimizer produced; ``algorithm_order`` is the greedy seed
    supplied by the solver, kept for optimizer-quality diagnostics only (D17/D22).

    Raises:
        InvalidRoutePlanError: the plan has no driver-selected first service stop (a committed
            route requires an explicit choice - I4/D32), or ``order`` does not begin with it
            (I3: a selected first stop survives any optimization unchanged).
        InvalidOrderError: either order is not exactly the enabled stops, each exactly once.
    """
    selected = plan.first_service_stop.selected_stop_id
    if selected is None:
        raise InvalidRoutePlanError(
            "a committed route requires an explicit driver-selected first service stop: the plan "
            "is 'awaiting_first_stop_choice', which is a valid state, not an error (D4/I4, D32)"
        )

    order = tuple(order)
    if not order or order[0] != selected:
        first = order[0] if order else None
        raise InvalidRoutePlanError(
            f"the committed route must begin with the driver-selected first stop {selected!r}; "
            f"got {first!r}. A driver-selected first stop is never reordered by optimization "
            "(I3, D10)."
        )

    evaluation = evaluate_order(plan=plan, travel_matrix=travel_matrix, order=order)
    baseline = evaluate_order(
        plan=plan,
        travel_matrix=travel_matrix,
        order=user_baseline_order(plan),
    )
    algorithm = evaluate_order(
        plan=plan,
        travel_matrix=travel_matrix,
        order=algorithm_baseline_order(plan, algorithm_order),
    )

    status = (
        SolutionStatus.HAS_INFEASIBLE_WINDOWS if evaluation.violations else SolutionStatus.OK
    )
    return RouteSolution(
        order=evaluation.order,
        timelines=evaluation.timelines,
        metrics=evaluation.metrics,
        status=status,
        first_service_stop=plan.first_service_stop,
        provenance=travel_matrix.provenance,
        inputs_fingerprint=plan.inputs_fingerprint(),
        recommendation=None,
        user_baseline=_labelled(baseline.metrics, BaselineKind.USER_SUPPLIED),
        algorithm_baseline=_labelled(algorithm.metrics, BaselineKind.ALGORITHM_GREEDY),
        violations=evaluation.violations,
    )


def _labelled(metrics: RouteMetrics, baseline_kind: BaselineKind) -> RouteMetrics:
    """The same measurements, labelled with the baseline they belong to (D22)."""
    return RouteMetrics(
        distance_m=metrics.distance_m,
        duration_sec=metrics.duration_sec,
        waiting_sec=metrics.waiting_sec,
        travel_sec=metrics.travel_sec,
        service_sec=metrics.service_sec,
        finish_arrival=metrics.finish_arrival,
        feasible=metrics.feasible,
        baseline_kind=baseline_kind,
    )
