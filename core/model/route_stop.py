"""Route stop model (spec section 17, decisions D20 and D28; v2 sections 25 and 30).

Three state fields are deliberately independent:

* ``geocode_status`` - address-to-coordinate state (``pending | resolved | ambiguous | failed``);
* ``service_status`` - route execution state (``pending | in_progress | served | failed | skipped``);
* ``enabled``        - whether the stop takes part in optimization at all, regardless of the
  other two.

``input_position`` is a fourth, different kind of field: **historical input-order provenance**. It
records where the stop stood in the list the user supplied or imported, it is immutable, and it is
never rewritten by optimization, by route order or by drag/reorder (v2 sections 25, 30). The
user-facing BEFORE baseline is built from it.

Coordinates stay plain coordinates: routing-specific information (side of road, approach,
geometry) belongs to a routing provider, never to a stop (spec section 17, D16).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum

from core.model.ids import StopId
from core.model.service_window import ServiceWindow
from core.model.value_objects import DurationSec, GeoPoint
from core.validation.errors import InvalidRouteStopError

__all__ = ["GeocodeStatus", "RouteStop", "ServiceStatus"]


class GeocodeStatus(str, Enum):
    """Address -> coordinates state (D20)."""

    PENDING = "pending"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class ServiceStatus(str, Enum):
    """Execution state of the stop during the route (D20). Never a geocoding state."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SERVED = "served"
    FAILED = "failed"
    SKIPPED = "skipped"


def _coerce(enum_type: type[Enum], value: object, field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError:
        raise InvalidRouteStopError(
            f"{field_name}={value!r} is invalid; expected one of "
            f"{[member.value for member in enum_type]}"
        ) from None


@dataclass(frozen=True)
class RouteStop:
    """A single service location the driver has to visit."""

    id: StopId
    raw_address: str
    service_window: ServiceWindow
    #: Immutable historical input-order provenance: the position the stop had in the user-supplied
    #: or imported list (v2 sections 25 and 30). It is **not** the current route order, and neither
    #: optimization nor drag/reorder may ever change it. Gaps are allowed.
    input_position: int
    normalized_address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    geocode_status: GeocodeStatus = GeocodeStatus.PENDING
    service_duration: DurationSec | None = None
    priority: int | None = None
    service_status: ServiceStatus = ServiceStatus.PENDING
    enabled: bool = True
    notes: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise InvalidRouteStopError("a route stop needs a non-empty id")
        if not isinstance(self.raw_address, str) or not self.raw_address.strip():
            raise InvalidRouteStopError(f"stop {self.id!r} needs a non-empty raw_address")
        if not isinstance(self.service_window, ServiceWindow):
            raise InvalidRouteStopError(
                f"stop {self.id!r} needs a ServiceWindow, got "
                f"{type(self.service_window).__name__}"
            )

        # input_position is provenance, so it must be a plain non-negative integer. Uniqueness
        # inside a plan is a plan-level invariant, checked by RoutePlan.
        if isinstance(self.input_position, bool) or not isinstance(self.input_position, int):
            raise InvalidRouteStopError(
                f"stop {self.id!r}: input_position must be an integer, got "
                f"{type(self.input_position).__name__}"
            )
        if self.input_position < 0:
            raise InvalidRouteStopError(
                f"stop {self.id!r}: input_position must be >= 0, got {self.input_position}"
            )

        object.__setattr__(
            self, "geocode_status", _coerce(GeocodeStatus, self.geocode_status, "geocode_status")
        )
        object.__setattr__(
            self, "service_status", _coerce(ServiceStatus, self.service_status, "service_status")
        )

        has_latitude = self.latitude is not None
        has_longitude = self.longitude is not None
        if has_latitude != has_longitude:
            raise InvalidRouteStopError(
                f"stop {self.id!r} must have both latitude and longitude or neither"
            )
        if has_latitude:
            assert self.latitude is not None and self.longitude is not None
            GeoPoint(self.latitude, self.longitude)  # range validation, shared with GeoPoint

        # A resolved address must have coordinates; unresolved states must not smuggle
        # half-guessed coordinates through.
        if self.geocode_status is GeocodeStatus.RESOLVED and not has_latitude:
            raise InvalidRouteStopError(
                f"stop {self.id!r} is geocode_status='resolved' but has no coordinates"
            )
        if self.geocode_status in (GeocodeStatus.PENDING, GeocodeStatus.FAILED) and has_latitude:
            raise InvalidRouteStopError(
                f"stop {self.id!r} is geocode_status={self.geocode_status.value!r} "
                "but carries coordinates; geocode_status must reflect reality"
            )

        # A customer with real opening hours must be locatable: a fixed window on an
        # unresolved address cannot be routed and must not be half-modelled.
        if self.service_window.is_fixed and not has_latitude:
            raise InvalidRouteStopError(
                f"stop {self.id!r} has a fixed service window but no coordinates; "
                "resolve the address first"
            )

        if self.service_duration is not None:
            if not isinstance(self.service_duration, int) or isinstance(self.service_duration, bool):
                raise InvalidRouteStopError(
                    f"stop {self.id!r}: service_duration must be whole seconds, "
                    f"got {type(self.service_duration).__name__}"
                )
            if self.service_duration <= 0:
                raise InvalidRouteStopError(
                    f"stop {self.id!r}: service_duration must be > 0, got {self.service_duration}"
                )

        if self.priority is not None and self.priority < 0:
            raise InvalidRouteStopError(
                f"stop {self.id!r}: priority must be >= 0, got {self.priority}"
            )

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    @property
    def location(self) -> GeoPoint | None:
        """Coordinates, or ``None`` when the address is not resolved yet."""
        if self.latitude is None or self.longitude is None:
            return None
        return GeoPoint(self.latitude, self.longitude)

    @property
    def is_active(self) -> bool:
        """A disabled stop is excluded from optimization regardless of other state."""
        return self.enabled

    @property
    def requires_location(self) -> bool:
        return self.enabled

    @property
    def fixed_window(self) -> tuple[time, time] | None:
        """Local ``(open, close)`` for a fixed window, else ``None``."""
        if not self.service_window.is_fixed:
            return None
        assert self.service_window.start_local is not None
        assert self.service_window.end_local is not None
        return (self.service_window.start_local, self.service_window.end_local)

    def describe_window(self) -> str:
        return self.service_window.describe()
