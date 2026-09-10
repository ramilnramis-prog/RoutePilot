"""Deterministic demo dataset: ~30 realistic service stops (spec section 24).

**DEMO / SYNTHETIC DATA.** Addresses, coordinates, opening hours and priorities here are
invented for demonstration. They are not real customers, and the travel times derived from these
coordinates by :mod:`demo.synthetic_matrix` are not road routing.

The dataset is built to demonstrate the product's core scenario (spec section 1): departure at
04:00, many customers opening at 08:00, so the best first stop is decided by the trade-off between
driving time and useless waiting - never by "nearest" or "farthest" alone.

Coordinates are derived from an intended travel time: the demo matrix turns one coordinate degree
into one hour, so a stop authored at ``offset_minutes`` sits ``offset_minutes / 60`` degrees from
the warehouse. That is a demo construction device, not a model of a road network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from core.model.cost_policy import RouteCostPolicy, demo_provisional_policy
from core.model.first_stop import FirstStopIntent
from core.model.ids import PlanId
from core.model.route_plan import RoutePlan
from core.model.route_stop import GeocodeStatus, RouteStop
from core.model.service_window import (
    DEFAULT_WINDOW_END_POLICY,
    ServiceWindow,
    WindowEndPolicy,
)
from core.model.value_objects import GeoPoint, PlaceRef
from core.time import tz, tzdata

__all__ = [
    "DEMO_DEFAULT_SERVICE_DURATION",
    "DEMO_DEPARTURE_TIME",
    "DEMO_PLAN_ID",
    "DEMO_SERVICE_DATE",
    "DEMO_TIMEZONE",
    "DEMO_WARNING",
    "FINISH_POINT",
    "HEADLINE_STOP_IDS",
    "WAREHOUSE_POINT",
    "build_demo_plan",
    "demo_departure_time_at",
    "demo_warning_text",
]

DEMO_TIMEZONE = "Europe/Moscow"
DEMO_SERVICE_DATE = date(2026, 9, 11)
DEMO_PLAN_ID = "demo-route-01"

#: 04:00 Moscow on the demo service date, stored the way the domain stores time: UTC.
DEMO_DEPARTURE_TIME = datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)

#: Used for stops whose own service duration is unknown (flag: service_duration_defaulted).
DEMO_DEFAULT_SERVICE_DURATION = 900

WAREHOUSE_POINT = GeoPoint(55.75, 37.62)
FINISH_POINT = GeoPoint(55.70, 37.55)
WAREHOUSE_LABEL = "Warehouse (demo)"
FINISH_LABEL = "Central depot (demo, fixed)"
_LONGITUDE_RATIO = 0.35

DEMO_WARNING = "DEMO / SYNTHETIC DATA - not real addresses, not real opening hours, not real routing"

#: Stops the demo report and tests refer to by role.
HEADLINE_STOP_IDS = {
    "nearest": "S01-NEAR",
    "near_second": "S02-NEAR2",
    "mid": "S03-MID",
    "far_before_opening": "S04-FAR-BEFORE-OPEN",
    "farthest": "S05-FARTHEST",
    "tight_window": "S06-TIGHT-WINDOW",
    "edge_window": "S07-EDGE-WINDOW",
    "always_open": "S09-ALWAYS-OPEN",
    "unknown_hours": "S08-UNKNOWN-HOURS",
    "disabled": "S10-DISABLED",
    "on_opening": "S25-ON-OPENING",
}


def _at(open_hour: int, open_minute: int, close_hour: int, close_minute: int) -> ServiceWindow:
    return ServiceWindow.fixed(time(open_hour, open_minute), time(close_hour, close_minute))


# Shared window definitions (value objects are immutable, so they are safe to share).
_W_0800_1800 = _at(8, 0, 18, 0)
_W_0800_1700 = _at(8, 0, 17, 0)
_W_0830_1600 = _at(8, 30, 16, 0)
_W_0830_1800 = _at(8, 30, 18, 0)
_W_0900_1700 = _at(9, 0, 17, 0)
_W_0900_1800 = _at(9, 0, 18, 0)
_W_1000_1800 = _at(10, 0, 18, 0)
_W_0800_0830 = _at(8, 0, 8, 30)
_W_1000_1015 = _at(10, 0, 10, 15)
_UNKNOWN = ServiceWindow.unknown()
_ALWAYS = ServiceWindow.unrestricted()


@dataclass(frozen=True)
class _StopSpec:
    stop_id: str
    label: str
    offset_minutes: int
    window: ServiceWindow
    service_duration: int | None
    priority: int | None = None
    enabled: bool = True


#: 31 stops: 30 enabled + 1 disabled. Several open at 08:00, as in the core scenario.
_SPECS: tuple[_StopSpec, ...] = (
    _StopSpec("S01-NEAR", "Demo customer 01 (nearest)", 20, _W_0800_1800, 15 * 60),
    _StopSpec("S02-NEAR2", "Demo customer 02", 45, _W_0800_1800, 10 * 60),
    _StopSpec("S03-MID", "Demo customer 03", 70, _W_0800_1700, 10 * 60),
    _StopSpec("S04-FAR-BEFORE-OPEN", "Demo customer 04 (3h55m away)", 235, _W_0800_1800, 10 * 60),
    _StopSpec("S05-FARTHEST", "Demo customer 05 (5h away)", 300, _W_0800_1800, 10 * 60),
    _StopSpec("S06-TIGHT-WINDOW", "Demo customer 06 (08:00-08:30)", 300, _W_0800_0830, 10 * 60),
    _StopSpec("S07-EDGE-WINDOW", "Demo customer 07 (10:00-10:15)", 365, _W_1000_1015, 20 * 60),
    _StopSpec("S08-UNKNOWN-HOURS", "Demo customer 08 (hours unknown)", 265, _UNKNOWN, 10 * 60),
    _StopSpec("S09-ALWAYS-OPEN", "Demo customer 09 (always accessible)", 250, _ALWAYS, 10 * 60),
    _StopSpec(
        "S10-DISABLED",
        "Demo customer 10 (disabled)",
        120,
        _W_0800_1800,
        10 * 60,
        enabled=False,
    ),
    _StopSpec("S11-OPEN-0900", "Demo customer 11 (opens 09:00)", 30, _W_0900_1800, 10 * 60, 1),
    _StopSpec("S12-OPEN-0900B", "Demo customer 12 (opens 09:00)", 55, _W_0900_1800, 15 * 60),
    _StopSpec("S13-OPEN-0830", "Demo customer 13 (opens 08:30)", 80, _W_0830_1600, 10 * 60),
    _StopSpec("S14-PRIORITY-2", "Demo customer 14", 100, _W_0800_1800, 20 * 60, 2),
    _StopSpec("S15-115", "Demo customer 15", 115, _W_0800_1800, 10 * 60),
    _StopSpec("S16-130", "Demo customer 16", 130, _W_0800_1800, 10 * 60),
    _StopSpec("S17-OPEN-0900", "Demo customer 17 (opens 09:00)", 145, _W_0900_1700, 15 * 60),
    # No service_duration: the plan default applies and the timeline is flagged accordingly.
    _StopSpec("S18-NO-DURATION", "Demo customer 18 (duration unknown)", 160, _W_0800_1800, None),
    _StopSpec("S19-ALWAYS-OPEN2", "Demo customer 19 (always accessible)", 285, _ALWAYS, 10 * 60),
    _StopSpec("S20-180", "Demo customer 20", 180, _W_0800_1800, 10 * 60),
    _StopSpec("S21-PRIORITY-1", "Demo customer 21 (opens 08:30)", 195, _W_0830_1800, 15 * 60, 1),
    _StopSpec("S22-205", "Demo customer 22", 205, _W_0800_1800, 10 * 60),
    _StopSpec("S23-UNKNOWN-HOURS2", "Demo customer 23 (hours unknown)", 275, _UNKNOWN, 10 * 60),
    _StopSpec("S24-OPEN-0900", "Demo customer 24 (opens 09:00)", 220, _W_0900_1800, 10 * 60),
    _StopSpec("S25-ON-OPENING", "Demo customer 25 (arrives at opening)", 240, _W_0800_1800, 10 * 60),
    _StopSpec("S26-250", "Demo customer 26", 250, _W_0800_1800, 20 * 60),
    _StopSpec("S27-OPEN-1000", "Demo customer 27 (opens 10:00)", 260, _W_1000_1800, 10 * 60),
    _StopSpec("S28-PRIORITY-3", "Demo customer 28", 270, _W_0800_1800, 10 * 60, 3),
    _StopSpec("S29-280", "Demo customer 29", 280, _W_0800_1800, 15 * 60),
    _StopSpec("S30-OPEN-0900", "Demo customer 30 (opens 09:00)", 290, _W_0900_1800, 10 * 60),
    _StopSpec("S31-245", "Demo customer 31", 245, _W_0800_1800, 10 * 60),
)


def _point_for_offset(offset_minutes: float) -> GeoPoint:
    """Synthetic coordinates at a chosen synthetic travel time from the warehouse."""
    degree = offset_minutes / 60.0
    return GeoPoint(
        WAREHOUSE_POINT.latitude + degree,
        WAREHOUSE_POINT.longitude + degree * _LONGITUDE_RATIO,
    )


def _build_stop(spec: _StopSpec) -> RouteStop:
    return RouteStop(
        id=spec.stop_id,
        raw_address=f"{spec.label}, demo district {spec.offset_minutes // 10}",
        normalized_address=f"{spec.label}, demo region, synthetic coordinates",
        latitude=_point_for_offset(spec.offset_minutes).latitude,
        longitude=_point_for_offset(spec.offset_minutes).longitude,
        geocode_status=GeocodeStatus.RESOLVED,
        service_window=spec.window,
        service_duration=spec.service_duration,
        priority=spec.priority,
        enabled=spec.enabled,
    )


def build_demo_plan(
    *,
    departure_time: datetime | None = None,
    window_end_policy: WindowEndPolicy = DEFAULT_WINDOW_END_POLICY,
    cost_policy: RouteCostPolicy | None = None,
    plan_id: str = DEMO_PLAN_ID,
) -> RoutePlan:
    """Build the demo plan. Deterministic: identical arguments give an identical plan."""
    return RoutePlan(
        id=PlanId(plan_id),
        timezone=DEMO_TIMEZONE,
        departure=PlaceRef(WAREHOUSE_LABEL, WAREHOUSE_POINT),
        departure_time=departure_time if departure_time is not None else DEMO_DEPARTURE_TIME,
        finish=PlaceRef(FINISH_LABEL, FINISH_POINT),
        stops=tuple(_build_stop(spec) for spec in _SPECS),
        cost_policy=cost_policy if cost_policy is not None else demo_provisional_policy(),
        window_end_policy=window_end_policy,
        default_service_duration=DEMO_DEFAULT_SERVICE_DURATION,
        first_service_stop=FirstStopIntent.auto(),
    )


def demo_departure_time_at(local_hour: int, local_minute: int = 0) -> datetime:
    """Departure instant for a local Moscow wall-clock time on the demo service date (UTC).

    Resolved through the plan's IANA zone, so it stays correct under any future DST rule.
    """
    zone = tzdata.load_timezone(DEMO_TIMEZONE)
    return tz.resolve_local_datetime(
        DEMO_SERVICE_DATE, time(local_hour, local_minute), zone
    )


def demo_warning_text() -> str:
    """One-line provenance warning for reports and UI."""
    return DEMO_WARNING
