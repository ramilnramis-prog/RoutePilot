"""Route plan aggregate (decisions D10, D11, D21, D22; spec sections 2, 5, 17).

The plan is the root of the domain graph. Three structural choices carry most of the weight:

* START and FINISH are :class:`~core.model.value_objects.PlaceRef`, a *different type* from a
  service stop, so a departure location or a finish location cannot be served, ordered or
  optimized (invariants I1/I2) - this is enforced by the type system, not by a runtime check.
* ``first_service_stop`` holds the driver's persisted **decision** (intent) only. What the engine
  recommends is derived, cached and recomputable, and never implies a selection (D4/D11/D32).
* ``order_overrides`` is a generic container so future drag/reorder does not require
  redesigning the plan (D21). Only ``first_stop`` constraints are implemented today.
* ``stops`` are held in **input order** (their immutable ``input_position``), which is also the
  order of the user-facing BEFORE baseline (v2 section 30). Route order is a separate concept and
  lives in the solution, never in the stop.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from core.model.cost_policy import RouteCostPolicy, empty_cost_policy
from core.model.first_stop import FirstStopIntent, FirstStopState
from core.model.ids import PlanId, StopId
from core.model.order_override import OrderOverrides
from core.model.route_mode import DEFAULT_ROUTE_MODE, RouteMode
from core.model.route_stop import RouteStop
from core.model.service_window import DEFAULT_WINDOW_END_POLICY, WindowEndPolicy
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
    #: Default interpretation of a fixed window's end; a stop may override it (D29).
    window_end_policy: WindowEndPolicy = DEFAULT_WINDOW_END_POLICY
    #: The driver's decision. Defaults to "RECOMMEND mode, nothing chosen yet": the optimizer
    #: proposes, the driver decides (D4/D32).
    first_service_stop: FirstStopIntent = field(default_factory=FirstStopIntent.recommend)
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
        seen: set[str] = set()
        positions: dict[int, str] = {}
        for stop in stops:
            if not isinstance(stop, RouteStop):
                raise InvalidRoutePlanError(
                    f"stops must contain RouteStop instances, got {type(stop).__name__}"
                )
            if stop.id in seen:
                raise InvalidRoutePlanError(f"duplicate stop id {stop.id!r} in plan {self.id!r}")
            seen.add(stop.id)

            previous = positions.get(stop.input_position)
            if previous is not None:
                raise InvalidRoutePlanError(
                    f"duplicate input_position {stop.input_position} in plan {self.id!r}: "
                    f"stops {previous!r} and {stop.id!r}. input_position is immutable input-order "
                    "provenance and must be unique within a plan (v2 section 30); gaps are "
                    "allowed, renumbering is not."
                )
            positions[stop.input_position] = stop.id

        # Keep the stops in input order, so nothing downstream depends on the order the caller
        # happened to pass them in. Nothing is renumbered: the positions are exactly as supplied.
        stops = tuple(sorted(stops, key=lambda stop: stop.input_position))
        object.__setattr__(self, "stops", stops)

        if not isinstance(self.route_mode, RouteMode):
            object.__setattr__(self, "route_mode", RouteMode(self.route_mode))
        if not isinstance(self.window_end_policy, WindowEndPolicy):
            object.__setattr__(
                self, "window_end_policy", WindowEndPolicy(self.window_end_policy)
            )

        if not isinstance(self.first_service_stop, FirstStopIntent):
            raise InvalidRoutePlanError("first_service_stop must be a FirstStopIntent")
        if not isinstance(self.order_overrides, OrderOverrides):
            raise InvalidRoutePlanError("order_overrides must be an OrderOverrides")

        # Declared-but-unimplemented constraint kinds are rejected, never ignored (D16/D21).
        self.order_overrides.validate_supported()

        # One source of truth for the chosen first stop: the driver's selection is authoritative.
        # When a generic first_stop order override is present it must mirror that selection, so the
        # two representations cannot silently diverge (D21). An absent override is not a conflict.
        constrained_first = self.order_overrides.first_stop_id()
        selected_first = self.first_service_stop.selected_stop_id
        if constrained_first is not None and constrained_first != selected_first:
            raise InvalidRoutePlanError(
                f"order_overrides says first stop {constrained_first!r} while "
                f"first_service_stop.selected_stop_id is {selected_first!r}; the plan would have "
                "two conflicting sources of truth (D21)"
            )

        # A selected first stop must be a real, enabled stop: disabling the chosen stop afterwards
        # would otherwise leave the plan claiming a first stop it can no longer serve.
        if selected_first is not None:
            by_id = {stop.id: stop for stop in stops}
            chosen = by_id.get(selected_first)
            if chosen is None:
                raise InvalidRoutePlanError(
                    f"first_service_stop.selected_stop_id {selected_first!r} is not a stop of "
                    f"plan {self.id!r}"
                )
            if not chosen.enabled:
                raise InvalidRoutePlanError(
                    f"first_service_stop.selected_stop_id {selected_first!r} is disabled; a "
                    "disabled stop cannot be the first service stop"
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
        """Enabled stops, in **input order** (their ``input_position``)."""
        return tuple(stop for stop in self.stops if stop.enabled)

    def disabled_stops(self) -> tuple[RouteStop, ...]:
        """Disabled stops, in input order. They are excluded from routing but keep their position."""
        return tuple(stop for stop in self.stops if not stop.enabled)

    def user_baseline_order(self) -> tuple[StopId, ...]:
        """The user-facing BEFORE route order (v2 section 30).

        ``START -> enabled stops sorted by input_position -> FINISH``: disabled stops are omitted,
        but their existence never renumbers the remaining stops.
        """
        return tuple(stop.id for stop in self.active_stops())

    def next_input_position(self) -> int:
        """The position to give a stop appended later (v2 section 30, requirement 8).

        Appending must never renumber historical stops, so a new stop takes the next free position
        after the highest one in use.
        """
        return max((stop.input_position for stop in self.stops), default=-1) + 1

    @property
    def has_active_stops(self) -> bool:
        return any(stop.enabled for stop in self.stops)

    @property
    def first_stop_state(self) -> FirstStopState:
        """Derived state of the first service stop (D9/D32); never stored.

        In RECOMMEND mode ``awaiting_first_stop_choice`` is the normal starting state: the engine
        has (or will compute) a recommendation, but no working route is committed until the driver
        chooses (I4).
        """
        if not self.stops:
            return FirstStopState.EMPTY_PLAN
        if not self.has_active_stops:
            return FirstStopState.NO_ACTIVE_STOPS
        if self.first_service_stop.selected_stop_id is None:
            return FirstStopState.AWAITING_FIRST_STOP_CHOICE
        return FirstStopState.FIRST_STOP_SELECTED

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
            # Interpreting a window end differently changes feasibility, so it must be able to
            # invalidate a cached recommendation (D4/D29).
            "window_end_policy": self.window_end_policy.value,
            # Deliberately NOT part of this payload: the driver's first-stop decision
            # (mode / selected_stop_id / selection_source / pinned) and the matching order
            # override. The recommendation depends on routing inputs, not on which candidate the
            # driver already accepted - otherwise choosing a stop would immediately make the
            # recommendation look stale, which is exactly the "recommendation has changed" bug
            # D4 warns about. Stage 2 will add a separate route fingerprint for the committed
            # route, which does depend on the selection.
            #
            # Also excluded: input_position. It is input-order provenance that the user-facing
            # BEFORE baseline uses (v2 section 30); the recommendation does not depend on the
            # order the stops arrived in, so reordering positions must not report the
            # recommendation as stale. The stop list is therefore serialised in a stable order
            # (by id) rather than in input order.
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
                    "window_end_policy": (
                        stop.service_window.window_end_policy.value
                        if stop.service_window.window_end_policy is not None
                        else None
                    ),
                    "service_duration": stop.service_duration,
                    "priority": stop.priority,
                    "enabled": stop.enabled,
                }
                for stop in sorted(self.stops, key=lambda some_stop: some_stop.id)
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso_or_none(value: time | None) -> str | None:
    return value.isoformat() if value is not None else None
