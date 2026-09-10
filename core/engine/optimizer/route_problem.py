"""The frozen route problem and the fast optimizer evaluation path (Stage 2 unit U2).

Two things live here, and they belong together because the second is the *reason* the first is
shaped the way it is.

**`RouteProblem`** is the immutable, prepared view of one optimization run:

* the plan, its travel matrix behind a :class:`~core.engine.optimizer.cache.LegCache`, and the
  driver-selected first service stop that optimization must never reorder (I3, D10);
* the remaining enabled stops in ``input_position`` order (v2 section 30 - the deterministic
  tie-break order), the departure point/time and the finish point;
* resolved coordinates, service durations, per-stop travel from START and to FINISH, all priced
  through the shared cache;
* **a precomputed service-window table**: for every enabled stop and every local service date
  the forward pass can reach, the window is resolved once, through :mod:`core.time.tz` (never
  hand-written DST arithmetic), and is then reused.

**The fast path** is a forward pass that works in *integer seconds relative to
``plan.departure_time``*. It performs no timezone conversion and no per-stop window resolution:
the service date of an arrival is found by bisecting a precomputed tuple of UTC day boundaries,
and the window is a dictionary lookup.

It is not a second model of the timeline. It is the *same* arithmetic, restricted to the
departure-time grid on which every instant is an exact whole number of seconds, so its results
are the authoritative results by construction. That claim is not taken on trust:

* the authoritative evaluation is still
  :func:`core.engine.optimizer.route_evaluation.evaluate_order`, and ``optimize`` runs it on the
  final order;
* ``tests/engine/test_optimizer.py`` compares the fast path against ``evaluate_order`` over
  orders with waiting, with lateness, with unknown/unrestricted windows and under both
  window-end policies (v2 sections 9, 15; D29);
* a window the precomputed table cannot answer - an arrival beyond the prepared dates, or an
  arrival on a day whose local midnight does not exist - falls back to the authoritative
  :func:`core.time.tz.resolve_service_window` instead of guessing, and the fallback resolution is
  memoized so it stays a one-time cost;
* preparing the table never resolves a time the route may not use: a local midnight or a window
  that falls into a DST gap is simply left out of the table (D3/D29), so building a problem can
  never fail for a plan the authoritative engine routes fine - only an arrival that really lands
  in a gap raises, exactly as :func:`core.time.timeline.compute_stop_timeline` does;
* a stop no complete route could serve - no coordinates, or no service duration anywhere - is
  reported with the authoritative error, not silently priced at the departure point or at zero
  seconds. The fast pass calls :meth:`RouteProblem.require_serviceable` for every stop before it
  prices it, so it rejects exactly the orders ``evaluate_order`` rejects.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from core.engine.optimizer.cache import LegCache
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.service_window import WindowEndPolicy
from core.model.solution import StopTimeline, Violation
from core.model.value_objects import DurationSec, GeoPoint, Instant
from core.time import timeline as timeline_engine
from core.time import tz
from core.validation.errors import (
    DSTValidationError,
    InvalidRoutePlanError,
    MissingServiceDurationError,
    StopNotGeocodedError,
)

__all__ = [
    "FastEvaluation",
    "OpenState",
    "ResolvedWindow",
    "RouteProblem",
    "build_problem",
    "fast_evaluate",
    "fast_evaluation_key",
    "fast_route_evaluation",
    "timelines_of",
    "violations_of",
]

#: How many local service dates past the departure date the window table always covers. A route
#: whose arrivals leave that horizon still works: the lookup falls back to the authoritative
#: resolver instead of guessing.
_MIN_PRECOMPUTED_DATES = 8

#: A window close is found within this many days of the earliest possible arrival.
_MAX_DAY_SEARCH = 40


@dataclass(frozen=True)
class ResolvedWindow:
    """A fixed service window resolved once for one stop and one local service date."""

    open_at: Instant
    close_at: Instant


@dataclass(frozen=True)
class OpenState:
    """The forward-pass state at one point of the route, on the departure-time grid."""

    elapsed_sec: DurationSec
    point: GeoPoint


@dataclass(frozen=True)
class FastEvaluation:
    """One complete route measured in whole seconds relative to ``plan.departure_time``.

    ``finish_elapsed_sec`` is the route's objective: elapsed seconds from departure until the
    driver reaches FINISH, the final leg included (v2 section 15). Waiting is part of it, which
    is exactly why a stop that avoids useless waiting can beat a nearer one.

    ``violations`` is a **count**, never a penalty: a hard-window miss is never traded against
    seconds (D13 amendment). The full :class:`~core.model.solution.Violation` records are
    produced by the authoritative evaluation of the same order.
    """

    order: tuple[StopId, ...]
    violations: int
    travel_sec: DurationSec
    waiting_sec: DurationSec
    service_sec: DurationSec
    distance_m: float
    finish_elapsed_sec: DurationSec
    departure_time: Instant = datetime(1970, 1, 1, tzinfo=timezone.utc)
    window_fallbacks: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", tuple(self.order))
        if self.violations < 0:
            raise InvalidRoutePlanError("a violation count cannot be negative")

    @property
    def elapsed_sec(self) -> DurationSec:
        """Alias for the objective: elapsed seconds from departure to FINISH."""
        return self.finish_elapsed_sec

    @property
    def duration_sec(self) -> DurationSec:
        """Driving + waiting + service over the whole route, FINISH leg included."""
        return self.travel_sec + self.waiting_sec + self.service_sec

    @property
    def feasible(self) -> bool:
        return self.violations == 0

    @property
    def finishes_at(self) -> Instant:
        """When the driver reaches FINISH, as an absolute instant."""
        return self.departure_time + timedelta(seconds=self.finish_elapsed_sec)


# --------------------------------------------------------------------------- #
# comparison: feasibility first, then the objective - lexicographic
# --------------------------------------------------------------------------- #
def fast_evaluation_key(evaluation: FastEvaluation) -> tuple[int, int]:
    """The deterministic acceptance key of a route: ``(violations, elapsed seconds)``.

    Fewer hard-window violations is always better; among equally violating routes the one that
    finishes earlier wins. A hard violation is never converted into seconds (D13 amendment).
    """
    return (evaluation.violations, evaluation.finish_elapsed_sec)


def is_improvement(candidate: FastEvaluation, current: FastEvaluation) -> bool:
    """Whether ``candidate`` is strictly better than ``current``, lexicographically."""
    return fast_evaluation_key(candidate) < fast_evaluation_key(current)


def not_worse(candidate: FastEvaluation, current: FastEvaluation) -> bool:
    """Whether ``candidate`` may replace ``current``: never more violations, never later."""
    return fast_evaluation_key(candidate) <= fast_evaluation_key(current)


@dataclass(frozen=True)
class RouteProblem:
    """A frozen, prepared optimization problem (Stage 2 unit U2).

    ``first_stop_id`` is the driver's decision: it must be an enabled stop of the plan, and
    optimization keeps it first (I3, D10). Asking for a committed route without it is an error,
    not a silently different route.

    The travel matrix is wrapped in a :class:`~core.engine.optimizer.cache.LegCache`, and every
    leg this problem prices goes through it, so the optimizer and a later candidate evaluation
    that share the problem also share the cache. ``cache_stats`` reports the reuse instead of
    claiming it (v2 section 20).
    """

    plan: RoutePlan
    legs: LegCache
    first_stop_id: StopId
    tzinfo: Any = None
    stop_ids: tuple[StopId, ...] = ()
    locations: tuple[GeoPoint, ...] = ()
    input_positions: tuple[int, ...] = ()
    duration_by_stop: tuple[DurationSec, ...] = ()
    window_end_policies: tuple[WindowEndPolicy | None, ...] = ()
    travel_from_departure: tuple[DurationSec, ...] = ()
    travel_to_finish: tuple[DurationSec, ...] = ()
    distance_from_departure: tuple[float, ...] = ()
    distance_to_finish: tuple[float, ...] = ()
    utc_cutoffs: tuple[Instant | None, ...] = ()
    cutoff_instants: tuple[Instant, ...] = field(default=(), compare=False, repr=False)
    cutoff_offsets: tuple[int, ...] = field(default=(), compare=False, repr=False)
    window_table: dict[tuple[StopId, int], ResolvedWindow] = field(
        default_factory=dict, compare=False, repr=False
    )
    fallback_windows: dict[tuple[StopId, date], ResolvedWindow] = field(
        default_factory=dict, compare=False, repr=False
    )
    fallbacks_used: int = field(default=0, compare=False)
    service_problems: dict[StopId, str] = field(default_factory=dict, compare=False, repr=False)
    stop_index: dict[StopId, int] = field(default_factory=dict, compare=False, repr=False)

    # ---- construction --------------------------------------------------- #
    def __post_init__(self) -> None:
        plan = self.plan
        if not isinstance(plan, RoutePlan):
            raise InvalidRoutePlanError("a route problem needs a RoutePlan")
        if not isinstance(self.first_stop_id, str) or not self.first_stop_id:
            raise InvalidRoutePlanError(
                "a route problem needs the driver's selected first service stop: nothing selects "
                "a stop automatically (I4/D32)"
            )
        if not isinstance(self.legs, LegCache):
            raise InvalidRoutePlanError(
                "a route problem needs its travel matrix wrapped in a LegCache, so every leg is "
                "priced once and the reuse is measurable (v2 section 20)"
            )

        first_stop = plan.stop_by_id(self.first_stop_id)  # raises for an unknown id
        if not first_stop.enabled:
            raise InvalidRoutePlanError(
                f"the selected first service stop {self.first_stop_id!r} is disabled; a disabled "
                "stop is excluded from optimization (spec section 27.4, D20)"
            )

        tzinfo = plan.load_timezone()
        object.__setattr__(self, "tzinfo", tzinfo)

        stops = plan.active_stops()  # enabled stops, already in input_position order
        stop_ids: list[StopId] = []
        locations: list[GeoPoint] = []
        input_positions: list[int] = []
        durations: list[DurationSec] = []
        policies: list[WindowEndPolicy | None] = []
        problems: dict[StopId, str] = {}

        for stop in stops:
            stop_ids.append(stop.id)
            input_positions.append(stop.input_position)
            try:
                duration, _used_default = timeline_engine.resolve_service_duration(stop, plan)
            except MissingServiceDurationError:
                problems[stop.id] = "no service duration and no plan default"
                duration = 0
            durations.append(duration)
            policies.append(stop.service_window.effective_end_policy(plan.window_end_policy))
            location = stop.location
            if location is None:
                problems[stop.id] = (
                    f"geocode_status={stop.geocode_status.value} and no coordinates"
                )
                location = plan.departure_point
            locations.append(location)

        object.__setattr__(self, "stop_ids", tuple(stop_ids))
        object.__setattr__(self, "locations", tuple(locations))
        object.__setattr__(self, "input_positions", tuple(input_positions))
        object.__setattr__(self, "duration_by_stop", tuple(durations))
        object.__setattr__(self, "window_end_policies", tuple(policies))
        object.__setattr__(self, "service_problems", problems)
        object.__setattr__(
            self, "stop_index", {stop_id: index for index, stop_id in enumerate(stop_ids)}
        )

        departure = plan.departure_point
        finish = plan.finish_point
        object.__setattr__(
            self,
            "travel_from_departure",
            tuple(self.legs.travel_time_seconds(departure, point) for point in locations),
        )
        object.__setattr__(
            self,
            "travel_to_finish",
            tuple(self.legs.travel_time_seconds(point, finish) for point in locations),
        )
        object.__setattr__(
            self,
            "distance_from_departure",
            tuple(self.legs.distance_meters(departure, point) for point in locations),
        )
        object.__setattr__(
            self,
            "distance_to_finish",
            tuple(self.legs.distance_meters(point, finish) for point in locations),
        )

        self._build_window_table(tzinfo)

    def _build_window_table(self, tzinfo: Any) -> None:
        """Resolve every reachable (stop, local service date) window exactly once.

        The day boundaries themselves come from :func:`core.time.tz.resolve_local_datetime`, so
        no DST rule is ever written by hand (D2/D3).

        Preparing the table may **not** fail for a plan the authoritative engine routes fine: a
        local midnight or a fixed window that falls into a DST gap is left out of the table
        instead of being raised here (U2 review). The entry is then answered by the authoritative
        resolver in :meth:`window_for`, which raises for exactly the arrivals that really land on
        such a date - the same arrivals :func:`core.time.timeline.compute_stop_timeline` rejects.
        """
        plan = self.plan
        departure_date = tz.local_date_of(plan.departure_time, tzinfo)
        last_date = self._latest_possible_service_date(departure_date, tzinfo)
        days = last_date.toordinal() - departure_date.toordinal() + 1

        cutoffs: list[Instant | None] = []
        instants: list[Instant] = []
        offsets: list[int] = []
        for step in range(days):
            service_date = date.fromordinal(departure_date.toordinal() + step)
            try:
                midnight = tz.resolve_local_datetime(service_date, time(0, 0), tzinfo)
            except DSTValidationError:
                # Local midnight does not exist (or is ambiguous) on that date: the date has no
                # usable boundary, so it is recorded as unresolved rather than shifted.
                cutoffs.append(None)
                continue
            cutoffs.append(midnight)
            instants.append(midnight)
            offsets.append(step)
        object.__setattr__(self, "utc_cutoffs", tuple(cutoffs))
        object.__setattr__(self, "cutoff_instants", tuple(instants))
        object.__setattr__(self, "cutoff_offsets", tuple(offsets))

        table: dict[tuple[StopId, int], ResolvedWindow] = {}
        for stop_id in self.stop_ids:
            window = plan.stop_by_id(stop_id).service_window
            if not window.is_fixed:
                continue  # nothing to resolve, nothing invented
            for offset in range(days):
                service_date = date.fromordinal(departure_date.toordinal() + offset)
                try:
                    resolved = tz.resolve_service_window(window, service_date, tzinfo)
                except DSTValidationError:
                    # This window does not exist on that service date (D3). It is left out so the
                    # authoritative resolver answers - and raises - for a real arrival there.
                    continue
                if resolved is None:  # pragma: no cover - a fixed window always resolves
                    continue
                table[(stop_id, offset)] = ResolvedWindow(
                    open_at=resolved.open_at, close_at=resolved.close_at
                )
        object.__setattr__(self, "window_table", table)

    def _latest_possible_service_date(self, departure_date: date, tzinfo: Any) -> date:
        """The last local service date the precomputed table must cover.

        Derived from the plan, never from a hand-written timezone rule: for each fixed window the
        close instant is resolved through :mod:`core.time.tz` on the day the earliest possible
        arrival falls on, and the latest close plus its service duration is the horizon. A date
        whose close cannot be resolved contributes nothing to the horizon (D3): it is not a day
        the route could be served on, so it cannot widen the table either.
        """
        horizon = departure_date.toordinal() + _MIN_PRECOMPUTED_DATES
        for index, stop_id in enumerate(self.stop_ids):
            window = self.plan.stop_by_id(stop_id).service_window
            if not window.is_fixed or window.end_local is None:
                continue
            earliest_arrival = self.plan.departure_time + timedelta(
                seconds=self.travel_from_departure[index]
            )
            arrival_date = tz.local_date_of(earliest_arrival, tzinfo)
            for step in range(_MAX_DAY_SEARCH):
                service_date = date.fromordinal(arrival_date.toordinal() + step)
                try:
                    close_at = tz.resolve_local_datetime(service_date, window.end_local, tzinfo)
                except DSTValidationError:
                    continue
                if close_at < earliest_arrival:
                    continue
                horizon = max(
                    horizon,
                    tz.local_date_of(
                        close_at + timedelta(seconds=self.duration_by_stop[index]), tzinfo
                    ).toordinal(),
                )
                break
        return date.fromordinal(horizon)

    # ---- queries --------------------------------------------------------- #
    @property
    def cache(self) -> LegCache:
        """The leg cache the optimizer and later candidate evaluation share."""
        return self.legs

    @property
    def cache_stats(self) -> CacheStats:
        """Hits, misses and entries of the leg cache, for the demo and the benchmark."""
        return self.legs.stats

    @property
    def departure_point(self) -> GeoPoint:
        """START, where driving begins - never a service stop (I1)."""
        return self.plan.departure_point

    @property
    def departure_time(self) -> Instant:
        return self.plan.departure_time

    @property
    def finish_point(self) -> GeoPoint:
        """FINISH: fixed, and never reordered like a service stop (I2)."""
        return self.plan.finish_point

    @property
    def stop_count(self) -> int:
        return len(self.stop_ids)

    @property
    def remaining_stop_ids(self) -> tuple[StopId, ...]:
        """The enabled stops other than the first stop, in ``input_position`` order."""
        return tuple(stop_id for stop_id in self.stop_ids if stop_id != self.first_stop_id)

    @property
    def service_dates_precomputed(self) -> int:
        """How many local service dates the window table covers."""
        return len(self.utc_cutoffs)

    def input_position_of(self, stop_id: StopId) -> int:
        return self.input_positions[self.index_of(stop_id)]

    def index_of(self, stop_id: StopId) -> int:
        index = self.stop_index.get(stop_id)
        if index is None:
            raise InvalidRoutePlanError(
                f"stop {stop_id!r} is not an enabled stop of plan {self.plan.id!r}"
            )
        return index

    def location_of(self, stop_id: StopId) -> GeoPoint:
        return self.locations[self.index_of(stop_id)]

    def service_duration_of(self, stop_id: StopId) -> DurationSec:
        self.require_serviceable(stop_id)
        return self.duration_by_stop[self.index_of(stop_id)]

    def window_end_policy_of(self, stop_id: StopId) -> WindowEndPolicy | None:
        return self.window_end_policies[self.index_of(stop_id)]

    # ---- travel: every leg goes through the shared cache ------------------ #
    def travel_sec(self, origin: GeoPoint, destination: GeoPoint) -> DurationSec:
        return self.legs.travel_time_seconds(origin, destination)

    def distance_m(self, origin: GeoPoint, destination: GeoPoint) -> float:
        return self.legs.distance_meters(origin, destination)

    def travel_from_departure_to(self, index: int) -> DurationSec:
        return self.travel_from_departure[index]

    def travel_to_finish_from(self, index: int) -> DurationSec:
        return self.travel_to_finish[index]

    def distance_from_departure_to(self, index: int) -> float:
        return self.distance_from_departure[index]

    def distance_to_finish_from(self, index: int) -> float:
        return self.distance_to_finish[index]

    def travel_between(self, origin_index: int, destination_index: int) -> DurationSec:
        return self.legs.travel_time_seconds(
            self.locations[origin_index], self.locations[destination_index]
        )

    def distance_between(self, origin_index: int, destination_index: int) -> float:
        return self.legs.distance_meters(
            self.locations[origin_index], self.locations[destination_index]
        )

    # ---- window lookup: precomputed table first, authoritative fallback --- #
    def elapsed_of(self, instant: Instant) -> DurationSec:
        """Whole seconds from ``plan.departure_time`` to ``instant`` (the fast path's clock)."""
        return int((instant - self.plan.departure_time).total_seconds())

    def day_offset_of(self, elapsed_sec: DurationSec) -> int | None:
        """The precomputed-table day offset of an elapsed time, via the UTC day boundaries.

        Returns ``None`` when the table cannot answer for that arrival: it is before the first
        resolvable local midnight, it is on or after the last prepared date, or the local midnight
        of the following day does not exist. In every such case the caller resolves the window
        authoritatively instead of using a neighbouring date's window (U2 review).
        """
        at = self.plan.departure_time + timedelta(seconds=elapsed_sec)
        index = bisect.bisect_right(self.cutoff_instants, at) - 1
        if index < 0:
            return None
        offset = self.cutoff_offsets[index]
        following = offset + 1
        if following >= len(self.utc_cutoffs):
            # The arrival is on (or beyond) the last prepared date: the table has no day after it
            # to bound the arrival's own local date, so it cannot answer - authoritative resolver.
            return None
        if self.utc_cutoffs[following] is None:
            # Local midnight of the next day does not exist, so this arrival could be on either
            # side of that boundary. Never guess which service date applies.
            return None
        return offset

    def window_for(self, stop_id: StopId, elapsed_sec: DurationSec) -> ResolvedWindow | None:
        """The stop's window for the local service date of that arrival.

        Answered from the precomputed table. When the table cannot answer - the arrival leaves
        the prepared dates, or its date has no resolvable local midnight - the window is resolved
        authoritatively by :func:`core.time.tz.resolve_service_window`, the same call the
        authoritative timeline makes, and memoized, never guessed. A window that does not exist on
        that service date (a DST gap) therefore raises here exactly as it does in
        :func:`core.time.timeline.compute_stop_timeline` (D3).
        """
        window = self.plan.stop_by_id(stop_id).service_window
        if not window.is_fixed:
            return None
        offset = self.day_offset_of(elapsed_sec)
        if offset is not None:
            known = self.window_table.get((stop_id, offset))
            if known is not None:
                return known

        arrival = self.plan.departure_time + timedelta(seconds=elapsed_sec)
        service_date = tz.local_date_of(arrival, self.tzinfo)
        cached = self.fallback_windows.get((stop_id, service_date))
        if cached is not None:
            return cached
        resolved = tz.resolve_service_window(window, service_date, self.tzinfo)
        if resolved is None:  # pragma: no cover - a fixed window always resolves
            return None
        entry = ResolvedWindow(open_at=resolved.open_at, close_at=resolved.close_at)
        self.fallback_windows[(stop_id, service_date)] = entry
        object.__setattr__(self, "fallbacks_used", self.fallbacks_used + 1)
        return entry

    def advance(self, state: OpenState, stop_id: StopId) -> OpenState:
        """Serve ``stop_id`` from ``state``, in integer seconds, using the precomputed table."""
        # A stop no complete route could serve is refused with the authoritative error, before any
        # arithmetic: the fast path never stands in for a route the engine would reject.
        self.require_serviceable(stop_id)
        index = self.index_of(stop_id)
        travel = self.legs.travel_time_seconds(state.point, self.locations[index])
        arrival = state.elapsed_sec + travel
        window = self.window_for(stop_id, arrival)
        duration = self.duration_by_stop[index]

        if window is None:
            service_start = arrival
        else:
            waiting = max(0, self.elapsed_of(window.open_at) - arrival)
            service_start = arrival + waiting
        return OpenState(elapsed_sec=service_start + duration, point=self.locations[index])

    def require_serviceable(self, stop_id: StopId) -> None:
        """Raise the authoritative error for a stop no complete route could serve."""
        problem = self.service_problems.get(stop_id)
        if problem is None:
            return
        stop = self.plan.stop_by_id(stop_id)
        if stop.location is None:
            raise StopNotGeocodedError(stop.id, stop.geocode_status.value)
        raise MissingServiceDurationError(stop.id)

    # ---- the same prepared problem for another driver-selected first stop - #
    def with_first_stop(self, first_stop_id: StopId) -> "RouteProblem":
        """The same plan and leg cache for a different driver-selected first stop.

        Sharing the cache is what makes evaluating several candidate first stops reuse every leg
        they have in common (v2 section 20).
        """
        if first_stop_id == self.first_stop_id:
            return self
        return RouteProblem(plan=self.plan, legs=self.legs, first_stop_id=first_stop_id)

    def without_precomputed_windows(self) -> "RouteProblem":
        """A copy of this problem whose window lookups must all use the authoritative resolver.

        This is the safety valve of the fast path made explicit: it exists so the fallback can be
        exercised deliberately (rather than only on an arrival that leaves the prepared horizon)
        and compared against the precomputed path, in tests and in a review.
        """
        copy = object.__new__(type(self))
        for name in type(self).__dataclass_fields__:
            object.__setattr__(copy, name, getattr(self, name))
        object.__setattr__(copy, "window_table", {})
        object.__setattr__(copy, "fallback_windows", {})
        return copy


# --------------------------------------------------------------------------- #
# the fast complete-route pass
# --------------------------------------------------------------------------- #
def fast_evaluate(problem: RouteProblem, order: Sequence[StopId]) -> FastEvaluation:
    """Evaluate a complete order in integer seconds, without a per-stop timezone conversion.

    The FINISH leg is included (v2 section 15). The result agrees with
    :func:`core.engine.optimizer.route_evaluation.evaluate_order` for the applied
    ``window_end_policy``; the module docstring says how that is proven.

    A stop that no complete route could serve is refused with the authoritative error *before* it
    is priced, exactly as :func:`core.time.timeline.compute_stop_timeline` does - a missing
    location is never substituted by the departure point and a missing duration is never zero. The
    check runs over the route first, in route order, so the reported error matches
    ``evaluate_order`` for the same order; it then covers every enabled stop, so a partial pass can
    never stand in for a complete route that the engine would reject.
    """
    for stop_id in order:
        problem.require_serviceable(stop_id)
    for stop_id in problem.stop_ids:
        problem.require_serviceable(stop_id)

    point = problem.departure_point
    elapsed = 0
    travel_sec = 0
    waiting_sec = 0
    service_sec = 0
    distance_m = 0.0
    violations = 0
    fallbacks_before = problem.fallbacks_used

    for stop_id in order:
        index = problem.index_of(stop_id)
        leg_travel = problem.legs.travel_time_seconds(point, problem.locations[index])
        arrival = elapsed + leg_travel
        window = problem.window_for(stop_id, arrival)
        duration = problem.duration_by_stop[index]

        if window is None:
            waiting = 0
            service_start = arrival
        else:
            waiting = max(0, problem.elapsed_of(window.open_at) - arrival)
            service_start = arrival + waiting
            if problem.window_end_policies[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                lateness = max(0, service_start + duration - problem.elapsed_of(window.close_at))
            else:
                lateness = max(0, service_start - problem.elapsed_of(window.close_at))
            if lateness > 0:
                violations += 1

        travel_sec += leg_travel
        waiting_sec += waiting
        service_sec += duration
        distance_m += problem.legs.distance_meters(point, problem.locations[index])
        elapsed = service_start + duration
        point = problem.locations[index]

    finish_travel = problem.legs.travel_time_seconds(point, problem.finish_point)
    finish_distance = problem.legs.distance_meters(point, problem.finish_point)
    return FastEvaluation(
        order=tuple(order),
        violations=violations,
        travel_sec=travel_sec + finish_travel,
        waiting_sec=waiting_sec,
        service_sec=service_sec,
        distance_m=distance_m + finish_distance,
        finish_elapsed_sec=elapsed + finish_travel,
        departure_time=problem.departure_time,
        window_fallbacks=problem.fallbacks_used - fallbacks_before,
    )


def fast_route_evaluation(problem: RouteProblem, evaluation: FastEvaluation):
    """The authoritative :class:`RouteEvaluation` of a fast evaluation's order.

    The optimizer's answer always goes through this: the shipped route is the one
    :func:`core.engine.optimizer.route_evaluation.evaluate_order` produces, and the fast path is
    only the inner loop's view of the same arithmetic.
    """
    from core.engine.optimizer.route_evaluation import evaluate_order

    return evaluate_order(plan=problem.plan, travel_matrix=problem.legs, order=evaluation.order)


def violations_of(problem: RouteProblem, evaluation: FastEvaluation) -> tuple[Violation, ...]:
    """The authoritative violation records of a fast evaluation's order."""
    return fast_route_evaluation(problem, evaluation).violations


def timelines_of(problem: RouteProblem, evaluation: FastEvaluation) -> tuple[StopTimeline, ...]:
    """The authoritative timelines of a fast evaluation's order."""
    return fast_route_evaluation(problem, evaluation).timelines


def build_problem(
    *,
    plan: RoutePlan,
    travel_matrix: TravelMatrix,
    first_stop_id: StopId | None = None,
    cache: LegCache | None = None,
) -> RouteProblem:
    """Prepare a :class:`RouteProblem`, defaulting the first stop to the driver's selection.

    An omitted ``first_stop_id`` is only filled in from an explicit driver selection; the domain
    never chooses a first stop on the driver's behalf (I4/D32). A supplied ``cache`` is reused, so
    a caller evaluating several candidates keeps one shared leg cache (v2 section 20).
    """
    selected = (
        first_stop_id if first_stop_id is not None else plan.first_service_stop.selected_stop_id
    )
    if selected is None:
        raise InvalidRoutePlanError(
            "optimization needs an explicit driver-selected first service stop: the plan is "
            "'awaiting_first_stop_choice', which is a valid state, not an error (D4/I4, D32)"
        )
    legs = cache if cache is not None else LegCache(travel_matrix)
    return RouteProblem(plan=plan, legs=legs, first_stop_id=selected)