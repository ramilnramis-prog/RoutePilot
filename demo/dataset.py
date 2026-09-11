"""Deterministic demo dataset: ~30 realistic service stops (spec section 24).

**DEMO / SYNTHETIC DATA.** Addresses, coordinates, opening hours and priorities here are
invented for demonstration. They are not real customers, and the travel times derived from these
coordinates by :mod:`demo.synthetic_matrix` are not road routing.

The dataset is built to demonstrate the product's core scenario (spec section 1 and v2 section 33):
departure at 04:00, many customers opening at 08:00, so the strongest first stop is decided by the
**complete route** - how much of the day is spent driving productively before the openings, how
much is spent waiting, and whether the whole day still fits inside the customers' hard windows.
It is deliberately *not* decided by "nearest" or "farthest" alone.

Coordinates are derived from an intended travel time: the demo matrix turns one coordinate degree
into one hour, so a stop authored ``north_minutes`` north and ``east_minutes`` east of the
warehouse sits that many sixtieths of a degree away, and the synthetic travel between two stops is
``max(north difference, east difference)`` minutes. That is a demo construction device, not a model
of a road network.

Why the day is calibrated the way it is (this is the part that makes the demo *work*):

* the 04:00 -> 08:00 gap is **240 minutes**, and the whole service area spans about 7 to 132
  synthetic minutes, so no candidate can drive the gap away: every complete route contains real
  waiting. That keeps the demo honest - the ranking is decided by the complete route's driving
  **and** waiting, not by an artefact of a world so large that one candidate happens to arrive at
  08:00 exactly;
* the short service durations (4-5 minutes, plus the 10-minute plan default for the one stop whose
  duration is unknown) keep the whole 31-stop enabled route inside one working day. With long
  services the
  day cannot be served inside the closing times at all, and the optimizer would spill the route
  into the next day's windows, which is not the scenario the demo is about;
* the opening times are mixed (08:00, 08:30, 09:00, 10:00) and the farthest customer opens at
  10:00, so "drive as far as possible before opening" stops paying after a point: the farthest
  candidate waits longest of all, and the strongest complete route starts at a customer that is
  neither the nearest nor the farthest;
* closing times are **19:00-20:00**, so that a route which wastes the morning waiting can still be
  served - it is simply a worse complete route, not an infeasible one;
* one customer (`S32-EARLY-CLOSE`) closes at **10:00**, and that deadline is the demo's
  **feasibility bottleneck**. A complete route can only serve it if it reaches that customer in its
  first two hours, which is exactly what a candidate that can drive productively before 08:00 does:
  the 26 feasible candidates reach it between 08:00 and 09:56 (`S25-ON-OPENING` at 09:26), while the
  five candidates whose first leg leaves them near the warehouse until 08:00 reach it at 12:27-13:36
  and are **rejected** with `S32-EARLY-CLOSE` named as the violating stop (v2 section 14). Without
  that early closing time every candidate would be feasible and the report would have no rejection
  diagnostics to show at all;
* the same deadline is also why the **USER baseline is infeasible**: the input order is a
  nearest-first work list, so it visits this customer 17th and reaches it at 13:23 - a route a
  driver could really have entered, and exactly the sort of route the optimizer exists to fix. This
  is not an accident of the calibration and it cannot be avoided by moving the deadline: the input
  order serves the bottleneck *after* the five rejected candidates do, so any deadline late enough
  to keep the input order feasible also makes every candidate feasible (measured: 08:00-13:30 gives
  31 feasible / 0 rejected, 08:00-13:00 gives 31 / 0, 08:00-10:00 gives 26 / 5). The demo therefore
  demonstrates the rejection diagnostics of v2 section 14 *and* an infeasible BEFORE route that the
  optimizer turns into a fully feasible AFTER route - instead of a feasibility-free fixture in which
  the section 14 machinery is never exercised;
* the previous calibration (closing times 18:00-20:00) could not serve the day at all and produced
  `no_fully_feasible_route` for all 30 candidates.

The fixture is deterministic: identical arguments always produce an identical plan and an identical
:meth:`core.model.route_plan.RoutePlan.inputs_fingerprint`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from core.model.cost_policy import RouteCostPolicy, smart_route_elapsed_policy
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
    "intended_travel_minutes",
]

DEMO_TIMEZONE = "Europe/Moscow"
DEMO_SERVICE_DATE = date(2026, 9, 11)
DEMO_PLAN_ID = "demo-route-01"

#: 04:00 Moscow on the demo service date, stored the way the domain stores time: UTC.
DEMO_DEPARTURE_TIME = datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)

#: Used for stops whose own service duration is unknown (flag: service_duration_defaulted).
DEMO_DEFAULT_SERVICE_DURATION = 600

WAREHOUSE_POINT = GeoPoint(55.75, 37.62)
FINISH_POINT = GeoPoint(55.70, 37.55)
WAREHOUSE_LABEL = "Warehouse (demo)"
FINISH_LABEL = "Central depot (demo, fixed)"

DEMO_WARNING = "DEMO / SYNTHETIC DATA - not real addresses, not real opening hours, not real routing"

#: Stops the demo report and tests refer to by role.
HEADLINE_STOP_IDS = {
    "nearest": "S01-NEAR",
    "near_second": "S02-NEAR2",
    "mid": "S03-MID",
    "far_before_opening": "S04-FAR-BEFORE-OPEN",
    "farthest": "S05-FARTHEST",
    "window_mid": "S06-WINDOW-MID",
    "late_opener": "S07-OPEN-1000",
    "unknown_hours": "S08-UNKNOWN-HOURS",
    "always_open": "S09-ALWAYS-OPEN",
    "disabled": "S10-DISABLED",
    "on_opening": "S25-ON-OPENING",
    "early_close": "S32-EARLY-CLOSE",
}


def _at(open_hour: int, open_minute: int, close_hour: int, close_minute: int) -> ServiceWindow:
    return ServiceWindow.fixed(time(open_hour, open_minute), time(close_hour, close_minute))


# Shared window definitions (value objects are immutable, so they are safe to share).
_W_0800_1900 = _at(8, 0, 19, 0)
_W_0800_2000 = _at(8, 0, 20, 0)
_W_0830_2000 = _at(8, 30, 20, 0)
_W_0900_2000 = _at(9, 0, 20, 0)
_W_1000_2000 = _at(10, 0, 20, 0)
#: The demo's feasibility bottleneck: a customer that closes at 10:00 (see the module docstring).
_W_0800_1000 = _at(8, 0, 10, 0)
_UNKNOWN = ServiceWindow.unknown()
_ALWAYS = ServiceWindow.unrestricted()


@dataclass(frozen=True)
class _StopSpec:
    """One demo stop: where it sits (in synthetic travel minutes) and what it requires."""

    stop_id: str
    label: str
    north_minutes: int
    east_minutes: int
    window: ServiceWindow
    service_duration: int | None
    priority: int | None = None
    enabled: bool = True

    @property
    def travel_minutes(self) -> int:
        """The intended first-leg travel from the warehouse, in synthetic minutes."""
        return max(abs(self.north_minutes), abs(self.east_minutes))


#: 32 stops: 31 enabled + 1 disabled. Many open at 08:00, as in the core scenario, and the
#: authored first-leg travel spreads them from 7 to 132 synthetic minutes.
_SPECS: tuple[_StopSpec, ...] = (
    _StopSpec("S01-NEAR", "Demo customer 01 (nearest, 7m away)", 7, 2, _W_0800_2000, 240),
    _StopSpec("S02-NEAR2", "Demo customer 02 (16m away)", 16, -5, _W_0800_1900, 240),
    _StopSpec("S03-MID", "Demo customer 03 (31m away)", 31, 8, _W_0800_1900, 240),
    _StopSpec("S04-FAR-BEFORE-OPEN", "Demo customer 04 (1h58m away)", 118, 11, _W_0800_2000, 240),
    _StopSpec("S05-FARTHEST", "Demo customer 05 (farthest, 2h12m away, opens 10:00)", 132, 0,
              _W_1000_2000, 240),
    _StopSpec("S06-WINDOW-MID", "Demo customer 06 (1h36m away)", 96, 13, _W_0800_1900, 240),
    _StopSpec("S07-OPEN-1000", "Demo customer 07 (opens 10:00)", 72, -11, _W_1000_2000, 240),
    _StopSpec("S08-UNKNOWN-HOURS", "Demo customer 08 (hours unknown)", 40, 8, _UNKNOWN, 240),
    _StopSpec("S09-ALWAYS-OPEN", "Demo customer 09 (always accessible)", 35, -7, _ALWAYS, 240),
    _StopSpec(
        "S10-DISABLED",
        "Demo customer 10 (disabled)",
        29,
        5,
        _W_0800_1900,
        240,
        enabled=False,
    ),
    _StopSpec("S11-OPEN-0900", "Demo customer 11 (opens 09:00)", 20, -3, _W_0900_2000, 240, 1),
    _StopSpec("S12-OPEN-0900B", "Demo customer 12 (opens 09:00)", 37, 6, _W_0900_2000, 300),
    _StopSpec("S13-OPEN-0830", "Demo customer 13 (opens 08:30)", 50, -11, _W_0830_2000, 240),
    _StopSpec("S14-PRIORITY-2", "Demo customer 14", 62, 13, _W_0800_2000, 300, 2),
    _StopSpec("S15-115", "Demo customer 15", 74, 0, _W_0800_1900, 240),
    _StopSpec("S16-130", "Demo customer 16", 83, -13, _W_0800_1900, 240),
    _StopSpec("S17-OPEN-0900", "Demo customer 17 (opens 09:00)", 90, 5, _W_0900_2000, 300),
    # No service_duration: the plan default applies and the timeline is flagged accordingly.
    _StopSpec("S18-NO-DURATION", "Demo customer 18 (duration unknown)", 97, -5, _W_0800_1900, None),
    _StopSpec("S19-ALWAYS-OPEN2", "Demo customer 19 (always accessible)", 43, 11, _ALWAYS, 240),
    _StopSpec("S20-192", "Demo customer 20", 109, -12, _W_0800_2000, 240),
    _StopSpec("S21-PRIORITY-1", "Demo customer 21 (opens 08:30)", 114, 4, _W_0830_2000, 300, 1),
    _StopSpec("S22-206", "Demo customer 22", 118, -10, _W_0800_2000, 240),
    _StopSpec("S23-UNKNOWN-HOURS2", "Demo customer 23 (hours unknown)", 37, -14, _UNKNOWN, 240),
    _StopSpec("S24-OPEN-0900", "Demo customer 24 (opens 09:00)", 121, -4, _W_0900_2000, 240),
    # Named "ON-OPENING" for the first-stop intent it demonstrates: it is the candidate that drives
    # the pre-opening gap productively and is the only one to reach its own first customer exactly at
    # the 08:00 opening (recorded id from Stage 1; the U5 review noted the name reads oddly now that
    # the route arrives at 06:08 and waits, and renaming it is not part of this unit).
    _StopSpec("S25-ON-OPENING", "Demo customer 25 (2h08m away, opens 08:00)", 128, 12,
              _W_0800_2000, 240),
    _StopSpec("S26-224", "Demo customer 26 (2h05m away)", 125, -8, _W_0800_2000, 240),
    _StopSpec("S27-OPEN-1000B", "Demo customer 27 (opens 10:00)", 107, -20, _W_1000_2000, 240),
    _StopSpec("S28-PRIORITY-3", "Demo customer 28", 122, 16, _W_0800_2000, 240, 3),
    _StopSpec("S29-196", "Demo customer 29", 112, 5, _W_0800_2000, 300),
    _StopSpec("S30-OPEN-0900B", "Demo customer 30 (opens 09:00)", 102, -17, _W_0900_2000, 240),
    _StopSpec("S31-175", "Demo customer 31", 100, 18, _W_0800_1900, 240),
    # The feasibility bottleneck: 1h36m away and closing at 10:00, so only a complete route that
    # reaches it in its first ~2 hours can serve it (module docstring; v2 section 14).
    _StopSpec("S32-EARLY-CLOSE", "Demo customer 32 (closes 10:00)", 96, 13, _W_0800_1000, 240),
)


def intended_travel_minutes(north_minutes: int, east_minutes: int) -> int:
    """The synthetic first-leg travel the demo author intends for a pair of offsets."""
    return max(abs(north_minutes), abs(east_minutes))


def work_list_order() -> tuple[_StopSpec, ...]:
    """The specs in the order the driver supplied them: the plan's ``input_position`` order.

    The stops are *authored* above in id order, which is what makes the file readable. The plan
    itself holds them in a plausible **nearest-first work list** (v2 section 30: ``input_position``
    is immutable input-order provenance), so the USER baseline - the product's BEFORE route - is a
    route a driver could really have entered rather than a jumble that cannot be served at all.
    Ties are broken by stop id, so the order is deterministic.
    """
    return tuple(sorted(_SPECS, key=lambda spec: (spec.travel_minutes, spec.stop_id)))


def _point_for_offset(north_minutes: float, east_minutes: float) -> GeoPoint:
    """Synthetic coordinates at a chosen synthetic travel time from the warehouse."""
    return GeoPoint(
        WAREHOUSE_POINT.latitude + north_minutes / 60.0,
        WAREHOUSE_POINT.longitude + east_minutes / 60.0,
    )


def _build_stop(spec: _StopSpec, input_position: int) -> RouteStop:
    point = _point_for_offset(spec.north_minutes, spec.east_minutes)
    return RouteStop(
        id=spec.stop_id,
        raw_address=f"{spec.label}, demo district {spec.travel_minutes // 10}",
        normalized_address=f"{spec.label}, demo region, synthetic coordinates",
        latitude=point.latitude,
        longitude=point.longitude,
        geocode_status=GeocodeStatus.RESOLVED,
        service_window=spec.window,
        service_duration=spec.service_duration,
        priority=spec.priority,
        enabled=spec.enabled,
        # Input order as authored: this is the user-facing BEFORE baseline (v2 section 30) and is
        # never rewritten by optimization.
        input_position=input_position,
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
        stops=tuple(
            _build_stop(spec, index) for index, spec in enumerate(work_list_order())
        ),
        cost_policy=cost_policy if cost_policy is not None else smart_route_elapsed_policy(),
        window_end_policy=window_end_policy,
        default_service_duration=DEMO_DEFAULT_SERVICE_DURATION,
        first_service_stop=FirstStopIntent.recommend(),
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
