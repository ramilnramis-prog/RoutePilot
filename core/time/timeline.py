"""Timeline arithmetic (spec section 7, decisions D13 and D29).

For one stop this computes, deterministically:

    departure time from previous location -> travel time -> ETA -> opening time ->
    waiting time -> service start -> service duration -> estimated departure ->
    lateness -> time-window feasibility

Three rules come from the decisions and are enforced by
:class:`~core.model.solution.StopTimeline` itself:

* service can never start before the window opens (``service_start = max(arrival, open)``);
* the meaning of the window **end** is explicit, not assumed (D29): under the default
  ``service_finish_before_end`` service must be over by closing; under
  ``service_start_before_end`` beginning service before closing is enough and running past
  closing is recorded as ``finish_overtime`` only;
* a missed **hard** window is an explicit :class:`Violation`, never a penalty folded into a
  score (D13 amendment).

The arithmetic lives in :func:`compute_stop_timeline`, which is used both by the chained
:func:`compute_timeline` and by first-stop candidate evaluation, so a candidate's first leg and a
real route leg can never disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

from core.engine.providers import TravelTimeProvider
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.service_window import WindowEndPolicy, WindowKind
from core.model.solution import (
    Feasibility,
    StopTimeline,
    TimelineFlag,
    Violation,
    ViolationKind,
)
from core.model.value_objects import DurationSec, GeoPoint, Instant
from core.time import tz
from core.validation.errors import (
    InvalidRoutePlanError,
    MissingServiceDurationError,
    StopNotGeocodedError,
)

__all__ = [
    "TimelineResult",
    "compute_stop_timeline",
    "compute_timeline",
    "resolve_service_duration",
    "violation_for",
]


@dataclass(frozen=True)
class TimelineResult:
    """Timelines for an ordered route plus the explicit violations they produced."""

    timelines: tuple[StopTimeline, ...]
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def has_infeasible_windows(self) -> bool:
        return bool(self.violations)

    @property
    def infeasible_stop_ids(self) -> tuple[StopId, ...]:
        return tuple(timeline.stop_id for timeline in self.timelines if timeline.is_infeasible)

    @property
    def total_waiting_sec(self) -> DurationSec:
        return sum(timeline.waiting_time for timeline in self.timelines)


def resolve_service_duration(stop: RouteStop, plan: RoutePlan) -> tuple[DurationSec, bool]:
    """Service duration of a stop, and whether the plan default had to be used.

    Unknown duration is never invented: it falls back to the plan's explicit default, and if
    there is none the route cannot be computed.
    """
    if stop.service_duration is not None:
        return stop.service_duration, False
    if plan.default_service_duration is not None:
        return plan.default_service_duration, True
    raise MissingServiceDurationError(stop.id)


def compute_stop_timeline(
    *,
    plan: RoutePlan,
    stop: RouteStop,
    departure_from_previous: Instant,
    previous_point: GeoPoint,
    travel_provider: TravelTimeProvider,
    tzinfo: ZoneInfo,
) -> StopTimeline:
    """Time a single leg: from ``previous_point`` at ``departure_from_previous`` to ``stop``.

    Raises:
        StopNotGeocodedError: the stop has no coordinates.
        MissingServiceDurationError: duration unknown and the plan has no default.
        DSTValidationError: a fixed window falls into a DST gap or is ambiguous (D3).
    """
    location = stop.location
    if location is None:
        raise StopNotGeocodedError(stop.id, stop.geocode_status.value)

    service_duration, used_plan_default = resolve_service_duration(stop, plan)

    travel_time = travel_provider.travel_time_seconds(previous_point, location)
    if isinstance(travel_time, bool) or not isinstance(travel_time, int):
        raise InvalidRoutePlanError(
            f"travel time provider returned {travel_time!r}; whole seconds are required"
        )
    if travel_time < 0:
        raise InvalidRoutePlanError(
            f"travel time provider returned a negative duration ({travel_time}s)"
        )

    estimated_arrival = departure_from_previous + timedelta(seconds=travel_time)

    flags: list[TimelineFlag] = []
    if stop.service_window.window_kind is WindowKind.UNKNOWN:
        flags.append(TimelineFlag.WINDOW_UNKNOWN)
    if used_plan_default:
        flags.append(TimelineFlag.SERVICE_DURATION_DEFAULTED)

    # The service date is the local date of arrival: that is the day whose hours apply.
    service_date = tz.local_date_of(estimated_arrival, tzinfo)
    resolved_window = tz.resolve_service_window(stop.service_window, service_date, tzinfo)

    if resolved_window is not None:
        window_start = resolved_window.open_at
        window_end = resolved_window.close_at
        end_policy: WindowEndPolicy | None = stop.service_window.effective_end_policy(
            plan.window_end_policy
        )
        waiting_time = max(0, int((window_start - estimated_arrival).total_seconds()))
    else:
        window_start = None
        window_end = None
        end_policy = None
        waiting_time = 0

    # Hard rule: service never begins before the window opens.
    service_start = estimated_arrival + timedelta(seconds=waiting_time)
    estimated_departure = service_start + timedelta(seconds=service_duration)

    if window_end is not None:
        start_miss = max(0, int((service_start - window_end).total_seconds()))
        finish_overtime = max(0, int((estimated_departure - window_end).total_seconds()))
        # D29: the policy decides which miss makes the stop infeasible.
        if end_policy is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
            lateness = finish_overtime
        else:
            lateness = start_miss
    else:
        lateness = 0
        finish_overtime = 0

    feasibility = Feasibility.INFEASIBLE if lateness > 0 else Feasibility.FEASIBLE

    return StopTimeline(
        stop_id=stop.id,
        departure_from_previous=departure_from_previous,
        travel_time=travel_time,
        estimated_arrival=estimated_arrival,
        window_kind=stop.service_window.window_kind,
        service_window_start=window_start,
        service_window_end=window_end,
        window_end_policy=end_policy,
        waiting_time=waiting_time,
        service_start=service_start,
        service_duration=service_duration,
        estimated_departure=estimated_departure,
        lateness=lateness,
        finish_overtime=finish_overtime,
        feasibility=feasibility,
        flags=tuple(flags),
    )


def violation_for(timeline: StopTimeline) -> Violation | None:
    """The explicit violation of an infeasible timeline, or ``None`` when it is feasible."""
    if not timeline.is_infeasible:
        return None

    window_end = timeline.service_window_end
    assert window_end is not None  # infeasibility requires a fixed window
    if timeline.window_end_policy is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
        detail = (
            f"service would finish at {timeline.estimated_departure.isoformat()}, "
            f"{timeline.finish_overtime}s after the window closes at {window_end.isoformat()} "
            f"(policy {timeline.window_end_policy.value}: service must finish before closing)"
        )
    else:
        detail = (
            f"earliest possible service start is {timeline.service_start.isoformat()}, "
            f"{timeline.lateness}s after the window closes at {window_end.isoformat()} "
            f"(policy {timeline.window_end_policy.value}: service must start before closing)"
        )
    return Violation(
        stop_id=timeline.stop_id,
        kind=ViolationKind.TIME_WINDOW_INFEASIBLE,
        message=f"Service cannot be served within the permitted window: {detail}.",
        service_start=timeline.service_start,
        service_window_end=window_end,
    )


def compute_timeline(
    *,
    plan: RoutePlan,
    order: Sequence[StopId],
    travel_provider: TravelTimeProvider,
) -> TimelineResult:
    """Compute the timeline for a complete ``order``.

    Raises:
        InvalidOrderError: if ``order`` is not exactly the enabled stops (via the plan).
    """
    plan.validate_order(order)
    tzinfo = plan.load_timezone()

    previous_point = plan.departure_point
    previous_departure = plan.departure_time
    timelines: list[StopTimeline] = []

    for stop_id in order:
        stop = plan.stop_by_id(stop_id)
        timeline = compute_stop_timeline(
            plan=plan,
            stop=stop,
            departure_from_previous=previous_departure,
            previous_point=previous_point,
            travel_provider=travel_provider,
            tzinfo=tzinfo,
        )
        timelines.append(timeline)
        location = stop.location
        assert location is not None  # compute_stop_timeline raises otherwise
        previous_point = location
        previous_departure = timeline.estimated_departure

    violations = tuple(
        violation for violation in (violation_for(t) for t in timelines) if violation is not None
    )
    return TimelineResult(timelines=tuple(timelines), violations=violations)
