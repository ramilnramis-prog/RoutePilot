"""Timeline arithmetic (spec section 7).

For every stop in an order this computes, deterministically:

    departure time from previous location -> travel time -> ETA -> opening time ->
    waiting time -> service start -> service duration -> estimated departure ->
    lateness -> time-window feasibility

Two rules come straight from the decisions and are enforced by
:class:`~core.model.solution.StopTimeline` itself:

* service can never start before the window opens (``service_start = max(arrival, open)``);
* a **hard** window miss is an explicit :class:`Violation`, not a penalty. ``lateness > 0`` means
  exactly "service cannot begin inside the permitted window" and makes the stop infeasible
  (D13 amendment). Service that starts in time but finishes after closing records ``overtime`` as
  information only - soft windows do not exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from core.engine.providers import TravelTimeProvider
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.service_window import WindowKind
from core.model.solution import (
    Feasibility,
    StopTimeline,
    TimelineFlag,
    Violation,
    ViolationKind,
)
from core.model.value_objects import DurationSec
from core.time import tz
from core.validation.errors import (
    InvalidRoutePlanError,
    MissingServiceDurationError,
    StopNotGeocodedError,
)

__all__ = ["TimelineResult", "compute_timeline", "resolve_service_duration"]


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


def compute_timeline(
    *,
    plan: RoutePlan,
    order: Sequence[StopId],
    travel_provider: TravelTimeProvider,
) -> TimelineResult:
    """Compute the timeline for ``order``.

    Raises:
        InvalidOrderError: if ``order`` is not exactly the enabled stops (via the plan).
        StopNotGeocodedError: if a routed stop has no coordinates.
        MissingServiceDurationError: if a stop's duration is unknown and the plan has no default.
        DSTValidationError: if a fixed window falls into a DST gap or is ambiguous (D3).
    """
    plan.validate_order(order)
    tzinfo = plan.load_timezone()

    previous_point = plan.departure_point
    previous_departure = plan.departure_time

    timelines: list[StopTimeline] = []
    violations: list[Violation] = []

    for stop_id in order:
        stop = plan.stop_by_id(stop_id)
        location = stop.location
        if location is None:
            raise StopNotGeocodedError(stop.id, stop.geocode_status.value)

        service_duration, used_plan_default = resolve_service_duration(stop, plan)

        travel_time = travel_provider.travel_time_seconds(previous_point, location)
        if not isinstance(travel_time, int) or isinstance(travel_time, bool):
            raise InvalidRoutePlanError(
                f"travel time provider returned {travel_time!r}; whole seconds are required"
            )
        if travel_time < 0:
            raise InvalidRoutePlanError(
                f"travel time provider returned a negative duration ({travel_time}s)"
            )

        estimated_arrival = previous_departure + timedelta(seconds=travel_time)

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
            waiting_time = max(0, int((window_start - estimated_arrival).total_seconds()))
        else:
            window_start = None
            window_end = None
            waiting_time = 0

        # Hard rule: service never begins before the window opens.
        service_start = estimated_arrival + timedelta(seconds=waiting_time)
        estimated_departure = service_start + timedelta(seconds=service_duration)

        if window_end is not None:
            lateness = max(0, int((service_start - window_end).total_seconds()))
            overtime = max(0, int((estimated_departure - window_end).total_seconds()))
        else:
            lateness = 0
            overtime = 0

        feasibility = Feasibility.INFEASIBLE if lateness > 0 else Feasibility.FEASIBLE

        timelines.append(
            StopTimeline(
                stop_id=stop.id,
                departure_from_previous=previous_departure,
                travel_time=travel_time,
                estimated_arrival=estimated_arrival,
                window_kind=stop.service_window.window_kind,
                service_window_start=window_start,
                service_window_end=window_end,
                waiting_time=waiting_time,
                service_start=service_start,
                service_duration=service_duration,
                estimated_departure=estimated_departure,
                lateness=lateness,
                overtime=overtime,
                feasibility=feasibility,
                flags=tuple(flags),
            )
        )

        if feasibility is Feasibility.INFEASIBLE:
            assert window_end is not None
            violations.append(
                Violation(
                    stop_id=stop.id,
                    kind=ViolationKind.TIME_WINDOW_INFEASIBLE,
                    message=(
                        "Service cannot begin within the permitted window: earliest possible "
                        f"service start is {service_start.isoformat()}, window closes at "
                        f"{window_end.isoformat()} (plan time zone {plan.timezone})."
                    ),
                    service_start=service_start,
                    service_window_end=window_end,
                )
            )

        previous_point = location
        previous_departure = estimated_departure

    return TimelineResult(timelines=tuple(timelines), violations=tuple(violations))
