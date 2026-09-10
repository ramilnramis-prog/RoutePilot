"""Route plan aggregate (decisions D10, D11, D21, D22; spec sections 2, 5, 17).

The plan is the root of the domain graph. Three structural choices carry most of the weight:

* START and FINISH are :class:`~core.model.value_objects.PlaceRef`, a *different type* from a
  service stop, so a departure location or a finish location cannot be served, ordered or
  optimized (invariants I1/I2) - this is enforced by the type system, not by a runtime check.
* ``first_service_stop`` holds the persisted **intent** only. The derived resolution (which
  stop was chosen, why, whether it is locked) belongs to a solution and is recomputed, because
  AUTO is dynamic (D4/D11).
* ``order_overrides`` is a generic container so future drag/reorder does not require
  redesigning the plan (D21). Only ``first_stop`` constraints are implemented today.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from core.model.cost_policy import RouteCostPolicy, empty_cost_policy
from core.model.first_stop import FirstStopIntent
from core.model.ids import PlanId, StopId
from core.model.order_override import OrderOverrides
from core.model.route_mode import DEFAULT_ROUTE_MODE, RouteMode
from core.model.route_stop import RouteStop
from core.model.value_objects import DurationSec, GeoPoint, Instant, PlaceRef, ensure_utc
from core.time import tzdata
from core.validation.errors import InvalidOrderError, InvalidRoutePlanError

__all__ = ["RoutePlan"]


@dataclass(frozen=True)
class RoutePlan:
    """A route to be planned: where the driver starts, when, what has to be visited, where it ends."""

    id: PlanId
    timezone: str
    departure: PlaceRef
    departure_time: Instant
    finish: PlaceRef
    stops: tuple[RouteStop, ...] = field(default_factory=tuple)
    cost_policy: RouteCostPolicy = field(default_factory=empty_cost_policy)
    route_mode: RouteMode = DEFAULT_ROUTE_MODE
    first_service_stop: FirstStopIntent = field(default_factory=FirstStopIntent.auto)
    order_overrides: OrderOverrides = field(default_factory=OrderOverrides.empty)
    default_service_duration: DurationSec | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise InvalidRoutePlanError("a route plan needs a non-empty id")

        # IANA time zone (D2). The name is always checked syntactically; existence is checked
        # only when a time zone database is actually available, so a pure domain test can build
        # a plan on a machine without tzdata (full resolution then fails loudly instead).
        tzdata.validate_timezone_name(self.timezone)

        for field_name in ("departure", "finish"):
            place = getattr(self, field_name)
            if isinstance(place, RouteStop):
                raise InvalidRoutePlanError(
                    f"{field_name} must be a PlaceRef, not a RouteStop: START and FINISH are "
                    "never service stops (I1/I2)"
                )
            if not isinstance(place, PlaceRef):
                raise InvalidRoutePlanError(
                    f"{field_name} must be a PlaceRef, got {type(place).__name__}"
                )

        object.__setattr__(
            self, "departure_time", ensure_utc(self.departure_time, field_name="departure_time")
        )

        stops = tuple(self.stops)
        object.__setattr__(self, "stops", stops)
        seen: set[str] = set()
        for stop in stops:
            if not isinstance(stop, RouteStop):
                raise InvalidRoutePlanError(
                    f"stops must contain RouteStop instances, got {type(stop).__name__}"
                )
            if stop.id in seen:
                raise InvalidRoutePlanError(f"duplicate stop id {stop.id!r} in plan {self.id!r}")
            seen.add(stop.id)

        if not isinstance(self.route_mode, RouteMode):
            object.__setattr__(self, "route_mode", RouteMode(self.route_mode))

        if not isinstance(self.first_service_stop, FirstStopIntent):
            raise InvalidRoutePlanError("first_service_stop must be a FirstStopIntent")
        if not isinstance(self.order_overrides, OrderOverrides):
            raise InvalidRoutePlanError("order_overrides must be an OrderOverrides")

        # Declared-but-unimplemented constraint kinds are rejected, never ignored (D16/D21).
        self.order_overrides.validate_supported()

        # One source of truth for the pinned first stop: if a first_stop constraint exists it
        # must be exactly the pinned intent, otherwise the two would silently diverge.
        constrained_first = self.order_overrides.first_stop_id()
        intent_first = self.first_service_stop.pinned_stop_id
        if constrained_first != intent_first:
            raise InvalidRoutePlanError(
                f"order_overrides says first stop {constrained_first!r} while "
                f"first_service_stop.pinned_stop_id is {intent_first!r}; the plan would have "
                "two conflicting sources of truth (D21)"
            )

        if self.default_service_duration is not None:
            if (
                not isinstance(self.default_service_duration, int)
                or isinstance(self.default_service_duration, bool)
                or self.default_service_duration <= 0
            ):
                raise InvalidRoutePlanError(
                    "default_service_duration must be a positive number of seconds, got "
                    f"{self.default_service_duration!r}"
                )

    # ------------------------------------------------------------------ #
    # locations
    # ------------------------------------------------------------------ #
    @property
    def departure_point(self) -> GeoPoint:
        """Where driving begins - never a service stop."""
        return self.departure.point

    @property
    def finish_point(self) -> GeoPoint:
        """Where the route ends - fixed, never reordered as a normal stop."""
        return self.finish.point

    # ------------------------------------------------------------------ #
    # stops
    # ------------------------------------------------------------------ #
    def active_stops(self) -> tuple[RouteStop, ...]:
        """Enabled stops, in the order the user supplied them."""
        return tuple(stop for stop in self.stops if stop.enabled)

    def disabled_stops(self) -> tuple[RouteStop, ...]:
        return tuple(stop for stop in self.stops if not stop.enabled)

    @property
    def has_active_stops(self) -> bool:
        return any(stop.enabled for stop in self.stops)

    def stop_by_id(self, stop_id: StopId) -> RouteStop:
        for stop in self.stops:
            if stop.id == stop_id:
                return stop
        raise InvalidRoutePlanError(f"unknown stop id {stop_id!r} in plan {self.id!r}")

    def load_timezone(self) -> ZoneInfo:
        """The plan's IANA time zone (raises if no time zone database is available)."""
        return tzdata.load_timezone(self.timezone)

    # ------------------------------------------------------------------ #
    # order validation (spec section 27.3/27.4/27.17)
    # ------------------------------------------------------------------ #
    def validate_order(self, order: Sequence[StopId]) -> None:
        """Check that ``order`` is a permutation of exactly the enabled stops.

        Pure domain validation, not a solver: it guarantees that route construction cannot
        duplicate a stop, lose a stop, route a disabled stop, or sneak START/FINISH into the
        service sequence.
        """
        order = list(order)
        seen: set[str] = set()
        duplicates: list[str] = []
        for stop_id in order:
            if stop_id in seen and stop_id not in duplicates:
                duplicates.append(stop_id)
            seen.add(stop_id)
        if duplicates:
            raise InvalidOrderError(
                f"order contains duplicate stop ids {duplicates}; every enabled stop must be "
                "visited exactly once (spec section 27.17)"
            )

        enabled_ids = {stop.id for stop in self.active_stops()}
        disabled_ids = {stop.id for stop in self.disabled_stops()}
        unknown = [stop_id for stop_id in order if stop_id not in enabled_ids | disabled_ids]
        if unknown:
            raise InvalidOrderError(
                f"order references ids that are not stops of plan {self.id!r}: {unknown}"
            )

        disabled_in_order = [stop_id for stop_id in order if stop_id in disabled_ids]
        if disabled_in_order:
            raise InvalidOrderError(
                f"order contains disabled stops {disabled_in_order}; disabled stops are "
                "excluded from optimization (spec section 27.4)"
            )

        missing = [stop.id for stop in self.active_stops() if stop.id not in seen]
        if missing:
            raise InvalidOrderError(
                f"order omits enabled stops {missing}; every enabled service stop must be "
                "visited exactly once (spec section 27.3)"
            )

    # ------------------------------------------------------------------ #
    # AUTO recomputation trigger (D4/D11, spec section 5)
    # ------------------------------------------------------------------ #
    def inputs_fingerprint(self, *, matrix_fingerprint: str | None = None) -> str:
        """Deterministic fingerprint of the inputs that make AUTO re-evaluate.

        Two runs with the same fingerprint may reuse a cached recommendation; a different
        fingerprint means AUTO must reconsider the first stop and the whole route.

        Deliberately excluded: ``notes`` (no routing effect) and ``service_status`` (execution
        state - a served stop is excluded by ``enabled``/reoptimization, not by this hash).
        """
        payload = {
            "timezone": self.timezone,
            # The IANA data version participates: rules can change between releases (D2).
            "tzdata_version": tzdata.tzdata_version(),
            "matrix_fingerprint": matrix_fingerprint,
            "departure": {
                "label": self.departure.label,
                "latitude": self.departure.point.latitude,
                "longitude": self.departure.point.longitude,
            },
            "departure_time": self.departure_time.isoformat(),
            "finish": {
                "label": self.finish.label,
                "latitude": self.finish.point.latitude,
                "longitude": self.finish.point.longitude,
            },
            "route_mode": self.route_mode.value,
            "first_service_stop": {
                "mode": self.first_service_stop.mode.value,
                "pinned": self.first_service_stop.pinned,
                "pinned_stop_id": self.first_service_stop.pinned_stop_id,
            },
            "order_overrides": [
                {"kind": c.kind.value, "stop_id": c.stop_id, "position": c.position}
                for c in self.order_overrides.constraints
            ],
            "default_service_duration": self.default_service_duration,
            "cost_policy": {
                "name": self.cost_policy.name,
                "weights": {
                    component.value: weight
                    for component, weight in sorted(
                        self.cost_policy.weights.items(), key=lambda item: item[0].value
                    )
                },
            },
            "stops": [
                {
                    "id": stop.id,
                    "raw_address": stop.raw_address,
                    "normalized_address": stop.normalized_address,
                    "latitude": stop.latitude,
                    "longitude": stop.longitude,
                    "geocode_status": stop.geocode_status.value,
                    "window_kind": stop.service_window.window_kind.value,
                    "window_start": _iso_or_none(stop.service_window.start_local),
                    "window_end": _iso_or_none(stop.service_window.end_local),
                    "service_duration": stop.service_duration,
                    "priority": stop.priority,
                    "enabled": stop.enabled,
                }
                for stop in self.stops
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso_or_none(value: time | None) -> str | None:
    return value.isoformat() if value is not None else None
