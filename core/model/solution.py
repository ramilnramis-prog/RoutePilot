"""Computed route results: timelines, violations, metrics, solution (spec section 7, D13).

Everything in this module is **derived**: a timeline is a projection of
``plan + travel matrix + cost policy`` and is never a source of truth. Nothing here is
persisted as authoritative data (see ``docs/STORAGE_SCHEMA.md``).

The central distinction from D13's amendment lives here: a missed **hard** service window is an
explicit :class:`Violation` and puts the solution in ``has_infeasible_windows``. It is never a
large number inside a cost sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum

from core.model.first_stop import FirstStopCandidate, FirstStopResolution
from core.model.ids import StopId
from core.model.service_window import WindowEndPolicy, WindowKind
from core.model.value_objects import DataProvenance, DurationSec, Instant, ensure_utc
from core.validation.errors import InvalidRoutePlanError

__all__ = [
    "BaselineKind",
    "Feasibility",
    "RouteMetrics",
    "RouteSolution",
    "SolutionStatus",
    "StopTimeline",
    "TimelineFlag",
    "Violation",
    "ViolationKind",
]


class Feasibility(str, Enum):
    """Time-window feasibility of a stop (spec section 7)."""

    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"


class TimelineFlag(str, Enum):
    """Non-fatal facts the driver should see."""

    #: Business hours are unknown; no window constraint was applied and none was invented.
    WINDOW_UNKNOWN = "window_unknown"
    #: The plan's default service duration was used because the stop has none.
    SERVICE_DURATION_DEFAULTED = "service_duration_defaulted"


class ViolationKind(str, Enum):
    """Kinds of explicit infeasibility. Extensible; never used as a penalty."""

    TIME_WINDOW_INFEASIBLE = "time_window_infeasible"


class SolutionStatus(str, Enum):
    """Overall outcome of an optimization run."""

    OK = "ok"
    HAS_INFEASIBLE_WINDOWS = "has_infeasible_windows"
    UNRESOLVED_FIRST_STOP = "unresolved_first_stop"


class BaselineKind(str, Enum):
    """What a metrics set describes (D22)."""

    #: The order exactly as the user supplied it - the product's "BEFORE".
    USER_SUPPLIED = "user_supplied"
    #: An algorithmic reference route, for benchmarking only. Never shown as "BEFORE".
    ALGORITHM_GREEDY = "algorithm_greedy"


@dataclass(frozen=True)
class Violation:
    """An explicit infeasibility attached to a stop and to the solution (D13 amendment)."""

    stop_id: StopId
    kind: ViolationKind
    message: str
    service_start: Instant | None = None
    service_window_end: Instant | None = None

    def __post_init__(self) -> None:
        if not self.kind:
            raise InvalidRoutePlanError("a violation needs a kind")
        if not self.message:
            raise InvalidRoutePlanError("a violation needs a human-readable message")
        for field_name in ("service_start", "service_window_end"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value, field_name=field_name))


@dataclass(frozen=True)
class StopTimeline:
    """Timeline of one service stop (spec section 7).

    The meaning of the window end is explicit (D29). ``lateness`` is the miss measured under the
    applied ``window_end_policy``, and ``lateness > 0`` means exactly one thing: the stop cannot
    be served within its permitted window.

    * ``service_finish_before_end`` (default) - service must be over by closing, so
      ``lateness`` equals ``finish_overtime``;
    * ``service_start_before_end`` - beginning service before closing is enough, so ``lateness``
      is the start miss and ``finish_overtime`` is informational only.

    Soft windows do not exist yet, so a missed hard window is always an explicit Violation and
    never a numeric penalty (D13 amendment).
    """

    stop_id: StopId
    departure_from_previous: Instant
    travel_time: DurationSec
    estimated_arrival: Instant
    window_kind: WindowKind
    waiting_time: DurationSec
    service_start: Instant
    service_duration: DurationSec
    estimated_departure: Instant
    lateness: DurationSec
    finish_overtime: DurationSec
    feasibility: Feasibility
    service_window_start: Instant | None = None
    service_window_end: Instant | None = None
    window_end_policy: WindowEndPolicy | None = None
    flags: tuple[TimelineFlag, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in (
            "departure_from_previous",
            "estimated_arrival",
            "service_start",
            "estimated_departure",
        ):
            object.__setattr__(
                self, field_name, ensure_utc(getattr(self, field_name), field_name=field_name)
            )
        for field_name in ("service_window_start", "service_window_end"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value, field_name=field_name))

        if not isinstance(self.window_kind, WindowKind):
            object.__setattr__(self, "window_kind", WindowKind(self.window_kind))
        if not isinstance(self.feasibility, Feasibility):
            object.__setattr__(self, "feasibility", Feasibility(self.feasibility))
        if self.window_end_policy is not None and not isinstance(
            self.window_end_policy, WindowEndPolicy
        ):
            object.__setattr__(
                self, "window_end_policy", WindowEndPolicy(self.window_end_policy)
            )
        object.__setattr__(self, "flags", tuple(self.flags))

        for field_name in ("travel_time", "waiting_time", "lateness", "finish_overtime"):
            if getattr(self, field_name) < 0:
                raise InvalidRoutePlanError(f"{field_name} must be >= 0")
        if self.service_duration <= 0:
            raise InvalidRoutePlanError("service_duration must be > 0")

        if self.window_kind is WindowKind.FIXED:
            if self.service_window_start is None or self.service_window_end is None:
                raise InvalidRoutePlanError(
                    "a fixed window timeline needs both service_window_start and "
                    "service_window_end"
                )
            if self.window_end_policy is None:
                raise InvalidRoutePlanError(
                    "a fixed window timeline must state which window_end_policy was applied, so "
                    "that infeasibility is never an implicit assumption (D29)"
                )
        else:
            if self.service_window_start is not None or self.service_window_end is not None:
                raise InvalidRoutePlanError(
                    f"window_kind={self.window_kind.value!r} must not carry window instants"
                )
            if self.window_end_policy is not None:
                raise InvalidRoutePlanError(
                    f"window_kind={self.window_kind.value!r} has no window end to interpret, so "
                    "it must not carry a window_end_policy"
                )

        if self.estimated_arrival != self.departure_from_previous + timedelta(
            seconds=self.travel_time
        ):
            raise InvalidRoutePlanError(
                "estimated_arrival must equal departure_from_previous + travel_time"
            )
        if self.service_start != self.estimated_arrival + timedelta(seconds=self.waiting_time):
            raise InvalidRoutePlanError(
                "service_start must equal estimated_arrival + waiting_time; a stop can never "
                "begin service before it opens (spec section 10)"
            )
        if self.estimated_departure != self.service_start + timedelta(
            seconds=self.service_duration
        ):
            raise InvalidRoutePlanError(
                "estimated_departure must equal service_start + service_duration"
            )

        if self.window_kind is WindowKind.FIXED:
            assert self.service_window_end is not None
            expected_finish_overtime = max(
                0, int((self.estimated_departure - self.service_window_end).total_seconds())
            )
            if self.finish_overtime != expected_finish_overtime:
                raise InvalidRoutePlanError(
                    "finish_overtime must equal how long service runs past the window end"
                )
            if self.window_end_policy is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
                expected_lateness = self.finish_overtime
            else:
                expected_lateness = self.start_lateness
            if self.lateness != expected_lateness:
                raise InvalidRoutePlanError(
                    f"lateness must be the miss measured under window_end_policy="
                    f"{self.window_end_policy.value!r}: expected {expected_lateness}s, got "
                    f"{self.lateness}s (D29)"
                )
        else:
            if self.finish_overtime != 0 or self.lateness != 0:
                raise InvalidRoutePlanError(
                    "a stop without a window end cannot be late or run overtime"
                )

        if (self.lateness > 0) != (self.feasibility is Feasibility.INFEASIBLE):
            raise InvalidRoutePlanError(
                "lateness > 0 and feasibility='infeasible' must agree: lateness is the miss "
                "measured under the applied window_end_policy, so it means the stop cannot be "
                "served within its permitted window (D29, D13 amendment)"
            )

    @property
    def start_lateness(self) -> DurationSec:
        """How late service would begin relative to the window end (0 without a fixed window)."""
        if self.service_window_end is None:
            return 0
        return max(0, int((self.service_start - self.service_window_end).total_seconds()))

    @property
    def starts_before_end(self) -> bool:
        """Whether service begins inside the window, regardless of the applied end policy."""
        return self.start_lateness == 0

    @property
    def is_infeasible(self) -> bool:
        return self.feasibility is Feasibility.INFEASIBLE


@dataclass(frozen=True)
class RouteMetrics:
    """Distance/duration summary of one route variant (D22).

    ``duration_sec`` is the whole working duration: driving + waiting + service.
    """

    distance_m: float
    duration_sec: DurationSec
    waiting_sec: DurationSec
    feasible: bool
    baseline_kind: BaselineKind | None = None

    def __post_init__(self) -> None:
        if self.distance_m < 0:
            raise InvalidRoutePlanError("distance_m must be >= 0")
        for field_name in ("duration_sec", "waiting_sec"):
            if getattr(self, field_name) < 0:
                raise InvalidRoutePlanError(f"{field_name} must be >= 0")
        if self.baseline_kind is not None and not isinstance(self.baseline_kind, BaselineKind):
            object.__setattr__(self, "baseline_kind", BaselineKind(self.baseline_kind))


@dataclass(frozen=True)
class RouteSolution:
    """Result of an optimization run.

    Stage 0 does not produce solutions (there is no optimizer yet); the type exists so that
    timelines, violations and the before/after baselines have one place to live and are
    validated from the start.
    """

    order: tuple[StopId, ...]
    timelines: tuple[StopTimeline, ...]
    metrics: RouteMetrics
    status: SolutionStatus
    first_service_stop: FirstStopResolution
    provenance: DataProvenance
    inputs_fingerprint: str
    tzdata_version: str | None = None
    user_baseline: RouteMetrics | None = None
    algorithm_baseline: RouteMetrics | None = None
    violations: tuple[Violation, ...] = field(default_factory=tuple)
    top_k: tuple[FirstStopCandidate, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        order = tuple(self.order)
        timelines = tuple(self.timelines)
        violations = tuple(self.violations)
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "timelines", timelines)
        object.__setattr__(self, "violations", violations)
        object.__setattr__(self, "top_k", tuple(self.top_k))

        if not isinstance(self.status, SolutionStatus):
            object.__setattr__(self, "status", SolutionStatus(self.status))
        if not isinstance(self.provenance, DataProvenance):
            object.__setattr__(self, "provenance", DataProvenance(self.provenance))
        if not self.inputs_fingerprint:
            raise InvalidRoutePlanError(
                "a solution needs inputs_fingerprint so AUTO can detect a stale recommendation"
            )

        if len(order) != len(timelines):
            raise InvalidRoutePlanError(
                "order and timelines must have the same length: every ordered stop has exactly "
                "one timeline"
            )
        if tuple(timeline.stop_id for timeline in timelines) != order:
            raise InvalidRoutePlanError("timelines must follow the order of the route")

        infeasible_timelines = [t.stop_id for t in timelines if t.is_infeasible]
        violating_ids = [v.stop_id for v in violations]
        if any(v.kind is ViolationKind.TIME_WINDOW_INFEASIBLE for v in violations):
            if self.status is not SolutionStatus.HAS_INFEASIBLE_WINDOWS:
                raise InvalidRoutePlanError(
                    "a solution carrying time-window violations must have status "
                    "'has_infeasible_windows' (D13 amendment)"
                )
        if self.status is SolutionStatus.HAS_INFEASIBLE_WINDOWS:
            if not violations or not infeasible_timelines:
                raise InvalidRoutePlanError(
                    "status 'has_infeasible_windows' requires at least one explicit violation "
                    "and an infeasible timeline"
                )
            if self.metrics.feasible:
                raise InvalidRoutePlanError(
                    "metrics.feasible must be False when the solution has infeasible windows"
                )
        if self.status is not SolutionStatus.UNRESOLVED_FIRST_STOP and not (
            self.first_service_stop.is_resolved
        ):
            raise InvalidRoutePlanError(
                "a built route needs a resolved first service stop; unresolved first stop is a "
                "separate solution status (D9)"
            )
        if self.status is SolutionStatus.UNRESOLVED_FIRST_STOP and self.first_service_stop.is_resolved:
            raise InvalidRoutePlanError(
                "status 'unresolved_first_stop' contradicts a resolved first service stop"
            )

        unknown_violation_ids = [sid for sid in violating_ids if sid not in order]
        if unknown_violation_ids:
            raise InvalidRoutePlanError(
                f"violations reference stops outside the route: {unknown_violation_ids}"
            )

        if self.user_baseline is not None and (
            self.user_baseline.baseline_kind is not BaselineKind.USER_SUPPLIED
        ):
            raise InvalidRoutePlanError(
                "user_baseline must be the order the user supplied (D22)"
            )
        if self.algorithm_baseline is not None and (
            self.algorithm_baseline.baseline_kind is not BaselineKind.ALGORITHM_GREEDY
        ):
            raise InvalidRoutePlanError(
                "algorithm_baseline is an internal benchmark and must be labelled as such (D22)"
            )

    # ---- queries ------------------------------------------------------- #
    @property
    def has_infeasible_windows(self) -> bool:
        return self.status is SolutionStatus.HAS_INFEASIBLE_WINDOWS

    def infeasible_stop_ids(self) -> tuple[StopId, ...]:
        return tuple(t.stop_id for t in self.timelines if t.is_infeasible)

    @property
    def saved_distance_m(self) -> float | None:
        """Distance saved against the user's own order; ``None`` when no baseline exists."""
        if self.user_baseline is None:
            return None
        return self.user_baseline.distance_m - self.metrics.distance_m

    @property
    def saved_duration_sec(self) -> DurationSec | None:
        """Time saved against the user's own order; ``None`` when no baseline exists."""
        if self.user_baseline is None:
            return None
        return self.user_baseline.duration_sec - self.metrics.duration_sec
