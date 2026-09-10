"""Small shared value objects: time, place, provenance.

Time model (D2, spec section 20): every absolute timestamp inside RoutePilot is a
timezone-aware :class:`datetime` in **UTC**. Local wall-clock values exist only at the
boundary (service window definitions and presentation) and are resolved through the plan's
explicit IANA time zone by :mod:`core.time.tz`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from core.validation.errors import InvalidRouteStopError, ValidationError

__all__ = [
    "DataProvenance",
    "DurationSec",
    "GeoPoint",
    "Instant",
    "PlaceRef",
    "ensure_utc",
    "local_wall_clock",
]

#: A timezone-aware ``datetime``. Always stored in UTC (D2).
Instant = datetime

#: A duration in whole seconds.
DurationSec = int


def ensure_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Return ``value`` converted to UTC, rejecting naive datetimes.

    Naive datetimes are the classic source of silent timezone bugs, so they are an error
    rather than an assumption about the local zone.
    """
    if not isinstance(value, datetime):
        raise ValidationError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationError(
            f"{field_name} must be timezone-aware (UTC in RoutePilot storage); "
            f"got a naive datetime {value!r}"
        )
    return value.astimezone(timezone.utc)


def local_wall_clock(value: datetime) -> datetime:
    """Return a naive copy representing local wall-clock time (for display only)."""
    return value.replace(tzinfo=None)


class DataProvenance(str, Enum):
    """Where travel time / distance data came from (spec section 24, D15).

    ``DEMO_SYNTHETIC`` must never be presented as real road routing.
    """

    DEMO_SYNTHETIC = "DEMO_SYNTHETIC"
    REAL_ROUTING = "REAL_ROUTING"


@dataclass(frozen=True)
class GeoPoint:
    """A coordinate pair - coordinates only, never routing metadata (spec section 17)."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= float(self.latitude) <= 90.0:
            raise ValidationError(f"latitude {self.latitude!r} is outside [-90, 90]")
        if not -180.0 <= float(self.longitude) <= 180.0:
            raise ValidationError(f"longitude {self.longitude!r} is outside [-180, 180]")


@dataclass(frozen=True)
class PlaceRef:
    """A labelled location that is **not** a service stop: START or FINISH.

    START and FINISH are modelled as ``PlaceRef`` precisely so that they cannot be served
    (invariants I1/I2): they are a different type from :class:`~core.model.route_stop.RouteStop`
    and therefore cannot appear in an optimized order.
    """

    label: str
    point: GeoPoint

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise InvalidRouteStopError("a place reference needs a non-empty label")
        if not isinstance(self.point, GeoPoint):
            raise InvalidRouteStopError(
                f"a place reference needs a GeoPoint, got {type(self.point).__name__}"
            )
