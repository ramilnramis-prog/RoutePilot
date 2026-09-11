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
from functools import lru_cache
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
    "fast_objective",
    "fast_route_evaluation",
    "require_complete_route",
    "route_objective_key",
    "timelines_of",
    "violations_of",
]

#: How many local service dates past the departure date the window table always covers. A route
#: whose arrivals leave that horizon still works: the lookup falls back to the authoritative
#: resolver instead of guessing.
_MIN_PRECOMPUTED_DATES = 8

#: A window close is found within this many days of the earliest possible arrival.
_MAX_DAY_SEARCH = 40


@lru_cache(maxsize=8192)
def _resolve_local_cached(zone: Any, ordinal: int, wall_time: time) -> Instant | None:
    """One local wall-clock time on one local service date, resolved through :mod:`core.time.tz`.

    A pure function of (IANA zone, date, wall time), memoized process-wide. It is the resolution
    :func:`core.time.tz.resolve_local_datetime` performs, and ``None`` means exactly what that
    function's :class:`~core.validation.errors.DSTValidationError` means: the local time does not
    exist (or is ambiguous) on that date, so the problem leaves the entry unresolved and the
    authoritative resolver answers a real arrival instead (D3). The cache carries no route
    decision - every answer is a converted instant - so sharing it across the ~100 problems of the
    exhaustive first-stop loop changes no result (U3).
    """
    try:
        return tz.resolve_local_datetime(date.fromordinal(ordinal), wall_time, zone)
    except DSTValidationError:
        return None


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
    #: The arrival clock (whole seconds from the departure time) of each entry of
    #: :attr:`cutoff_instants`. The route passes bisect these integers instead of building a
    #: ``datetime`` and a ``timedelta`` per stop, which is the same comparison on the same prepared
    #: boundaries - see :meth:`day_offset_of` (U3 recovery: measured bottleneck, no new answer).
    cutoff_elapsed: tuple[DurationSec, ...] = field(default=(), compare=False, repr=False)
    #: For each entry of :attr:`cutoff_instants`, the prepared day offset the table can answer with,
    #: or ``None`` when it cannot: the arrival is on the last prepared date, or the next day's local
    #: midnight does not exist. It is the per-stop check the route passes used to repeat.
    step_offsets: tuple[int | None, ...] = field(default=(), compare=False, repr=False)
    window_table: dict[tuple[StopId, int], ResolvedWindow] = field(
        default_factory=dict, compare=False, repr=False
    )
    fallback_windows: dict[tuple[StopId, date], ResolvedWindow] = field(
        default_factory=dict, compare=False, repr=False
    )
    #: Each enabled stop's own service window, in stop order (``None`` when it is not fixed). It is
    #: immutable plan data, so resolving it once here keeps the per-arrival lookup free of a plan
    #: scan (``plan.stop_by_id`` is O(n) and the fast path asks about a stop thousands of times).
    fixed_windows: tuple[ServiceWindow | None, ...] = field(
        default=(), compare=False, repr=False
    )
    #: Windows already resolved for one (stop, precomputed day offset); see :meth:`window_for`.
    day_windows: dict[tuple[StopId, int], ResolvedWindow] = field(
        default_factory=dict, compare=False, repr=False
    )
    #: ``window_open_elapsed[stop_index]`` = the arrival clock value (seconds from departure) from
    #: which that stop's window is open, or ``None`` when the stop has no resolved window on the
    #: prepared date. It is derived, never a second reading of the timezone: see
    #: :meth:`window_bounds_at`.
    window_open_elapsed: tuple[int | None, ...] = field(default=(), compare=False, repr=False)
    #: ``window_close_elapsed[stop_index]`` - the same for the window's close.
    window_close_elapsed: tuple[int | None, ...] = field(default=(), compare=False, repr=False)
    #: The day offset the two arrays above were resolved for, or ``None`` when they are empty.
    window_bounds_offset: int | None = field(default=None, compare=False, repr=False)
    #: The same day's windows as a plain dict, so :meth:`window_for` stays one lookup on any
    #: arrival: it is the same answer as the precomputed table, resolved once per prepared date.
    window_bounds: dict[StopId, ResolvedWindow] = field(
        default_factory=dict, compare=False, repr=False
    )
    fallbacks_used: int = field(default=0, compare=False)
    service_problems: dict[StopId, str] = field(default_factory=dict, compare=False, repr=False)
    stop_index: dict[StopId, int] = field(default_factory=dict, compare=False, repr=False)
    #: The flat, frozen stop-to-stop travel snapshot; read it through :attr:`travel_time_table`.
    #: U3's route pass uses the wider table built by :meth:`_extended_travel_table` instead, which
    #: adds START's and FINISH's rows so no leg is ever looked up twice. The two tables are kept in
    #: separate fields on purpose: the wider one must never overwrite this one, or
    #: :attr:`travel_time_table` and :meth:`travel_between_indices` would read a stop-to-stop cell at
    #: the wrong stride (a stale value) as soon as a route pass had built the wider table.
    _travel_table: tuple[int, ...] = field(default=(), compare=False, repr=False)
    #: The wider ``(stop_count + 2) ** 2`` snapshot the route passes read, built on first use by
    #: :meth:`_extended_travel_table`; it includes START's and FINISH's own rows.
    _full_travel_table: tuple[int, ...] = field(default=(), compare=False, repr=False)
    #: ``_distance_table[origin * count + destination]`` - the same flat snapshot as
    #: :attr:`travel_table`, for metric distance. It is built from the shared leg cache on the same
    #: first use, so the route pass never asks the cache for a leg the table already holds (U3).
    #: Read it through :attr:`distance_table`.
    _distance_table: tuple[float, ...] = field(default=(), compare=False, repr=False)
    #: ``(START -> stop distances, FINISH -> stop distances)``, prepared once; see
    #: :meth:`_distance_ends_row`.
    _distance_ends: tuple[tuple[float, ...], tuple[float, ...]] = field(
        default=(), compare=False, repr=False
    )
    #: ``window_bounds_rows[offset][index]`` - the resolved ``(open, close)`` of one stop's window
    #: on one prepared day offset, on the arrival clock. It is the same resolution the precomputed
    #: table holds (see :meth:`window_bounds_for_index`), prepared for every day offset at once so
    #: the route pass is one tuple index per stop (U3).
    window_bounds_rows: dict[int, tuple[tuple[int, int] | None, ...]] = field(
        default_factory=dict, compare=False, repr=False
    )

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
        object.__setattr__(
            self,
            "fixed_windows",
            tuple(
                stop.service_window if stop.service_window.is_fixed else None for stop in stops
            ),
        )
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
        object.__setattr__(
            self, "cutoff_elapsed", tuple(self.elapsed_of(midnight) for midnight in instants)
        )
        steps: list[int | None] = []
        for offset in offsets:
            following = offset + 1
            if following >= len(cutoffs) or cutoffs[following] is None:
                # The arrival is on (or beyond) the last prepared date, or the next day's local
                # midnight does not exist: the prepared table cannot answer for that day.
                steps.append(None)
            else:
                steps.append(offset)
        object.__setattr__(self, "step_offsets", tuple(steps))

        table: dict[tuple[StopId, int], ResolvedWindow] = {}
        # One memo for the whole plan: the benchmark's stops share a handful of opening and closing
        # times, and resolving the same (local date, wall-clock time) through the timezone rules
        # once per plan instead of once per stop keeps preparing a ~100-stop problem cheap.
        resolved_local: dict[tuple[date, time], Instant | None] = {}
        for stop_id in self.stop_ids:
            window = plan.stop_by_id(stop_id).service_window
            if not window.is_fixed:
                continue  # nothing to resolve, nothing invented
            start_local = window.start_local
            end_local = window.end_local
            for offset in range(days):
                service_date = date.fromordinal(departure_date.toordinal() + offset)
                open_at = self._resolve_local(start_local, service_date, resolved_local)
                close_at = self._resolve_local(end_local, service_date, resolved_local)
                if open_at is None or close_at is None:
                    # This window does not exist on that service date (D3). It is left out so the
                    # authoritative resolver answers - and raises - for a real arrival there.
                    continue
                table[(stop_id, offset)] = ResolvedWindow(open_at=open_at, close_at=close_at)
        object.__setattr__(self, "window_table", table)

    def _resolve_local(
        self,
        wall_time: time | None,
        service_date: date,
        memo: dict[tuple[date, time], Instant | None],
    ) -> Instant | None:
        """A local window time on one service date, memoized; ``None`` when it does not exist.

        The resolution itself is :func:`core.time.tz.resolve_local_datetime` and nothing else - no
        DST rule is written here. The process-wide memo keyed on (zone, date, wall time) exists
        because the exhaustive first-stop loop of v2 section 20 prepares ~100 problems that all
        ask the same question; the answer is a pure function of those three values, so sharing it
        changes no result and removes the repeated timezone lookup (U3).
        """
        if wall_time is None:  # pragma: no cover - a fixed window always carries both
            return None
        key = (service_date, wall_time)
        if key in memo:
            return memo[key]
        resolved = _resolve_local_cached(self.tzinfo, service_date.toordinal(), wall_time)
        memo[key] = resolved
        return resolved

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
            end_local = window.end_local
            earliest_arrival = self.plan.departure_time + timedelta(
                seconds=self.travel_from_departure[index]
            )
            arrival_date = tz.local_date_of(earliest_arrival, tzinfo)
            for step in range(_MAX_DAY_SEARCH):
                service_ordinal = arrival_date.toordinal() + step
                close_at = _resolve_local_cached(self.tzinfo, service_ordinal, end_local)
                if close_at is None:
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

    @property
    def travel_time_table(self) -> tuple[int, ...]:
        """Every stop-to-stop leg in one flat tuple, prepared once and shared by every candidate.

        The optimizer's hot loops rank *moves* - 2-opt and relocate candidates of the same stop
        set - and the first-stop candidate evaluation compares the same stops over and over. Ask
        the leg cache for each of those legs once, in a fixed order, and the ranking becomes array
        indexing instead of a dict lookup keyed on two ``GeoPoint`` values, which is what makes the
        exhaustive loop of v2 section 20 affordable at ~100 stops.

        Two properties are deliberately preserved: the table is built **through the shared
        :class:`~core.engine.optimizer.cache.LegCache`**, so the reuse is measured with everything
        else instead of hiding next to it; and it is a *frozen* snapshot of the same answers the
        cache would return, so a route decision can never differ from one taken through the cache.

        Indices ``0 .. stop_count - 1`` are the enabled stops, in stop order, and this table is
        **stop-to-stop only**: START's and FINISH's rows are the wider
        :meth:`_extended_travel_table` (see :meth:`index_of` for the general accessor). Keeping the
        two tables apart is what makes :meth:`travel_between_indices` answer with this table's own
        stride no matter which of them a route pass built first.
        """
        built = self._travel_table
        if built:
            return built
        count = len(self.stop_ids)
        table = [0] * (count * count)
        for origin in range(count):
            origin_point = self.locations[origin]
            base = origin * count
            for destination in range(count):
                table[base + destination] = self.legs.travel_time_seconds(
                    origin_point, self.locations[destination]
                )
        result = tuple(table)
        object.__setattr__(self, "_travel_table", result)
        return result

    @property
    def start_index(self) -> int:
        """The table index of START, the departure point: never a service stop (I1)."""
        return len(self.stop_ids)

    @property
    def finish_index(self) -> int:
        """The table index of FINISH: fixed, and never reordered like a stop (I2)."""
        return len(self.stop_ids) + 1

    @property
    def distance_table(self) -> tuple[float, ...]:
        """Every stop-to-stop distance in one flat tuple, prepared once (U3).

        The same snapshot :attr:`travel_table` is, for the other metric the authoritative
        evaluation reports. It is built through the shared :class:`~core.engine.optimizer.cache.LegCache`
        on first use, so the reuse is measured exactly like every other leg question.
        """
        built = self._distance_table
        if built:
            return built
        count = len(self.stop_ids)
        table = [0.0] * (count * count)
        for origin in range(count):
            origin_point = self.locations[origin]
            base = origin * count
            for destination in range(count):
                table[base + destination] = self.legs.distance_meters(
                    origin_point, self.locations[destination]
                )
        result = tuple(table)
        object.__setattr__(self, "_distance_table", result)
        return result

    def _distance_ends_row(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """Distances from START and from FINISH to every enabled stop, prepared once (U3).

        The distance counterpart of START's and FINISH's rows in the travel table: the route pass
        reads distances by index instead of asking the shared cache for the same two legs at every
        stop. Both rows are built through that cache on first use, so the reuse stays measured.
        """
        built = self._distance_ends
        if built:
            return built
        departure = self.departure_point
        finish = self.finish_point
        locations = self.locations
        legs = self.legs
        rows = (
            tuple(legs.distance_meters(departure, point) for point in locations),
            tuple(legs.distance_meters(finish, point) for point in locations),
        )
        object.__setattr__(self, "_distance_ends", rows)
        return rows

    def distance_between_indices(self, origin: int, destination: int) -> float:
        """Distance in metres between two points of the prepared problem, by their table index.

        The same indices :meth:`travel_between_indices` accepts, answered from the frozen
        :attr:`distance_table` for two service stops and through the shared cache at START/FINISH.
        """
        count = len(self.stop_ids)
        if origin < count and destination < count:
            built = self._distance_table
            if built:
                return built[origin * count + destination]
            return self.legs.distance_meters(self.locations[origin], self.locations[destination])
        origin_point = (
            self.departure_point
            if origin == count
            else self.finish_point if origin > count else self.locations[origin]
        )
        destination_point = (
            self.departure_point
            if destination == count
            else self.finish_point if destination > count else self.locations[destination]
        )
        return self.legs.distance_meters(origin_point, destination_point)

    def travel_between_indices(self, origin: int, destination: int) -> DurationSec:
        """Travel in seconds between two points of the prepared problem, by their table index.

        ``origin``/``destination`` are stop indices, :attr:`start_index` or :attr:`finish_index`,
        so a route edge is priced without building a ``GeoPoint`` key or a cache lookup. The
        answers are the same ones the shared cache holds (see :attr:`travel_time_table`).
        """
        count = len(self.stop_ids)
        if origin < count and destination < count:
            built = self._travel_table
            if built:
                return built[origin * count + destination]
            return self.legs.travel_time_seconds(
                self.locations[origin], self.locations[destination]
            )
        origin_point = (
            self.departure_point
            if origin == count
            else self.finish_point if origin > count else self.locations[origin]
        )
        destination_point = (
            self.departure_point
            if destination == count
            else self.finish_point if destination > count else self.locations[destination]
        )
        return self.legs.travel_time_seconds(origin_point, destination_point)

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
        index = bisect.bisect_right(self.cutoff_elapsed, elapsed_sec) - 1
        if index < 0:
            return None
        return self.step_offsets[index]

    def window_for(self, stop_id: StopId, elapsed_sec: DurationSec) -> ResolvedWindow | None:
        """The stop's window for the local service date of that arrival.

        Answered from the precomputed table. When the table cannot answer - the arrival leaves
        the prepared dates, or its date has no resolvable local midnight - the window is resolved
        authoritatively by :func:`core.time.tz.resolve_service_window`, the same call the
        authoritative timeline makes, and memoized, never guessed. A window that does not exist on
        that service date (a DST gap) therefore raises here exactly as it does in
        :func:`core.time.timeline.compute_stop_timeline` (D3).

        Two caches keep the hot loop cheap without changing a single answer: the stop's own window
        is resolved once per stop (``fixed_windows``), and the windows of one prepared local
        service date are turned into arrival-clock bounds once (see :meth:`window_bounds_at`). A
        window depends on the *date* of an arrival, not on the arrival time, so a route that
        reaches the same stop on the same date - which every candidate evaluation does - pays once.
        """
        index = self.index_of(stop_id)
        if self.fixed_windows[index] is None:
            return None
        offset = self.day_offset_of(elapsed_sec)
        if offset is not None:
            bounds = self.window_bounds if self.window_bounds_offset == offset else None
            if bounds is not None:
                return bounds.get(stop_id)
            known = self.day_windows.get((stop_id, offset))
            if known is not None:
                return known
            table_entry = self.window_table.get((stop_id, offset))
            if table_entry is not None:
                return table_entry

        arrival = self.plan.departure_time + timedelta(seconds=elapsed_sec)
        service_date = tz.local_date_of(arrival, self.tzinfo)
        cached = self.fallback_windows.get((stop_id, service_date))
        if cached is not None:
            return cached
        resolved = tz.resolve_service_window(
            self.fixed_windows[index], service_date, self.tzinfo
        )
        if resolved is None:  # pragma: no cover - a fixed window always resolves
            return None
        entry = ResolvedWindow(open_at=resolved.open_at, close_at=resolved.close_at)
        self.fallback_windows[(stop_id, service_date)] = entry
        object.__setattr__(self, "fallbacks_used", self.fallbacks_used + 1)
        return entry

    def window_bounds_at(
        self, stop_id: StopId, elapsed_sec: DurationSec
    ) -> tuple[int, int] | None:
        """``(open, close)`` of the stop's window **on the arrival clock**, or ``None``.

        The fast path's own view of a window: the same instants :meth:`window_for` returns, already
        expressed as whole seconds from the departure time, so a prescanned route does not convert
        two instants per stop. Built for one prepared day offset at a time and only when the
        precomputed table can answer, so the authoritative resolver stays the single source of
        every window (this is a conversion of its result, never a second calculation of it).
        """
        offset = self.day_offset_of(elapsed_sec)
        if offset is None:
            return None
        return self.window_bounds_for_index(self.index_of(stop_id), offset)

    def window_bounds_for_index(self, index: int, offset: int) -> tuple[int, int] | None:
        """The arrival-clock window bounds of one enabled stop for one prepared day offset."""
        if self.fixed_windows[index] is None:
            return None
        known = self.window_bounds_rows.get(offset)
        if known is None:
            self._build_window_bounds(offset)
            known = self.window_bounds_rows[offset]
        return known[index]

    def _build_window_bounds(self, offset: int) -> None:
        """Turn the prepared windows of one day offset into arrival-clock bounds for every stop.

        The result is stored twice: as the row :meth:`window_bounds_row` hands to the route pass
        (one tuple index per stop), and as the per-stop ``window_bounds`` mapping
        :meth:`window_for` answers from. Both are conversions of the same precomputed resolution,
        never a second reading of the timezone.
        """
        departure = self.plan.departure_time
        opens: list[int | None] = []
        closes: list[int | None] = []
        row: list[tuple[int, int] | None] = []
        bounds: dict[StopId, ResolvedWindow] = {}
        for index, stop_id in enumerate(self.stop_ids):
            if self.fixed_windows[index] is None:
                opens.append(None)
                closes.append(None)
                row.append(None)
                continue
            resolved = self._day_window(stop_id, offset)
            if resolved is None:
                opens.append(None)
                closes.append(None)
                row.append(None)
                continue
            bounds[stop_id] = resolved
            open_elapsed = int((resolved.open_at - departure).total_seconds())
            close_elapsed = int((resolved.close_at - departure).total_seconds())
            opens.append(open_elapsed)
            closes.append(close_elapsed)
            row.append((open_elapsed, close_elapsed))
        object.__setattr__(self, "window_open_elapsed", tuple(opens))
        object.__setattr__(self, "window_close_elapsed", tuple(closes))
        object.__setattr__(self, "window_bounds", bounds)
        object.__setattr__(self, "window_bounds_offset", offset)
        object.__setattr__(self, "window_bounds_rows", {**self.window_bounds_rows, offset: tuple(row)})

    def _day_window(self, stop_id: StopId, offset: int) -> ResolvedWindow | None:
        """The stop's window for one day offset, or ``None``.

        The precomputed table answers every prepared date, which is the normal case and the only
        one the hot loop pays for. When it cannot - a problem whose table was deliberately emptied
        (:meth:`without_precomputed_windows`), or a date the horizon does not cover - the window is
        resolved authoritatively by :func:`core.time.tz.resolve_service_window`, exactly as
        :meth:`window_for` resolves it, and memoized under the same key so the two agree by
        construction. The resolution is counted in ``fallbacks_used``, so the fast path's use of
        the authoritative resolver stays measurable rather than hidden (U2 review issue 1).
        """
        entry = self.window_table.get((stop_id, offset))
        if entry is not None:
            return entry
        known = self.day_windows.get((stop_id, offset))
        if known is not None:
            return known
        index = self.index_of(stop_id)
        window = self.fixed_windows[index]
        if window is None:
            return None
        service_date = tz.local_date_of(
            self.plan.departure_time + timedelta(days=offset), self.tzinfo
        )
        resolved = tz.resolve_service_window(window, service_date, self.tzinfo)
        if resolved is None:  # pragma: no cover - a fixed window always resolves
            return None
        entry = ResolvedWindow(open_at=resolved.open_at, close_at=resolved.close_at)
        self.day_windows[(stop_id, offset)] = entry
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

    def _try_advance(
        self, previous: int, elapsed: int, index: int, *, require_serviceable: bool = True
    ) -> tuple[int, int, int]:
        """Serve one stop from a service state, in the hot loop's own terms.

        ``previous`` is the prepared index the driver is standing at and ``elapsed`` the whole
        seconds since departure at that moment; the return is
        ``(service_finish_elapsed, waiting_sec, violations)``. It is the same arithmetic
        :meth:`advance` performs - the prepared arrival-clock window when the table can answer,
        :meth:`window_for` otherwise - plus the waiting and the hard-window outcome the seed
        compares on, so a candidate is priced without building an :class:`OpenState` or resolving
        the window twice.

        ``require_serviceable=False`` is for callers that have already run
        :func:`require_complete_route` once for the whole problem (the seed's inner loop); the
        public :meth:`advance` always checks.
        """
        if require_serviceable:
            self.require_serviceable(self.stop_ids[index])
        table = self._extended_travel_table()
        sizes = len(self.stop_ids) + 2
        elapsed += table[previous * sizes + index]
        duration = self.duration_by_stop[index]
        step = bisect.bisect_right(self.cutoff_elapsed, elapsed) - 1
        offset = self.step_offsets[step] if step >= 0 else None
        window_row = None if offset is None else self.window_bounds_rows.get(offset)
        if window_row is None and offset is not None:
            window_row = self.window_bounds_row(offset)
        bounds = None if window_row is None else window_row[index]
        waiting = 0
        violations = 0

        if bounds is None:
            window = self.window_for(self.stop_ids[index], elapsed)
            if window is not None:
                opened = self.elapsed_of(window.open_at)
                if opened > elapsed:
                    waiting = opened - elapsed
                    elapsed = opened
                closed = self.elapsed_of(window.close_at)
                if self.window_end_policies[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                    if elapsed + duration > closed:
                        violations = 1
                elif elapsed > closed:
                    violations = 1
        else:
            opened, closed = bounds
            if opened > elapsed:
                waiting = opened - elapsed
                elapsed = opened
            if self.window_end_policies[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                if elapsed + duration > closed:
                    violations = 1
            elif elapsed > closed:
                violations = 1
        return (elapsed + duration, waiting, violations)

    def require_serviceable(self, stop_id: StopId) -> None:
        """Raise the authoritative error for a stop no complete route could serve."""
        problem = self.service_problems.get(stop_id)
        if problem is None:
            return
        stop = self.plan.stop_by_id(stop_id)
        if stop.location is None:
            raise StopNotGeocodedError(stop.id, stop.geocode_status.value)
        raise MissingServiceDurationError(stop.id)

    def window_bounds_row(self, offset: int) -> tuple[tuple[int, int] | None, ...]:
        """The arrival-clock ``(open, close)`` of **every** stop for one prepared day offset.

        ``row[index]`` is exactly what :meth:`window_bounds_for_index` returns for that index, and
        ``None`` where that method returns ``None`` - a stop without a fixed window, or a window
        this offset has no resolution for. It is prepared once per day offset so a route pass reads
        one tuple index per stop instead of resolving a window per stop (U3). The resolution itself
        is unchanged and stays the precomputed table's (see :meth:`_build_window_bounds`).
        """
        known = self.window_bounds_rows.get(offset)
        if known is not None:
            return known
        self._build_window_bounds(offset)
        return self.window_bounds_rows[offset]

    def route_objective_key(
        self, order: Sequence[StopId], *, require_serviceable: bool = True
    ) -> tuple[int, int]:
        """The acceptance key ``(violations, elapsed seconds)`` of a complete order, table-only.

        This is :func:`fast_objective` made affordable for the hot loops of U3 - the seed's
        per-candidate complete-route comparison and the local search's verification pass - without
        changing a single answer:

        * every leg is read from the frozen :attr:`travel_table` (plus START's and FINISH's rows,
          built through the same shared cache), so no leg question is repeated and no ``GeoPoint``
          is hashed;
        * every window is read from the prepared :meth:`window_bounds_row`, which is the same
          resolution :meth:`window_for` returns; a stop whose window the prepared rows cannot
          answer falls back to :meth:`window_for`, which resolves authoritatively or raises
          exactly as :func:`core.engine.optimizer.route_evaluation.evaluate_order` does;
        * nothing else is computed: no distance, no travel/waiting/service breakdown, and no
          per-stop index dictionary.

        ``require_serviceable`` is the caller's declaration that the problem has already been
        checked with :func:`require_complete_route`. Every public entry point passes ``True`` (the
        default) and pays that check; only the seed's inner loop, after one explicit check, passes
        ``False`` - a stop no complete route could serve is never silently priced either way.
        """
        if require_serviceable:
            _require_complete(self, order)
        return self._route_key(order)

    def _route_key(self, order: Sequence[StopId]) -> tuple[int, int]:
        """The unchecked forward pass behind :meth:`route_objective_key`."""
        return self._route_from(self.start_index, 0, order)[0]

    def _extended_travel_table(self) -> tuple[int, ...]:
        """The stop-to-stop **and** STOP-to-START/FINISH travel snapshot, built on first use.

        The stop-to-stop part is :attr:`travel_time_table`; this wider table adds START's and
        FINISH's rows, so the route passes price every leg - including the last one to FINISH -
        with one index instead of a cache question. It is built through the same shared
        :class:`~core.engine.optimizer.cache.LegCache`, so the reuse stays measured, and it is a
        frozen snapshot of the same answers, so a route can never differ from one priced through
        the cache.

        **Every cell is the real leg** of the pair its row and column name, FINISH's row included:
        ``table[finish_index * size + destination]`` is FINISH's own leg to that destination, asked
        of the shared cache exactly like every other cell. The row used to be filled with a repeated
        FINISH-to-START leg - a fabricated constant that was neither FINISH's row nor any leg of the
        destination it sat under - so a route pass that ever read it would have silently priced a
        leg that does not exist (U3 owner-decided fix 4). No pass reads it today; with real legs, a
        pass that ever does reads the truth instead of a copied unrelated value.

        This table lives in its own field: it never overwrites the stop-to-stop
        :attr:`travel_time_table`, whose readers use the shorter stride.
        """
        count = len(self.stop_ids)
        size = count + 2
        built = self._full_travel_table
        if len(built) == size * size:
            return built
        departure = self.departure_point
        finish = self.finish_point
        locations = self.locations
        legs = self.legs
        flat = self.travel_time_table
        table = [0] * (size * size)
        for origin in range(size):
            base = origin * size
            if origin == count:
                origin_point = departure
                for destination in range(count):
                    table[base + destination] = legs.travel_time_seconds(
                        origin_point, locations[destination]
                    )
                table[base + count] = legs.travel_time_seconds(origin_point, departure)
                table[base + count + 1] = legs.travel_time_seconds(origin_point, finish)
                continue
            if origin == count + 1:
                # FINISH's own row: the real leg from FINISH to each destination, so no cell of
                # this table is a copy of an unrelated leg.
                for destination in range(count):
                    table[base + destination] = legs.travel_time_seconds(
                        finish, locations[destination]
                    )
                table[base + count] = legs.travel_time_seconds(finish, departure)
                table[base + count + 1] = legs.travel_time_seconds(finish, finish)
                continue
            origin_point = locations[origin]
            for destination in range(count):
                table[base + destination] = flat[origin * count + destination]
            table[base + count] = legs.travel_time_seconds(origin_point, departure)
            table[base + count + 1] = legs.travel_time_seconds(origin_point, finish)
        result = tuple(table)
        object.__setattr__(self, "_full_travel_table", result)
        return result

    def _route_from(
        self,
        previous: int,
        elapsed: int,
        order: Sequence[StopId],
        *,
        table: tuple[int, ...] | None = None,
    ) -> tuple[tuple[int, int], int, int]:
        """Price ``order`` from a service state already reached (an unchecked forward pass).

        ``previous`` is the prepared index the driver is standing at (START, or a stop just served)
        and ``elapsed`` the whole seconds since departure at that moment. Returns
        ``((violations, finish_elapsed), end_elapsed, end_index)``: the acceptance key of the
        complete route ``... -> order -> FINISH``, and the state it leaves behind, so a caller can
        continue the same pass without re-pricing what it has already committed to.

        Every leg is one index into the prepared travel table; every window is the prepared
        arrival-clock bound of the service date the arrival falls on, with
        :meth:`window_for` deciding (and raising, exactly as ``evaluate_order`` does) whenever the
        prepared table cannot answer for that arrival. No distance, no breakdown and no
        per-candidate serviceability scan is computed - this is the hot-loop pass of U3, and it is
        only ever called on a problem the caller has already established to be serviceable.
        """
        if table is None:
            table = self._extended_travel_table()
        count = len(self.stop_ids)
        sizes = count + 2
        durations = self.duration_by_stop
        finish_before = self.window_end_policies
        index_of = self.stop_index.get
        cutoffs = self.cutoff_elapsed
        step_offsets = self.step_offsets
        rows_get = self.window_bounds_rows.get
        violations = 0

        for stop_id in order:
            index = index_of(stop_id)
            elapsed += table[previous * sizes + index]
            duration = durations[index]
            # The same day offset ``window_bounds_at`` computes, inlined: one bisect over the
            # prepared local midnights on the arrival clock, and the row it selects is the
            # resolution the precomputed table holds.
            step = bisect.bisect_right(cutoffs, elapsed) - 1
            offset = step_offsets[step] if step >= 0 else None
            window_bounds = None if offset is None else rows_get(offset)
            if window_bounds is None and offset is not None:
                window_bounds = self.window_bounds_row(offset)
            bounds = None if window_bounds is None else window_bounds[index]

            if bounds is None:
                # The prepared rows cannot answer for this stop (no fixed window, or a window this
                # date has no resolution for): the authoritative resolver decides, and raises for
                # exactly the arrivals ``evaluate_order`` rejects.
                window = self.window_for(stop_id, elapsed)
                if window is not None:
                    opened = self.elapsed_of(window.open_at)
                    if opened > elapsed:
                        elapsed = opened
                    closed = self.elapsed_of(window.close_at)
                    if finish_before[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                        if elapsed + duration > closed:
                            violations += 1
                    elif elapsed > closed:
                        violations += 1
            else:
                opened, closed = bounds
                if opened > elapsed:
                    elapsed = opened
                if finish_before[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                    if elapsed + duration > closed:
                        violations += 1
                elif elapsed > closed:
                    violations += 1
            elapsed += duration
            previous = index

        return ((violations, elapsed + table[previous * sizes + count + 1]), elapsed, previous)

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
        # The prepared arrival-clock rows are a conversion of the table above, so they go with it:
        # the copy resolves every window authoritatively instead of reusing a prepared answer.
        object.__setattr__(copy, "window_bounds_rows", {})
        object.__setattr__(copy, "window_bounds", {})
        object.__setattr__(copy, "window_bounds_offset", None)
        return copy


# --------------------------------------------------------------------------- #
# the fast complete-route pass
# --------------------------------------------------------------------------- #
def _route_cost(
    problem: RouteProblem, order: Sequence[StopId], *, with_distance: bool = True
) -> tuple[int, int, int, int, float, int]:
    """The shared forward pass: ``(violations, finish_elapsed, travel, waiting, distance, service)``.

    This is the single implementation of the fast route arithmetic - :func:`fast_objective` and
    :func:`fast_evaluate` are two views of it, so they cannot drift apart, and neither can drift
    from :func:`core.engine.optimizer.route_evaluation.evaluate_order` (the module docstring says
    how that agreement is proven and tested).

    Every leg and every distance is read from the prepared problem's frozen tables (U3), which hold
    the answers the shared :class:`~core.engine.optimizer.cache.LegCache` gave, so the pass is
    array indexing instead of a per-stop cache question. Windows come from the same prepared
    day-offset rows the seed uses (:meth:`RouteProblem.window_bounds_row`), whose values are the
    instants :meth:`RouteProblem.window_for` resolves - one timezone resolution per stop and service
    date, never a guess. An arrival the prepared table cannot place (beyond its horizon, or on a
    date with no resolvable local midnight) still goes through :meth:`RouteProblem.window_for`,
    which resolves it authoritatively or raises exactly as ``evaluate_order`` does.
    """
    count = len(problem.stop_ids)
    table = problem._extended_travel_table()
    sizes = count + 2
    distances = problem.distance_table if with_distance else ()
    if with_distance:
        starts_to_stop, _finish_to_stop = problem._distance_ends_row()
        to_finish_distance = problem.distance_to_finish
    else:
        starts_to_stop, to_finish_distance = (), ()
    durations = problem.duration_by_stop
    policies = problem.window_end_policies
    index_of = problem.stop_index.get
    cutoffs = problem.cutoff_elapsed
    step_offsets = problem.step_offsets
    rows_get = problem.window_bounds_rows.get
    elapsed = 0
    travel_sec = 0
    waiting_sec = 0
    service_sec = 0
    distance_m = 0.0
    violations = 0
    previous = count

    for stop_id in order:
        index = index_of(stop_id)
        leg_travel = table[previous * sizes + index]
        travel_sec += leg_travel
        elapsed += leg_travel
        duration = durations[index]
        step = bisect.bisect_right(cutoffs, elapsed) - 1
        offset = step_offsets[step] if step >= 0 else None
        window_row = None if offset is None else rows_get(offset)
        if window_row is None and offset is not None:
            window_row = problem.window_bounds_row(offset)
        bounds = None if window_row is None else window_row[index]

        if bounds is None:
            window = problem.window_for(stop_id, elapsed)
            if window is None:
                waiting = 0
                service_start = elapsed
            else:
                opened = problem.elapsed_of(window.open_at)
                waiting = opened - elapsed if opened > elapsed else 0
                service_start = elapsed + waiting
                closed = problem.elapsed_of(window.close_at)
                if policies[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                    lateness = service_start + duration - closed
                else:
                    lateness = service_start - closed
                if lateness > 0:
                    violations += 1
        else:
            opened, closed = bounds
            waiting = opened - elapsed if opened > elapsed else 0
            service_start = elapsed + waiting
            if policies[index] is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                lateness = service_start + duration - closed
            else:
                lateness = service_start - closed
            if lateness > 0:
                violations += 1

        waiting_sec += waiting
        service_sec += duration
        if with_distance:
            distance_m += (
                distances[previous * count + index]
                if previous < count
                else starts_to_stop[index]
            )
        elapsed = service_start + duration
        previous = index

    finish_travel = table[previous * sizes + count + 1]
    if with_distance:
        distance_m += to_finish_distance[previous] if previous < count else 0.0
    return (
        violations,
        elapsed + finish_travel,
        travel_sec + finish_travel,
        waiting_sec,
        distance_m,
        service_sec,
    )


def _require_complete(problem: RouteProblem, order: Sequence[StopId]) -> None:
    """Refuse exactly the orders :func:`evaluate_order` refuses, before any leg is priced.

    A stop that no complete route could serve is reported with the authoritative error, not
    silently priced at the departure point or at zero seconds: every stop of the order first, in
    route order, so the reported error matches ``evaluate_order`` for the same order, and then
    every enabled stop of the plan, so a partial pass can never stand in for a complete route the
    engine would reject (``tests/engine/test_optimizer.py``).
    """
    for stop_id in order:
        problem.require_serviceable(stop_id)
    for stop_id in problem.stop_ids:
        problem.require_serviceable(stop_id)


def require_complete_route(problem: RouteProblem, order: Sequence[StopId]) -> None:
    """Whether ``order`` can stand in for a complete route: the public form of the check above."""
    _require_complete(problem, order)


def route_objective_key(
    problem: RouteProblem, order: Sequence[StopId], *, require_serviceable: bool = True
) -> tuple[int, int]:
    """The acceptance key of a complete order, priced from the prepared tables (U3).

    The module-level form of :meth:`RouteProblem.route_objective_key`: the same number
    :func:`fast_objective` returns, for the hot loops that have already established that every
    enabled stop is serviceable and may therefore pass ``require_serviceable=False``.
    """
    return problem.route_objective_key(order, require_serviceable=require_serviceable)


def fast_objective(problem: RouteProblem, order: Sequence[StopId]) -> tuple[int, int]:
    """The **acceptance key** of a complete order - ``(violations, elapsed seconds)`` - and nothing else.

    Every decision the optimizer makes (the seed's next stop, the acceptance of a local-search
    move) is decided by exactly this pair (v2 section 21, D13 amendment). Computing only the pair
    keeps the hot loop small: travel, waiting, service and distance are reported by
    :func:`fast_evaluate`, which measures the same route through the same arithmetic, and neither
    number may take part in a decision. Metric distance is the one component deliberately not
    accumulated here, and it is the only reason this call is cheaper than :func:`fast_evaluate`.

    It is answered by :meth:`RouteProblem.route_objective_key`, the table-only form of the same
    forward pass, so the seed and the local search can price thousands of candidates without
    repeating a leg question - and still get exactly the number :func:`fast_evaluate` reports.

    The FINISH leg is included, so the objective is the complete route of v2 section 15. A stop no
    complete route could serve is refused with the authoritative error first, exactly as
    :func:`core.time.timeline.compute_stop_timeline` does.
    """
    return problem.route_objective_key(order)


def fast_evaluate(problem: RouteProblem, order: Sequence[StopId]) -> FastEvaluation:
    """Evaluate a complete order in integer seconds, without a per-stop timezone conversion.

    The FINISH leg is included (v2 section 15). The result agrees with
    :func:`core.engine.optimizer.route_evaluation.evaluate_order` for the applied
    ``window_end_policy``; the module docstring says how that is proven.

    A stop that no complete route could serve is refused with the authoritative error *before* it
    is priced, exactly as :func:`core.time.timeline.compute_stop_timeline` does - a missing
    location is never substituted by the departure point and a missing duration is never zero.
    """
    _require_complete(problem, order)
    fallbacks_before = problem.fallbacks_used
    violations, finish_elapsed, travel_sec, waiting_sec, distance_m, service_sec = _route_cost(
        problem, order
    )
    return FastEvaluation(
        order=tuple(order),
        violations=violations,
        travel_sec=travel_sec,
        waiting_sec=waiting_sec,
        service_sec=service_sec,
        distance_m=distance_m,
        finish_elapsed_sec=finish_elapsed,
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