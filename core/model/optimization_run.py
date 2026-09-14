"""Immutable optimization-run history: one run row and its stored JSON payloads (U11; D38).

The approved storage schema (``docs/STORAGE_SCHEMA.md`` sections 5-7) stores one immutable row per
optimize/reoptimize/preview execution in ``route_optimization_runs``. This module is the **domain
record of that row** plus the JSON codecs for its ``*_json`` columns. It is pure by construction:
only ``core`` types plus ``json``/``dataclasses``/``typing`` are imported, so ``core`` still never
imports storage (D14/D38, ``tests/test_core_isolation.py``).

A run is an **audit record, never plan state**
---------------------------------------------

``plan_id`` points at the plan the run belongs to; nothing here writes to a plan. The
recommendation payload is "what the engine showed at that moment": it never selects, never pins and
never becomes ``plan.first_service_stop`` (D4/D11/D32). A recommendation stored here is history.

Reused vocabulary, never a second metrics model
-----------------------------------------------

The schema's ``metrics_json`` shape (``distance_m``, ``duration_sec``, ``waiting_sec``,
``feasible``) **is** :class:`core.model.solution.RouteMetrics`, so the record stores three real
``RouteMetrics`` value objects and never a parallel metrics class:

* ``user_baseline`` - the driver's own BEFORE order, labelled ``BaselineKind.USER_SUPPLIED``
  (D22/D33);
* ``algorithm_baseline`` - the optimizer's greedy seed, labelled ``BaselineKind.ALGORITHM_GREEDY``,
  always labelled and never presented as the user's BEFORE route (D22);
* ``after`` - the committed route, which carries no baseline label, exactly like
  ``RouteSolution.metrics``.

Each route is written with every field of ``RouteMetrics`` (distance, elapsed duration, waiting,
travel, service, FINISH arrival, feasibility, baseline label), so a historical run stays fully
interpretable and its derived timelines stay recomputable instead of being persisted (D38, schema
open question 2 = RECOMPUTE). ``duration_sec`` keeps its :class:`RouteMetrics` meaning - the whole
elapsed route duration, service time included. ``saved_distance_m`` / ``saved_duration_sec`` are
required stored values and are checked on load to equal ``user_baseline - after``, so the stored
savings cannot drift from the baselines they claim to describe.

What the recommendation and top-K payloads hold
-----------------------------------------------

Both are the run's audit record of what was **shown**, never plan state:

* ``recommendation`` (:class:`OptimizationRunRecommendation`) - the status, the recommended stop id
  (``None`` when nothing could be recommended), the ranked candidate ids in rank order, the
  rejection diagnostics, ``resolved_at`` and ``inputs_fingerprint``;
* ``top_k`` - the ranked candidates themselves, each with every field of
  :class:`~core.model.first_stop.FirstStopCandidate`, because those are the numbers the driver saw
  (v2 section 13). It is stored as SQL ``NULL`` when a run kept no candidate detail, so "nothing
  stored" stays distinguishable from "no candidate existed" (D9).

``order`` is the stored route order: ``START -> service stops -> FINISH`` is implicit, so only the
service stop ids appear (schema section 5: "ordered stop ids"). ``violations`` is the run's explicit
infeasibility list (D13 amendment) - never a penalty folded into a number.

Both payloads are validated by this domain on load and are never trusted (schema section 1, D38
acceptance item 5). Malformed stored bytes raise :class:`InvalidOptimizationRunError`; content that
reaches a real value object raises that object's own error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from core.engine.optimizer.route_fingerprint import ROUTE_FINGERPRINT_VERSION
from core.model.cost_policy import RouteCostPolicy
from core.model.first_stop import (
    CandidateDiagnostic,
    CandidateMetrics,
    FirstStopCandidate,
    FirstStopRecommendation,
    RecommendationStatus,
)
from core.model.ids import PlanId, RunId, StopId
from core.model.solution import (
    BaselineKind,
    RouteMetrics,
    RouteSolution,
    Violation,
    ViolationKind,
)
from core.model.value_objects import DataProvenance
from core.validation.errors import InvalidOptimizationRunError

__all__ = [
    "ALGORITHM_NAME",
    "ALGORITHM_VERSION",
    "RANKED_VIOLATING_STOP_MARKER",
    "RECOMMENDATION_OUTSIDE_ORDER_MARKER",
    "OptimizationRun",
    "OptimizationRunMetrics",
    "OptimizationRunRecommendation",
    "RunKind",
    "RunStatus",
    "decode_order_json",
    "decode_recommendation_json",
    "decode_route_metrics_json",
    "decode_run_metrics_json",
    "decode_top_k_json",
    "decode_violations_json",
    "encode_cost_policy_json",
    "encode_order_json",
    "encode_recommendation_json",
    "encode_route_metrics_json",
    "encode_run_metrics_json",
    "encode_top_k_json",
    "encode_violations_json",
    "run_metrics_from_solution",
]

#: The optimizer pipeline this build runs (D17): a constraint-aware greedy seed followed by
#: deterministic local improvement. Stored per run so a historical row names what produced it.
ALGORITHM_NAME = "greedy_seed+2opt"

#: Version of the stored algorithm *record*, derived from the committed route's fingerprint version
#: so the two can never drift: that fingerprint is what names the route arithmetic.
ALGORITHM_VERSION = str(ROUTE_FINGERPRINT_VERSION)

#: ``YYYY-MM-DDTHH:MM:SSZ`` - UTC ISO-8601 with a trailing ``Z`` (schema section 1).
_UTC_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The exact phrase :meth:`OptimizationRun._check_recommendation_inside_order` uses for the D32
#: rule that couples a stored recommendation to the run's own route. Storage matches on it to report
#: a hand-edited row as a storage-coherence failure without re-writing the rule's wording twice.
RECOMMENDATION_OUTSIDE_ORDER_MARKER = "outside its own order"

#: The exact phrase the same method uses for the v2 section 14 / D32 rule that a run never ranks a
#: stop it also marks as violating; storage matches on it for the same reason as the marker above.
RANKED_VIOLATING_STOP_MARKER = "never a ranked candidate"

#: The per-route keys the stored metrics payload carries: the schema's four public components plus
#: the fields :class:`RouteMetrics` needs to be reconstructible.
_ROUTE_METRIC_KEYS = frozenset(
    {
        "distance_m",
        "duration_sec",
        "waiting_sec",
        "travel_sec",
        "service_sec",
        "finish_arrival",
        "feasible",
        "baseline_kind",
    }
)

_RUN_METRIC_KEYS = frozenset(
    {"user_baseline", "algorithm_baseline", "after", "saved_distance_m", "saved_duration_sec"}
)

_RECOMMENDATION_KEYS = frozenset(
    {
        "status",
        "recommended_stop_id",
        "ranked_stop_ids",
        "resolved_at",
        "inputs_fingerprint",
        "diagnostics",
    }
)

_DIAGNOSTIC_KEYS = frozenset(
    {"stop_id", "code", "message", "candidate_stop_id", "reason", "violation_kind"}
)

_CANDIDATE_KEYS = frozenset(
    {
        "stop_id",
        "travel_time",
        "estimated_arrival",
        "waiting_time",
        "lateness",
        "estimated_complete_route_duration",
        "feasible",
        "service_window_start",
        "score",
        "explanation",
        "complete_travel_time",
        "complete_waiting_time",
        "total_service_time",
        "estimated_finish",
        "estimated_service_start",
        "violating_stop_ids",
        "max_lateness",
        "metrics",
    }
)

_VIOLATION_KEYS = frozenset({"kind", "stop_id", "message", "service_start", "service_window_end"})

_EnumT = TypeVar("_EnumT", bound=Enum)


class RunKind(str, Enum):
    """Which execution produced a row (schema section 5, ``run_kind``)."""

    OPTIMIZE = "optimize"
    REOPTIMIZE = "reoptimize"
    PREVIEW = "preview"


class RunStatus(str, Enum):
    """The stored outcome of one run (schema section 5, ``status``)."""

    OK = "ok"
    HAS_INFEASIBLE_WINDOWS = "has_infeasible_windows"
    #: No committed route: in RECOMMEND mode the driver has not chosen a first stop yet (D4/I4).
    UNRESOLVED_FIRST_STOP = "unresolved_first_stop"


# --------------------------------------------------------------------------- #
# validation helpers: the repository's established taxonomy, no parallel one (D26)
# --------------------------------------------------------------------------- #
def _error(message: str) -> InvalidOptimizationRunError:
    return InvalidOptimizationRunError(message)


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{field} must be a non-empty string, got {value!r}")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _coerce_enum(enum_type: type[_EnumT], value: object, field: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError:
        raise _error(
            f"{field}={value!r} is not one of {[member.value for member in enum_type]}"
        ) from None


def _coerce_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise _error(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise _error(
            f"{field} must be a timezone-aware UTC instant; a naive datetime is a silent "
            f"time-zone bug, not a guess (D2): {value!r}"
        )
    return value.astimezone(timezone.utc)


def _optional_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _coerce_utc(value, field)


def _stop_ids(values: object, field: str) -> tuple[StopId, ...]:
    """A sequence of non-empty stop ids. A bare string is not a sequence of ids."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise _error(f"{field} must be a sequence of stop ids, got {type(values).__name__}")
    return tuple(
        StopId(_require_text(value, f"{field}[{index}]")) for index, value in enumerate(values)
    )


def _require_type(value: object, expected: type, field: str) -> None:
    if not isinstance(value, expected):
        raise _error(f"{field} must be a {expected.__name__}, got {type(value).__name__}")


def _require_metrics(value: object, field: str) -> RouteMetrics:
    _require_type(value, RouteMetrics, field)
    return value  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# the metrics payload
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptimizationRunMetrics:
    """The stored ``metrics_json`` value: the three routes and what the run saved (D22/D38).

    ``user_baseline`` and ``algorithm_baseline`` are the two baselines
    :class:`~core.model.solution.RouteSolution` already distinguishes, stored per run so a
    historical run stays interpretable even after the plan's stops are reordered or disabled
    (schema section 5); ``algorithm_baseline`` is never the user's BEFORE route (D22). ``after`` is
    the committed route of the run and carries no baseline label, exactly like
    ``RouteSolution.metrics``.

    ``saved_distance_m`` and ``saved_duration_sec`` are stored rather than derived because the
    approved payload stores them; they are validated to equal ``user_baseline - after`` so the
    stored saving and the baselines it describes cannot disagree.
    """

    user_baseline: RouteMetrics
    algorithm_baseline: RouteMetrics
    after: RouteMetrics
    saved_distance_m: float
    saved_duration_sec: int

    def __post_init__(self) -> None:
        user = _require_metrics(self.user_baseline, "user_baseline")
        algorithm = _require_metrics(self.algorithm_baseline, "algorithm_baseline")
        after = _require_metrics(self.after, "after")

        if user.baseline_kind is not BaselineKind.USER_SUPPLIED:
            raise _error(
                "user_baseline must be the order the user supplied, labelled "
                f"baseline_kind={BaselineKind.USER_SUPPLIED.value!r} (D22); got "
                f"{user.baseline_kind!r}"
            )
        if algorithm.baseline_kind is not BaselineKind.ALGORITHM_GREEDY:
            raise _error(
                "algorithm_baseline must be labelled "
                f"baseline_kind={BaselineKind.ALGORITHM_GREEDY.value!r}; an algorithmic reference "
                "route is an internal benchmark and is never presented as the user's BEFORE route "
                f"(D22); got {algorithm.baseline_kind!r}"
            )
        if after.baseline_kind is not None:
            raise _error(
                "after must be the committed route and carries no baseline_kind; got "
                f"{after.baseline_kind.value!r}"
            )

        distance = self.saved_distance_m
        if isinstance(distance, bool) or not isinstance(distance, (int, float)):
            raise _error(f"saved_distance_m must be a number, got {type(distance).__name__}")
        if distance != distance or distance in (float("inf"), float("-inf")):
            raise _error(f"saved_distance_m must be finite, got {distance!r}")
        object.__setattr__(self, "saved_distance_m", float(distance))

        seconds = _whole_seconds(self.saved_duration_sec, "saved_duration_sec")
        object.__setattr__(self, "saved_duration_sec", seconds)

        expected_duration = user.duration_sec - after.duration_sec
        if seconds != expected_duration:
            raise _error(
                "saved_duration_sec must be user_baseline.duration_sec - after.duration_sec: "
                f"expected {expected_duration}s, got {seconds}s"
            )
        expected_distance = user.distance_m - after.distance_m
        if abs(float(distance) - expected_distance) > 1e-9:
            raise _error(
                "saved_distance_m must be user_baseline.distance_m - after.distance_m: expected "
                f"{expected_distance!r}m, got {float(distance)!r}m"
            )

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def of(
        cls,
        *,
        user_baseline: RouteMetrics,
        algorithm_baseline: RouteMetrics,
        after: RouteMetrics,
    ) -> "OptimizationRunMetrics":
        """The metrics of one run, with the savings computed from the stored baselines (D22)."""
        _require_metrics(user_baseline, "user_baseline")
        _require_metrics(algorithm_baseline, "algorithm_baseline")
        _require_metrics(after, "after")
        return cls(
            user_baseline=user_baseline,
            algorithm_baseline=algorithm_baseline,
            after=after,
            saved_distance_m=user_baseline.distance_m - after.distance_m,
            saved_duration_sec=user_baseline.duration_sec - after.duration_sec,
        )

    # ---- queries ------------------------------------------------------- #
    def describe(self) -> str:
        return (
            f"user {self.user_baseline.distance_m:.0f}m/{self.user_baseline.duration_sec}s, "
            f"algorithm {self.algorithm_baseline.distance_m:.0f}m/"
            f"{self.algorithm_baseline.duration_sec}s, after {self.after.distance_m:.0f}m/"
            f"{self.after.duration_sec}s, saved {self.saved_distance_m:.0f}m/"
            f"{self.saved_duration_sec}s"
        )


def run_metrics_from_solution(solution: RouteSolution) -> OptimizationRunMetrics:
    """The stored metrics of a committed :class:`~core.model.solution.RouteSolution` (D38).

    The three routes are the solution's own - the committed route and the two baselines it already
    distinguishes (D22) - so a run's stored metrics cannot be a differently-computed copy of them.
    """
    _require_type(solution, RouteSolution, "solution")
    if solution.user_baseline is None or solution.algorithm_baseline is None:
        raise _error(
            "a committed solution must carry both baselines before a run can store them: "
            "user_baseline is the driver's BEFORE route and algorithm_baseline the labelled "
            "internal benchmark (D22)"
        )
    return OptimizationRunMetrics.of(
        user_baseline=solution.user_baseline,
        algorithm_baseline=solution.algorithm_baseline,
        after=solution.metrics,
    )


# --------------------------------------------------------------------------- #
# the recommendation payload
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptimizationRunRecommendation:
    """What the recommendation the run showed contained (schema section 5, D32).

    The **ranked candidate ids** are stored here in rank order and the candidates' own numbers are
    stored in the run's ``top_k`` payload; the two are validated to agree when both are present.
    This records what was shown. It never selects, pins or applies anything: the driver's decision
    lives on the plan and is not part of a run (D4/D11/D32).
    """

    status: RecommendationStatus
    recommended_stop_id: StopId | None = None
    ranked_stop_ids: tuple[StopId, ...] = ()
    resolved_at: datetime | None = None
    inputs_fingerprint: str | None = None
    diagnostics: tuple[CandidateDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        status = _coerce_enum(RecommendationStatus, self.status, "status")
        object.__setattr__(self, "status", status)

        ranked = _stop_ids(self.ranked_stop_ids, "ranked_stop_ids")
        if len(set(ranked)) != len(ranked):
            raise _error(f"ranked_stop_ids contains a duplicate stop id: {list(ranked)!r}")
        object.__setattr__(self, "ranked_stop_ids", ranked)

        diagnostics = tuple(self.diagnostics)
        for index, diagnostic in enumerate(diagnostics):
            _require_type(diagnostic, CandidateDiagnostic, f"diagnostics[{index}]")
        object.__setattr__(self, "diagnostics", diagnostics)

        recommended = self.recommended_stop_id
        if recommended is not None:
            recommended = StopId(_require_text(recommended, "recommended_stop_id"))
        object.__setattr__(self, "recommended_stop_id", recommended)
        object.__setattr__(self, "resolved_at", _optional_utc(self.resolved_at, "resolved_at"))
        object.__setattr__(
            self,
            "inputs_fingerprint",
            _optional_text(self.inputs_fingerprint, "inputs_fingerprint"),
        )

        if status is RecommendationStatus.RECOMMENDED:
            if recommended is None:
                raise _error("status='recommended' needs recommended_stop_id")
            if self.resolved_at is None:
                raise _error("status='recommended' needs resolved_at")
            if not self.inputs_fingerprint:
                raise _error(
                    "status='recommended' needs inputs_fingerprint so a stale recommendation can "
                    "be detected (D4)"
                )
            if not ranked:
                raise _error(
                    "status='recommended' must carry the ranked candidate ids it recommends from"
                )
            if recommended not in ranked:
                raise _error("recommended_stop_id must be one of ranked_stop_ids")
        else:
            if recommended is not None:
                raise _error(
                    f"status={status.value!r} must not carry recommended_stop_id: a recommendation "
                    "that does not exist is not masked with a plausible stop (D9)"
                )
            if ranked:
                raise _error(f"status={status.value!r} must not carry ranked stop ids")

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def of(cls, recommendation: FirstStopRecommendation) -> "OptimizationRunRecommendation":
        """The stored payload of a derived :class:`~core.model.first_stop.FirstStopRecommendation`."""
        _require_type(recommendation, FirstStopRecommendation, "recommendation")
        return cls(
            status=recommendation.status,
            recommended_stop_id=recommendation.recommended_stop_id,
            ranked_stop_ids=tuple(candidate.stop_id for candidate in recommendation.ranked),
            resolved_at=recommendation.resolved_at,
            inputs_fingerprint=recommendation.inputs_fingerprint,
            diagnostics=recommendation.diagnostics,
        )

    @classmethod
    def unavailable(
        cls,
        status: RecommendationStatus,
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "OptimizationRunRecommendation":
        """A run that could not recommend anything (D9: a reason, never a placeholder stop)."""
        return cls(status=status, diagnostics=diagnostics)

    # ---- queries ------------------------------------------------------- #
    @property
    def is_available(self) -> bool:
        return self.status is RecommendationStatus.RECOMMENDED

    def ranked_ids(self) -> tuple[StopId, ...]:
        return self.ranked_stop_ids

    def describe(self) -> str:
        if self.recommended_stop_id is None:
            return f"{self.status.value}: nothing recommended"
        return f"recommended {self.recommended_stop_id} of {len(self.ranked_stop_ids)} ranked"


# --------------------------------------------------------------------------- #
# the run record
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptimizationRun:
    """One immutable ``route_optimization_runs`` row (schema section 5, D38).

    History is append-only: this type exists so a run can be written once and read back faithfully,
    and it deliberately carries no mutation API. ``created_at_utc`` is the run's own timestamp (the
    instant the run happened), not a repository "last written" stamp, because a run row is never
    rewritten.
    """

    id: RunId
    plan_id: PlanId
    run_kind: RunKind
    algorithm: str
    algorithm_version: str
    inputs_fingerprint: str
    route_fingerprint: str
    cost_policy: RouteCostPolicy
    data_provenance: DataProvenance
    status: RunStatus
    order: tuple[StopId, ...]
    recommendation: OptimizationRunRecommendation
    violations: tuple[Violation, ...]
    metrics: OptimizationRunMetrics
    created_at_utc: datetime
    tzdata_version: str | None = None
    top_k: tuple[FirstStopCandidate, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", RunId(_require_text(self.id, "id")))
        object.__setattr__(self, "plan_id", PlanId(_require_text(self.plan_id, "plan_id")))
        object.__setattr__(self, "run_kind", _coerce_enum(RunKind, self.run_kind, "run_kind"))
        object.__setattr__(self, "algorithm", _require_text(self.algorithm, "algorithm"))
        object.__setattr__(
            self,
            "algorithm_version",
            _require_text(self.algorithm_version, "algorithm_version"),
        )
        object.__setattr__(
            self,
            "inputs_fingerprint",
            _require_text(self.inputs_fingerprint, "inputs_fingerprint"),
        )
        object.__setattr__(
            self,
            "route_fingerprint",
            _require_text(self.route_fingerprint, "route_fingerprint"),
        )
        object.__setattr__(
            self, "tzdata_version", _optional_text(self.tzdata_version, "tzdata_version")
        )
        object.__setattr__(
            self,
            "data_provenance",
            _coerce_enum(DataProvenance, self.data_provenance, "data_provenance"),
        )
        object.__setattr__(self, "status", _coerce_enum(RunStatus, self.status, "status"))
        object.__setattr__(self, "order", _stop_ids(self.order, "order"))
        object.__setattr__(
            self, "created_at_utc", _coerce_utc(self.created_at_utc, "created_at_utc")
        )

        if not isinstance(self.cost_policy, RouteCostPolicy):
            raise _error(
                f"cost_policy must be a RouteCostPolicy, got {type(self.cost_policy).__name__}"
            )
        _require_type(self.recommendation, OptimizationRunRecommendation, "recommendation")
        _require_type(self.metrics, OptimizationRunMetrics, "metrics")

        violations = tuple(self.violations)
        for index, violation in enumerate(violations):
            _require_type(violation, Violation, f"violations[{index}]")
        object.__setattr__(self, "violations", violations)

        top_k = self.top_k
        if top_k is not None:
            top_k = tuple(top_k)
            if not top_k:
                raise _error(
                    "top_k must be absent (None) or carry at least one candidate, never an empty "
                    "tuple: 'no candidate detail stored' and 'no candidate existed' are different "
                    "facts (D9)"
                )
            for index, candidate in enumerate(top_k):
                _require_type(candidate, FirstStopCandidate, f"top_k[{index}]")
        object.__setattr__(self, "top_k", top_k)

        self._check_status_agrees_with_order()
        self._check_status_agrees_with_violations()
        self._check_top_k_agrees_with_recommendation()
        self._check_recommendation_inside_order()

    # ---- cross-field rules --------------------------------------------- #
    def _check_status_agrees_with_order(self) -> None:
        """``unresolved_first_stop`` is the status of a run with no committed route (D4/I4)."""
        has_route = bool(self.order)
        if self.status is RunStatus.UNRESOLVED_FIRST_STOP:
            if has_route:
                raise _error(
                    "status='unresolved_first_stop' means the driver had not chosen a first stop, "
                    "so there is no committed route and order must be empty; got "
                    f"{list(self.order)!r} (D4/I4)"
                )
            if self.recommendation.is_available:
                raise _error(
                    "status='unresolved_first_stop' cannot carry a ranked recommendation"
                )
            return
        if not has_route:
            raise _error(
                f"status={self.status.value!r} describes a committed route, so order must not be "
                "empty; a run with no order is 'unresolved_first_stop'"
            )

    def _check_status_agrees_with_violations(self) -> None:
        """An infeasible window is explicit data, never folded into a status-free number (D13)."""
        if self.status is RunStatus.HAS_INFEASIBLE_WINDOWS and not self.violations:
            raise _error(
                "status='has_infeasible_windows' needs at least one explicit violation; a missed "
                "hard window is data, never an implicit assumption (D13 amendment)"
            )
        if self.status is RunStatus.OK and self.violations:
            raise _error(
                f"status='ok' cannot carry {len(self.violations)} violation(s): explicit "
                "infeasibilities and the status must agree (D13 amendment)"
            )

    def _check_top_k_agrees_with_recommendation(self) -> None:
        """The stored candidates are the recommendation's own ranked ones, position by position.

        With no driver selection there is no committed route, so a run has no recommendation and no
        candidate detail; that is checked by ``_check_status_agrees_with_order``.
        """
        if self.top_k is None:
            return
        ids = tuple(candidate.stop_id for candidate in self.top_k)
        if len(set(ids)) != len(ids):
            raise _error(f"top_k contains a duplicate candidate stop id: {list(ids)!r}")
        ranked = self.recommendation.ranked_stop_ids
        if not self.recommendation.is_available:
            raise _error(
                f"top_k carries {len(ids)} candidate(s) but the stored recommendation is "
                f"{self.recommendation.status.value!r}: candidates are the ranked outcomes a "
                "recommendation showed, so there is nothing for them to belong to"
            )
        if tuple(ranked[: len(ids)]) != ids:
            raise _error(
                "top_k must be the head of the recommendation's ranked candidates, in rank order: "
                f"top_k={list(ids)!r} but ranked_stop_ids={list(ranked)!r}"
            )

    def _check_recommendation_inside_order(self) -> None:
        """A recommendation may only name stops the run's own route and violations allow (D32).

        The recommendation, the route order and the violations are three different stored columns of
        the same row, so both rules here are cross-field rules of the record itself: the ranked
        candidate ids describe what the run showed about *this* route, and a ranked id outside
        ``order`` - or one the run's own ``violations`` mark as an infeasible stop - is contradictory
        history that could never be rendered. Both are enforced here, on construction, so an
        incoherent run can neither be built nor appended; the repository re-checks them when it loads
        a row, because a hand-edited row never passed through this constructor.

        The second rule is v2 section 14 / D32: an infeasible complete route is never a ranked
        candidate, so a run that ranks a stop it also marks as violating contradicts itself. It is
        deliberately enforced here rather than only on the read path, so ``append()`` cannot store
        history that ``list_for_plan()`` would then refuse forever.
        """
        unknown = [
            stop_id for stop_id in self.recommendation.ranked_stop_ids if stop_id not in self.order
        ]
        if unknown:
            raise _error(
                f"run {self.id!r} carries a recommendation naming stop(s) "
                f"{RECOMMENDATION_OUTSIDE_ORDER_MARKER} "
                f"{list(self.order)!r}: {[str(stop_id) for stop_id in unknown]!r}. A recommendation "
                "is the audit record of what this run showed about this route (D32), so it cannot "
                "reference a route the run did not commit"
            )

        violating = {violation.stop_id for violation in self.violations}
        contradictory = [
            str(stop_id)
            for stop_id in self.recommendation.ranked_stop_ids
            if stop_id in violating
        ]
        if contradictory:
            raise _error(
                f"run {self.id!r} ranks stop(s) {contradictory!r} that its own stored violations "
                f"mark as infeasible: an infeasible complete route is {RANKED_VIOLATING_STOP_MARKER} "
                "(v2 section 14, D32), so a recommendation cannot name a stop this run's own "
                "violations reject"
            )

    # ---- queries ------------------------------------------------------- #
    @property
    def has_committed_route(self) -> bool:
        return bool(self.order)

    @property
    def is_feasible(self) -> bool:
        return self.status is RunStatus.OK

    @property
    def top_k_stop_ids(self) -> tuple[StopId, ...]:
        return tuple(candidate.stop_id for candidate in self.top_k or ())

    def describe(self) -> str:
        return (
            f"{self.run_kind.value} run {self.id} of plan {self.plan_id} "
            f"({self.algorithm} {self.algorithm_version}, status={self.status.value}, "
            f"{len(self.order)} stops, {len(self.violations)} violations)"
        )


# --------------------------------------------------------------------------- #
# JSON codecs: storage keeps the text, this module owns the shape
# --------------------------------------------------------------------------- #
def encode_cost_policy_json(policy: RouteCostPolicy) -> str:
    """The stored policy payload: name, weights, ``provisional`` and ``notes`` (D13/D16/D31/D35).

    ``declarations`` are deliberately absent: they are the code's static capability registry and are
    rebuilt from the code on load, while the weights themselves are stored truth, because D13/D16/D35
    make weights explicit configuration rather than hidden defaults.
    """
    _require_type(policy, RouteCostPolicy, "cost_policy")
    return json.dumps(
        {
            "name": policy.name,
            "weights": {
                component.value: weight
                for component, weight in sorted(
                    policy.weights.items(), key=lambda item: item[0].value
                )
            },
            "provisional": policy.provisional,
            "notes": policy.notes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _utc_text(value: datetime, field: str) -> str:
    """One instant as the stored ``YYYY-MM-DDTHH:MM:SSZ`` text, refusing sub-second precision.

    The storage convention is whole seconds (schema section 1), so a sub-second instant cannot be
    represented: ``strftime`` would silently drop the fraction and the reloaded payload would no
    longer equal the value that was written. Such an instant is refused instead, naming the field and
    the value, exactly as the plan repository refuses a sub-second ``departure_time``.
    """
    if value.microsecond:
        raise _error(
            f"{field}={value.isoformat()} has sub-second precision, which the storage convention "
            "(UTC ISO-8601 seconds with a trailing Z, schema section 1) cannot represent; storing it "
            "would silently change the stored instant, so it is refused instead"
        )
    return value.astimezone(timezone.utc).strftime(_UTC_Z_FORMAT)


def _parse_utc_text(value: object, field: str) -> datetime:
    """Parse a stored ``...Z`` timestamp raising this domain's error, never a bare ``ValueError``.

    Storage validates the stored shape and the real calendar first (schema section 1: an impossible
    date such as ``2026-02-30T01:00:00Z`` is a storage error); the content reaching here is a real
    instant, and a truncated or unrelated string is refused as a domain error, not a crash.
    """
    if not isinstance(value, str):
        raise _error(f"{field} must be an ISO-8601 UTC timestamp string, got {value!r}")
    try:
        parsed = datetime.strptime(value, _UTC_Z_FORMAT)
    except ValueError:
        raise _error(
            f"{field}={value!r} is not UTC ISO-8601 like 2026-09-11T01:00:00Z (schema section 1)"
        ) from None
    return parsed.replace(tzinfo=timezone.utc)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{field} must be a number, got {value!r}")
    return float(value)


def _whole_seconds(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{field} must be whole seconds, got {value!r}")
    number = float(value)
    if number != int(number):
        raise _error(f"{field} must be whole seconds, got {value!r}")
    return int(number)


def _flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error(f"{field} must be true or false, got {value!r}")
    return value


def _json_object(text: object, field: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise _error(f"{field} must be TEXT holding JSON, got {type(text).__name__}")
    if not text.strip():
        raise _error(f"{field} is empty, so it holds no JSON object")
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise _error(f"{field} is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise _error(f"{field} must hold a JSON object, got {type(payload).__name__}")
    return payload


def _json_array(text: object, field: str) -> list[Any]:
    if not isinstance(text, str):
        raise _error(f"{field} must be TEXT holding a JSON array, got {type(text).__name__}")
    if not text.strip():
        raise _error(f"{field} is empty, so it holds no JSON array")
    try:
        payload = json.loads(text)
    except ValueError as error:
        # ValueError covers json.JSONDecodeError and the "expecting value" cases alike, so a
        # malformed payload can never escape as the interpreter's own exception.
        raise _error(f"{field} is not valid JSON: {error}") from None
    if not isinstance(payload, list):
        raise _error(f"{field} must hold a JSON array, got {type(payload).__name__}")
    return payload


def _require_keys(payload: Mapping[str, Any], field: str, *, allowed: frozenset[str]) -> None:
    found = set(payload)
    missing = sorted(allowed - found)
    unknown = sorted(found - allowed)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise _error(
            f"{field} does not have the stored shape ({', '.join(details)}); the reader refuses to "
            "guess at a payload written by another format"
        )


# ---- route metrics --------------------------------------------------------- #
def _route_metrics_payload(metrics: RouteMetrics) -> dict[str, Any]:
    return {
        "distance_m": float(metrics.distance_m),
        "duration_sec": int(metrics.duration_sec),
        "waiting_sec": int(metrics.waiting_sec),
        "travel_sec": int(metrics.travel_sec),
        "service_sec": int(metrics.service_sec),
        "finish_arrival": _utc_text(metrics.finish_arrival, "route_metrics.finish_arrival"),
        "feasible": bool(metrics.feasible),
        "baseline_kind": metrics.baseline_kind.value if metrics.baseline_kind is not None else None,
    }


def _route_metrics_object(value: object, field: str) -> RouteMetrics:
    if not isinstance(value, dict):
        raise _error(f"{field} must be a JSON object, got {type(value).__name__}")
    _require_keys(value, field, allowed=_ROUTE_METRIC_KEYS)
    baseline_kind = value["baseline_kind"]
    return RouteMetrics(
        distance_m=_number(value["distance_m"], f"{field}.distance_m"),
        duration_sec=_whole_seconds(value["duration_sec"], f"{field}.duration_sec"),
        waiting_sec=_whole_seconds(value["waiting_sec"], f"{field}.waiting_sec"),
        travel_sec=_whole_seconds(value["travel_sec"], f"{field}.travel_sec"),
        service_sec=_whole_seconds(value["service_sec"], f"{field}.service_sec"),
        finish_arrival=_parse_utc_text(value["finish_arrival"], f"{field}.finish_arrival"),
        feasible=_flag(value["feasible"], f"{field}.feasible"),
        baseline_kind=(
            None
            if baseline_kind is None
            else _coerce_enum(BaselineKind, baseline_kind, f"{field}.baseline_kind")
        ),
    )


def encode_route_metrics_json(metrics: RouteMetrics) -> str:
    """One :class:`~core.model.solution.RouteMetrics` as the stored per-route JSON object."""
    _require_metrics(metrics, "metrics")
    return json.dumps(
        _route_metrics_payload(metrics),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_route_metrics_json(text: object) -> RouteMetrics:
    """The reverse of :func:`encode_route_metrics_json`; malformed stored bytes fail loudly.

    The stored text is parsed with the module's JSON-object helper before it is validated, exactly
    like :func:`decode_run_metrics_json`, so text that is not a JSON object is refused with this
    domain's error instead of being handed to a dict-only helper as a string.
    """
    return _route_metrics_object(_json_object(text, "route_metrics"), "route_metrics")


# ---- run metrics ----------------------------------------------------------- #
def encode_run_metrics_json(metrics: OptimizationRunMetrics) -> str:
    """The approved ``metrics_json`` payload (schema section 5): three routes plus the savings."""
    _require_type(metrics, OptimizationRunMetrics, "metrics")
    return json.dumps(
        {
            "user_baseline": _route_metrics_payload(metrics.user_baseline),
            "algorithm_baseline": _route_metrics_payload(metrics.algorithm_baseline),
            "after": _route_metrics_payload(metrics.after),
            "saved_distance_m": float(metrics.saved_distance_m),
            "saved_duration_sec": int(metrics.saved_duration_sec),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_run_metrics_json(text: object) -> OptimizationRunMetrics:
    """Rebuild :class:`OptimizationRunMetrics`, re-validating the baselines and the savings."""
    payload = _json_object(text, "metrics_json")
    _require_keys(payload, "metrics_json", allowed=_RUN_METRIC_KEYS)
    return OptimizationRunMetrics(
        user_baseline=_route_metrics_object(payload["user_baseline"], "metrics_json.user_baseline"),
        algorithm_baseline=_route_metrics_object(
            payload["algorithm_baseline"], "metrics_json.algorithm_baseline"
        ),
        after=_route_metrics_object(payload["after"], "metrics_json.after"),
        saved_distance_m=_number(payload["saved_distance_m"], "metrics_json.saved_distance_m"),
        saved_duration_sec=_whole_seconds(
            payload["saved_duration_sec"], "metrics_json.saved_duration_sec"
        ),
    )


# ---- order and violations -------------------------------------------------- #
def encode_order_json(order: Sequence[StopId]) -> str:
    """The committed route's service stop ids, in route order (schema section 5, ``order_json``)."""
    return json.dumps(
        [str(stop_id) for stop_id in order], ensure_ascii=False, separators=(",", ":")
    )


def decode_order_json(text: object) -> tuple[StopId, ...]:
    """The stored route order; a non-array or a blank id is refused, never repaired."""
    return _stop_ids(_json_array(text, "order_json"), "order_json")


def _diagnostic_payload(diagnostic: CandidateDiagnostic) -> dict[str, Any]:
    return {
        "stop_id": str(diagnostic.stop_id),
        "code": diagnostic.code,
        "message": diagnostic.message,
        "candidate_stop_id": (
            None if diagnostic.candidate_stop_id is None else str(diagnostic.candidate_stop_id)
        ),
        "reason": diagnostic.reason,
        "violation_kind": (
            diagnostic.violation_kind.value if diagnostic.violation_kind is not None else None
        ),
    }


def _diagnostic_object(value: object, field: str) -> CandidateDiagnostic:
    if not isinstance(value, dict):
        raise _error(f"{field} must be a JSON object, got {type(value).__name__}")
    _require_keys(value, field, allowed=_DIAGNOSTIC_KEYS)
    reason = value["reason"]
    if not isinstance(reason, str):
        raise _error(f"{field}.reason must be a string, got {reason!r}")
    candidate_stop_id = value["candidate_stop_id"]
    violation_kind = value["violation_kind"]
    return CandidateDiagnostic(
        stop_id=StopId(_require_text(value["stop_id"], f"{field}.stop_id")),
        code=_require_text(value["code"], f"{field}.code"),
        # The message goes to the domain as stored, so CandidateDiagnostic's own rule reports it.
        message=value["message"],
        candidate_stop_id=(
            None
            if candidate_stop_id is None
            else StopId(_require_text(candidate_stop_id, f"{field}.candidate_stop_id"))
        ),
        reason=reason,
        violation_kind=(
            None
            if violation_kind is None
            else _coerce_enum(ViolationKind, violation_kind, f"{field}.violation_kind")
        ),
    )


def _violation_payload(violation: Violation) -> dict[str, Any]:
    return {
        "kind": violation.kind.value,
        "stop_id": str(violation.stop_id),
        "message": violation.message,
        "service_start": (
            None
            if violation.service_start is None
            else _utc_text(violation.service_start, "violations_json.service_start")
        ),
        "service_window_end": (
            None
            if violation.service_window_end is None
            else _utc_text(violation.service_window_end, "violations_json.service_window_end")
        ),
    }


def encode_violations_json(violations: Sequence[Violation]) -> str:
    """The explicit infeasibilities of the run (D13 amendment), as stored data."""
    return json.dumps(
        [_violation_payload(violation) for violation in violations],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_violations_json(text: object) -> tuple[Violation, ...]:
    """Rebuild each :class:`~core.model.solution.Violation` through the domain's own validation."""
    payload = _json_array(text, "violations_json")
    violations = []
    for index, item in enumerate(payload):
        where = f"violations_json[{index}]"
        if not isinstance(item, dict):
            raise _error(f"{where} must be a JSON object, got {type(item).__name__}")
        _require_keys(item, where, allowed=_VIOLATION_KEYS)
        service_start = item["service_start"]
        service_window_end = item["service_window_end"]
        violations.append(
            Violation(
                stop_id=StopId(_require_text(item["stop_id"], f"{where}.stop_id")),
                kind=_coerce_enum(ViolationKind, item["kind"], f"{where}.kind"),
                # The message goes to the domain as stored: Violation's own rule rejects a missing
                # or empty one, and that is the error the reader must surface, not a second copy.
                message=item["message"],
                service_start=(
                    None
                    if service_start is None
                    else _parse_utc_text(service_start, f"{where}.service_start")
                ),
                service_window_end=(
                    None
                    if service_window_end is None
                    else _parse_utc_text(service_window_end, f"{where}.service_window_end")
                ),
            )
        )
    return tuple(violations)


# ---- recommendation and top-K --------------------------------------------- #
def encode_recommendation_json(recommendation: OptimizationRunRecommendation) -> str:
    """The recommendation the run showed: status, recommended id, ranked ids and diagnostics."""
    _require_type(recommendation, OptimizationRunRecommendation, "recommendation")
    return json.dumps(
        {
            "status": recommendation.status.value,
            "recommended_stop_id": (
                None
                if recommendation.recommended_stop_id is None
                else str(recommendation.recommended_stop_id)
            ),
            "ranked_stop_ids": [str(stop_id) for stop_id in recommendation.ranked_stop_ids],
            "resolved_at": (
                None
                if recommendation.resolved_at is None
                else _utc_text(
                    recommendation.resolved_at, "first_stop_recommendation_json.resolved_at"
                )
            ),
            "inputs_fingerprint": recommendation.inputs_fingerprint,
            "diagnostics": [
                _diagnostic_payload(diagnostic) for diagnostic in recommendation.diagnostics
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_recommendation_json(text: object) -> OptimizationRunRecommendation:
    """Rebuild :class:`OptimizationRunRecommendation`, re-validating every stored rule."""
    field = "first_stop_recommendation_json"
    payload = _json_object(text, field)
    _require_keys(payload, field, allowed=_RECOMMENDATION_KEYS)

    ranked = payload["ranked_stop_ids"]
    if not isinstance(ranked, list):
        raise _error(f"{field}.ranked_stop_ids must be a JSON array, got {type(ranked).__name__}")
    diagnostics = payload["diagnostics"]
    if not isinstance(diagnostics, list):
        raise _error(f"{field}.diagnostics must be a JSON array, got {type(diagnostics).__name__}")
    inputs_fingerprint = payload["inputs_fingerprint"]
    if inputs_fingerprint is not None and not isinstance(inputs_fingerprint, str):
        raise _error(
            f"{field}.inputs_fingerprint must be a string or null, got {inputs_fingerprint!r}"
        )
    recommended = payload["recommended_stop_id"]
    resolved_at = payload["resolved_at"]
    return OptimizationRunRecommendation(
        status=_coerce_enum(RecommendationStatus, payload["status"], f"{field}.status"),
        recommended_stop_id=(
            None
            if recommended is None
            else StopId(_require_text(recommended, f"{field}.recommended_stop_id"))
        ),
        ranked_stop_ids=_stop_ids(ranked, f"{field}.ranked_stop_ids"),
        resolved_at=(
            None
            if resolved_at is None
            else _parse_utc_text(resolved_at, f"{field}.resolved_at")
        ),
        inputs_fingerprint=inputs_fingerprint,
        diagnostics=tuple(
            _diagnostic_object(item, f"{field}.diagnostics[{index}]")
            for index, item in enumerate(diagnostics)
        ),
    )


def _candidate_metrics_payload(metrics: CandidateMetrics) -> dict[str, Any]:
    return {
        "travel_sec": int(metrics.travel_sec),
        "waiting_sec": int(metrics.waiting_sec),
        "distance_m": float(metrics.distance_m),
    }


def _candidate_payload(candidate: FirstStopCandidate) -> dict[str, Any]:
    return {
        "stop_id": str(candidate.stop_id),
        "travel_time": int(candidate.travel_time),
        "estimated_arrival": _utc_text(candidate.estimated_arrival, "top_k_json.estimated_arrival"),
        "waiting_time": int(candidate.waiting_time),
        "lateness": int(candidate.lateness),
        "estimated_complete_route_duration": int(candidate.estimated_complete_route_duration),
        "feasible": bool(candidate.feasible),
        "service_window_start": (
            None
            if candidate.service_window_start is None
            else _utc_text(candidate.service_window_start, "top_k_json.service_window_start")
        ),
        "score": None if candidate.score is None else float(candidate.score),
        "explanation": [[key, float(value)] for key, value in candidate.explanation],
        "complete_travel_time": int(candidate.complete_travel_time),
        "complete_waiting_time": int(candidate.complete_waiting_time),
        "total_service_time": int(candidate.total_service_time),
        "estimated_finish": (
            None
            if candidate.estimated_finish is None
            else _utc_text(candidate.estimated_finish, "top_k_json.estimated_finish")
        ),
        "estimated_service_start": (
            None
            if candidate.estimated_service_start is None
            else _utc_text(candidate.estimated_service_start, "top_k_json.estimated_service_start")
        ),
        "violating_stop_ids": [str(stop_id) for stop_id in candidate.violating_stop_ids],
        "max_lateness": int(candidate.max_lateness),
        "metrics": (
            None
            if candidate.metrics is None
            else _candidate_metrics_payload(candidate.metrics)
        ),
    }


def _candidate_object(value: object, field: str) -> FirstStopCandidate:
    if not isinstance(value, dict):
        raise _error(f"{field} must be a JSON object, got {type(value).__name__}")
    _require_keys(value, field, allowed=_CANDIDATE_KEYS)

    explanation = value["explanation"]
    if not isinstance(explanation, list):
        raise _error(f"{field}.explanation must be a JSON array, got {type(explanation).__name__}")
    pairs: list[tuple[str, float]] = []
    for index, pair in enumerate(explanation):
        if not isinstance(pair, list) or len(pair) != 2:
            raise _error(
                f"{field}.explanation[{index}] must be a [component, value] pair, got {pair!r}"
            )
        pairs.append(
            (
                _require_text(pair[0], f"{field}.explanation[{index}][0]"),
                _number(pair[1], f"{field}.explanation[{index}][1]"),
            )
        )

    score = value["score"]
    if score is not None:
        score = _number(score, f"{field}.score")

    metrics = value["metrics"]
    if metrics is None:
        measured: CandidateMetrics | None = None
    else:
        if not isinstance(metrics, dict):
            raise _error(
                f"{field}.metrics must be a JSON object or null, got {type(metrics).__name__}"
            )
        where = f"{field}.metrics"
        _require_keys(
            metrics, where, allowed=frozenset({"travel_sec", "waiting_sec", "distance_m"})
        )
        measured = CandidateMetrics(
            travel_sec=_whole_seconds(metrics["travel_sec"], f"{where}.travel_sec"),
            waiting_sec=_whole_seconds(metrics["waiting_sec"], f"{where}.waiting_sec"),
            distance_m=_number(metrics["distance_m"], f"{where}.distance_m"),
        )

    def optional_instant(name: str) -> datetime | None:
        raw = value[name]
        return None if raw is None else _parse_utc_text(raw, f"{field}.{name}")

    return FirstStopCandidate(
        stop_id=StopId(_require_text(value["stop_id"], f"{field}.stop_id")),
        travel_time=_whole_seconds(value["travel_time"], f"{field}.travel_time"),
        estimated_arrival=_parse_utc_text(
            value["estimated_arrival"], f"{field}.estimated_arrival"
        ),
        waiting_time=_whole_seconds(value["waiting_time"], f"{field}.waiting_time"),
        lateness=_whole_seconds(value["lateness"], f"{field}.lateness"),
        estimated_complete_route_duration=_whole_seconds(
            value["estimated_complete_route_duration"],
            f"{field}.estimated_complete_route_duration",
        ),
        feasible=_flag(value["feasible"], f"{field}.feasible"),
        service_window_start=optional_instant("service_window_start"),
        score=score,
        explanation=tuple(pairs),
        complete_travel_time=_whole_seconds(
            value["complete_travel_time"], f"{field}.complete_travel_time"
        ),
        complete_waiting_time=_whole_seconds(
            value["complete_waiting_time"], f"{field}.complete_waiting_time"
        ),
        total_service_time=_whole_seconds(
            value["total_service_time"], f"{field}.total_service_time"
        ),
        estimated_finish=optional_instant("estimated_finish"),
        estimated_service_start=optional_instant("estimated_service_start"),
        violating_stop_ids=_stop_ids(value["violating_stop_ids"], f"{field}.violating_stop_ids"),
        max_lateness=_whole_seconds(value["max_lateness"], f"{field}.max_lateness"),
        metrics=measured,
    )


def encode_top_k_json(candidates: Sequence[FirstStopCandidate]) -> str:
    """The ranked candidates the run showed, in rank order (schema section 5, ``top_k_json``)."""
    return json.dumps(
        [_candidate_payload(candidate) for candidate in candidates],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_top_k_json(text: object) -> tuple[FirstStopCandidate, ...]:
    """Rebuild each :class:`~core.model.first_stop.FirstStopCandidate` through its own validation."""
    payload = _json_array(text, "top_k_json")
    return tuple(
        _candidate_object(item, f"top_k_json[{index}]") for index, item in enumerate(payload)
    )
