"""Shared deterministic fixtures.

The travel provider is synthetic and declares itself as ``DEMO_SYNTHETIC`` (spec section 24):
tests must never look like real road routing.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

from core.engine.providers import ProviderCapabilities
from core.model.ids import PlanId, StopId
from core.model.route_plan import RoutePlan
from core.model.route_stop import GeocodeStatus, RouteStop, ServiceStatus
from core.model.service_window import ServiceWindow
from core.model.value_objects import DataProvenance, GeoPoint, PlaceRef

__all__ = [
    "BERLIN",
    "FixedTravelMatrix",
    "MOSCOW",
    "SECONDS_PER_DEGREE",
    "UTC",
    "build_plan",
    "degrees_for",
    "place",
    "point",
    "stop",
    "utc",
]

UTC = timezone.utc
MOSCOW = "Europe/Moscow"
BERLIN = "Europe/Berlin"

#: Synthetic scale: one degree of coordinate delta equals one hour of driving.
SECONDS_PER_DEGREE = 3600

#: A fixed reference point; realistic-looking but synthetic.
WAREHOUSE = (55.75, 37.62)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A UTC instant. 04:00 Moscow on 2026-09-11 is ``utc(2026, 9, 11, 1, 0)``."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def degrees_for(seconds: int) -> float:
    """Coordinate delta that yields exactly ``seconds`` of synthetic travel."""
    return seconds / SECONDS_PER_DEGREE


def point(latitude: float, longitude: float) -> GeoPoint:
    return GeoPoint(latitude, longitude)


def place(label: str, latitude: float, longitude: float) -> PlaceRef:
    return PlaceRef(label=label, point=point(latitude, longitude))


class FixedTravelMatrix:
    """Deterministic synthetic travel matrix (chebyshev metric, 1 degree == 1 hour).

    ``provenance`` is ``DEMO_SYNTHETIC`` so no caller can mistake it for real routing.
    """

    provenance = DataProvenance.DEMO_SYNTHETIC
    capabilities = ProviderCapabilities()

    def __init__(
        self,
        overrides: dict[tuple[float, float, float, float], int] | None = None,
        *,
        seconds_per_degree: int = SECONDS_PER_DEGREE,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._seconds_per_degree = seconds_per_degree

    @staticmethod
    def _key(origin: GeoPoint, destination: GeoPoint) -> tuple[float, float, float, float]:
        return (
            round(origin.latitude, 6),
            round(origin.longitude, 6),
            round(destination.latitude, 6),
            round(destination.longitude, 6),
        )

    def travel_time_seconds(self, origin: GeoPoint, destination: GeoPoint) -> int:
        key = self._key(origin, destination)
        if key in self._overrides:
            return int(self._overrides[key])
        delta = max(
            abs(destination.latitude - origin.latitude),
            abs(destination.longitude - origin.longitude),
        )
        return int(round(delta * self._seconds_per_degree))

    def distance_meters(self, origin: GeoPoint, destination: GeoPoint) -> float:
        # Synthetic marker distance; tests never present it as real road distance.
        return float(self.travel_time_seconds(origin, destination)) * 12.0


def stop(
    stop_id: str,
    latitude: float | None = None,
    longitude: float | None = None,
    *,
    window: ServiceWindow | None = None,
    service_duration: int | None = 600,
    priority: int | None = None,
    enabled: bool = True,
    geocode_status: GeocodeStatus | None = None,
    raw_address: str | None = None,
    service_status: ServiceStatus = ServiceStatus.PENDING,
) -> RouteStop:
    """Build a stop with sensible defaults; coordinates imply a resolved address."""
    if geocode_status is None:
        geocode_status = (
            GeocodeStatus.RESOLVED if latitude is not None else GeocodeStatus.PENDING
        )
    return RouteStop(
        id=StopId(stop_id),
        raw_address=raw_address or f"{stop_id} street 1",
        normalized_address=f"{stop_id} street 1, Moscow" if latitude is not None else None,
        latitude=latitude,
        longitude=longitude,
        geocode_status=geocode_status,
        service_window=window if window is not None else ServiceWindow.unrestricted(),
        service_duration=service_duration,
        priority=priority,
        service_status=service_status,
        enabled=enabled,
    )


def build_plan(
    *stops: RouteStop,
    plan_id: str = "plan-1",
    timezone_name: str = MOSCOW,
    departure_time: datetime | None = None,
    departure: PlaceRef | None = None,
    finish: PlaceRef | None = None,
    default_service_duration: int | None = None,
    first_service_stop=None,
    order_overrides=None,
    cost_policy=None,
    window_end_policy=None,
) -> RoutePlan:
    """Build a plan whose START is a warehouse at 04:00 Moscow by default."""
    from core.model.first_stop import FirstStopIntent  # local import keeps the fixture light
    from core.model.service_window import DEFAULT_WINDOW_END_POLICY

    kwargs = {}
    if first_service_stop is not None:
        kwargs["first_service_stop"] = first_service_stop
    if order_overrides is not None:
        kwargs["order_overrides"] = order_overrides
    if cost_policy is not None:
        kwargs["cost_policy"] = cost_policy
    return RoutePlan(
        id=PlanId(plan_id),
        timezone=timezone_name,
        departure=departure or place("Warehouse", *WAREHOUSE),
        departure_time=departure_time or utc(2026, 9, 11, 1, 0),  # 04:00 Moscow
        finish=finish or place("Depot", 55.70, 37.55),
        stops=tuple(stops),
        default_service_duration=default_service_duration,
        window_end_policy=(
            window_end_policy if window_end_policy is not None else DEFAULT_WINDOW_END_POLICY
        ),
        **kwargs,
    )
