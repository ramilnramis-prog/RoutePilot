"""First service stop: the driver's decision and the engine's recommendation (D4-D11, D32).

Two concepts that must never be merged:

* **intent** - the driver's persisted decision: ``mode``, ``selected_stop_id``,
  ``selection_source``, ``pinned``. It is a decision, not a computation.
* **recommendation** - derived, cached and recomputable: ``recommended_stop_id`` and the ranked
  top-K candidates. It **never implies a selection**.

The optimizer recommends; the driver decides (D4). Nothing in this module applies a
recommendation, and nothing automatic can produce a selection or ``pinned = True`` (D5).

This supersedes the revoked AUTO model, in which the system applied a recommendation itself and
provenance was recorded with ``auto_recommendation`` / ``driver`` plus a separate ``pinned_via``.
``pinned_via`` is gone: with no automatic application there is no "Lock" action left for it to
describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Mapping

from core.model.ids import StopId
from core.model.value_objects import DurationSec, Instant, ensure_utc
from core.validation.errors import InvalidRoutePlanError

if TYPE_CHECKING:  # pragma: no cover - import for typing only, avoids an import cycle
    from core.model.solution import ViolationKind

__all__ = [
    "CandidateDiagnostic",
    "CandidateMetrics",
    "FirstStopCandidate",
    "FirstStopIntent",
    "FirstStopMode",
    "FirstStopRecommendation",
    "FirstStopState",
    "RecommendationStatus",
    "SelectionSource",
]


class FirstStopMode(str, Enum):
    """How the first service stop is decided (D4).

    Not to be confused with :class:`~core.model.route_mode.RouteMode`, which is a different
    axis (spec section 19).
    """

    #: RoutePilot ranks candidates and shows the top-K; the driver still chooses.
    RECOMMEND = "recommend"
    #: The driver selects directly, without needing a ranked recommendation.
    MANUAL = "manual"


class SelectionSource(str, Enum):
    """How the driver arrived at the choice (D6).

    Provenance belongs to the *choice*, not to the recommendation: a recommendation carries none.
    """

    ACCEPTED_RECOMMENDATION = "accepted_recommendation"
    MANUAL_CHOICE = "manual_choice"


class FirstStopState(str, Enum):
    """Derived state of the plan's first service stop (D9, D32). Never stored.

    ``awaiting_first_stop_choice`` is a normal state, not an error: in RECOMMEND mode the driver
    is looking at ranked complete-route outcomes and has not chosen yet.
    """

    AWAITING_FIRST_STOP_CHOICE = "awaiting_first_stop_choice"
    FIRST_STOP_SELECTED = "first_stop_selected"
    NO_ACTIVE_STOPS = "no_active_stops"
    EMPTY_PLAN = "empty_plan"


class RecommendationStatus(str, Enum):
    """Outcome of the recommendation computation, including why nothing could be recommended."""

    RECOMMENDED = "recommended"
    #: No candidate produces a completely feasible route (v2 section 14). RoutePilot must not
    #: label an infeasible candidate as a valid recommended route.
    NO_FULLY_FEASIBLE_ROUTE = "no_fully_feasible_route"
    NO_ACTIVE_STOPS = "no_active_stops"
    EMPTY_PLAN = "empty_plan"


def _coerce(enum_type: type[Enum], value: object, field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError:
        raise InvalidRoutePlanError(
            f"{field_name}={value!r} is invalid; expected one of "
            f"{[member.value for member in enum_type]}"
        ) from None


@dataclass(frozen=True)
class FirstStopIntent:
    """The driver's persisted decision about the first service stop (D11, v2 sections 4-5).

    Exactly two valid states:

    * **unresolved** - ``selected_stop_id is None``, ``selection_source is None``,
      ``pinned = False``. This is where the driver starts, in both modes;
    * **selected** - ``selected_stop_id`` set, ``selection_source`` records how the driver chose,
      and ``pinned = True``.

    A selected-but-not-pinned first stop is **invalid**: "a selected first stop is always
    considered committed and pinned" (v2 section 5). There is no unlocked selection state.
    """

    mode: FirstStopMode = FirstStopMode.RECOMMEND
    selected_stop_id: StopId | None = None
    selection_source: SelectionSource | None = None
    pinned: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _coerce(FirstStopMode, self.mode, "mode"))
        if self.selection_source is not None:
            object.__setattr__(
                self,
                "selection_source",
                _coerce(SelectionSource, self.selection_source, "selection_source"),
            )

        has_selection = self.selected_stop_id is not None
        if not has_selection:
            if self.selection_source is not None:
                raise InvalidRoutePlanError(
                    "selection_source must be None while nothing is selected: provenance "
                    "describes a driver's choice, and there is no choice yet (D6)"
                )
            if self.pinned:
                raise InvalidRoutePlanError(
                    "nothing is selected, so nothing can be pinned (D5)"
                )
            return

        if self.selection_source is None:
            raise InvalidRoutePlanError(
                "a selected first stop must record how the driver chose it "
                "(accepted_recommendation or manual_choice) - D6"
            )
        if not self.pinned:
            raise InvalidRoutePlanError(
                "a selected first stop is always pinned: 'selected_stop_id is not None and "
                "pinned is False' is invalid (v2 section 5). Cancel the selection instead of "
                "leaving it unlocked."
            )
        if (
            self.mode is FirstStopMode.MANUAL
            and self.selection_source is not SelectionSource.MANUAL_CHOICE
        ):
            raise InvalidRoutePlanError(
                "in MANUAL mode the driver selects directly, so selection_source must be "
                "'manual_choice' (D7)"
            )

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def recommend(cls) -> "FirstStopIntent":
        """RECOMMEND mode with no choice made yet."""
        return cls(FirstStopMode.RECOMMEND)

    @classmethod
    def manual_mode(cls) -> "FirstStopIntent":
        """MANUAL mode with no choice made yet."""
        return cls(FirstStopMode.MANUAL)

    @classmethod
    def accepted_recommendation(cls, stop_id: StopId) -> "FirstStopIntent":
        """The driver pressed "start from this stop" on the recommended candidate."""
        return cls(
            FirstStopMode.RECOMMEND,
            stop_id,
            SelectionSource.ACCEPTED_RECOMMENDATION,
            True,
        )

    @classmethod
    def manual_choice(
        cls,
        stop_id: StopId,
        *,
        mode: FirstStopMode = FirstStopMode.RECOMMEND,
    ) -> "FirstStopIntent":
        """The driver chose a stop themselves (another alternative, or directly in MANUAL mode)."""
        return cls(mode, stop_id, SelectionSource.MANUAL_CHOICE, True)

    # ---- queries ------------------------------------------------------- #
    @property
    def has_selection(self) -> bool:
        return self.selected_stop_id is not None

    @property
    def state(self) -> FirstStopState:
        """Plan-level state from the intent alone (stop-set states need the plan)."""
        if self.has_selection:
            return FirstStopState.FIRST_STOP_SELECTED
        return FirstStopState.AWAITING_FIRST_STOP_CHOICE

    def cleared(self) -> "FirstStopIntent":
        """The same mode with no choice: back to ``awaiting_first_stop_choice`` (D8).

        The domain never substitutes a stop here.
        """
        return FirstStopIntent(mode=self.mode)

    def describe(self) -> str:
        if not self.has_selection:
            return f"{self.mode.value}: awaiting first stop choice"
        return (
            f"{self.mode.value}: {self.selected_stop_id} "
            f"({self.selection_source.value}, pinned)"  # type: ignore[union-attr]
        )


@dataclass(frozen=True)
class CandidateDiagnostic:
    """Why a candidate stop was rejected (D9: diagnostics, not a bare ``None``).

    A diagnostic carries **two different stops**, and they are the same stop only when the miss is
    at the candidate's own first stop:

    * :attr:`stop_id` - the **violating** stop, whose hard window the complete route cannot meet;
    * :attr:`candidate_stop_id` - the **candidate** whose complete route cannot meet it, i.e. the
      first stop that route starts from. It is the key
      :meth:`core.engine.first_stop.evaluation.FirstStopEvaluationReport.reasons_for` answers for,
      so a caller holding a rejected candidate gets *that candidate's own* reasons.

    ``code`` is the :class:`~core.model.solution.ViolationKind` value, so a rejection reason is
    machine-readable rather than prose only.
    """

    stop_id: StopId
    code: str
    message: str
    #: The candidate whose complete route could not serve :attr:`stop_id`: that candidate's own
    #: first stop id. It differs from :attr:`stop_id` whenever the miss is later in the route, so
    #: lookup by candidate is never the same question as lookup by violating stop. ``None`` only
    #: for a diagnostic that is not tied to one candidate at all.
    candidate_stop_id: StopId | None = None
    reason: str = ""
    violation_kind: "ViolationKind | None" = None

    def __post_init__(self) -> None:
        if not self.code:
            raise InvalidRoutePlanError("a candidate diagnostic needs a non-empty code")
        if not self.message:
            raise InvalidRoutePlanError("a candidate diagnostic needs a human-readable message")


@dataclass(frozen=True)
class CandidateMetrics:
    """The measured components one candidate's complete route is scored on (v2 sections 12, 16).

    Every component is **measured**, never an assumption or an estimate developed from the first
    leg: the values are the complete route's own travel, waiting and metric distance, FINISH leg
    included (v2 section 15). Only components a policy can actually weight are scored
    (:func:`core.engine.cost.score_breakdown`); the rest of the v2 section 12 metrics are reported
    as data on :class:`FirstStopCandidate`.

    Service time is deliberately **not** a component: for one plan every candidate serves exactly
    the same stops, so total service seconds are constant across candidates and cannot separate
    them. It is reported, never scored.
    """

    travel_sec: DurationSec
    waiting_sec: DurationSec
    distance_m: float

    def __post_init__(self) -> None:
        for field_name in ("travel_sec", "waiting_sec"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidRoutePlanError(
                    f"{field_name} must be whole seconds, got {value!r}"
                )
            if value < 0:
                raise InvalidRoutePlanError(f"{field_name} must be >= 0")
        if self.distance_m < 0:
            raise InvalidRoutePlanError("distance_m must be >= 0")


@dataclass(frozen=True)
class FirstStopCandidate:
    """One first-stop alternative with the complete-route metrics of v2 section 12.

    The candidate is the **complete** outcome of ``START -> this stop -> optimized remaining
    enabled stops -> FINISH`` (v2 sections 12/15, D32), so ``estimated_complete_route_duration``
    and ``estimated_finish`` include the final leg to FINISH. Its first-leg metrics of v2 section
    12 are ``travel_time``, ``estimated_arrival``, ``waiting_time``, ``lateness`` and
    ``estimated_service_start``; its complete-route metrics are ``complete_travel_time``,
    ``complete_waiting_time``, ``total_service_time``, ``estimated_complete_route_duration``,
    ``estimated_finish``, ``violating_stop_ids`` and :attr:`metrics`.

    ``feasible`` means the **complete route** contains no hard service-window violation
    (v2 section 14, D32). The first stop's own lateness stays a reported metric
    (``lateness``), but it no longer decides feasibility: a candidate whose later remainder misses
    a hard window is infeasible too.

    The objective that ranked this candidate is :func:`core.engine.first_stop.evaluation.score_of`
    over :attr:`metrics`, and only candidates whose ``feasible`` is ``True`` are ranked. The
    breakdown is carried per candidate so a recommendation can explain itself deterministically
    (v2 sections 13, 16); service time is reported separately and never scored.
    """

    stop_id: StopId
    travel_time: DurationSec
    estimated_arrival: Instant
    waiting_time: DurationSec
    lateness: DurationSec
    estimated_complete_route_duration: DurationSec
    feasible: bool
    service_window_start: Instant | None = None
    score: float | None = None
    explanation: tuple[tuple[str, float], ...] = ()
    # ---- complete-route metrics (v2 section 12) -------------------------------------------- #
    #: Travel seconds of the whole route, FINISH leg included.
    complete_travel_time: DurationSec = 0
    #: Waiting seconds of the whole route.
    complete_waiting_time: DurationSec = 0
    #: Service seconds of the whole route. Constant across the candidates of one plan, because
    #: every candidate serves exactly the same enabled stops; reported, never scored.
    total_service_time: DurationSec = 0
    #: When the driver reaches FINISH, the last service leg included.
    estimated_finish: Instant | None = None
    #: When service actually begins at the first stop: the ETA plus the first-stop waiting, never
    #: before the window opens (v2 sections 10, 12).
    estimated_service_start: Instant | None = None
    #: Ascending ids of the stops whose hard window the complete route misses. Empty exactly when
    #: ``feasible`` is ``True`` (v2 section 14).
    violating_stop_ids: tuple[StopId, ...] = ()
    #: The worst measured lateness inside the complete route (0 on a fully feasible route).
    max_lateness: DurationSec = 0
    #: The measured objective components the candidate was scored on. Deliberately last: every
    #: earlier field of this Stage 1 type keeps its position.
    metrics: CandidateMetrics | None = None

    def __post_init__(self) -> None:
        if self.travel_time < 0:
            raise InvalidRoutePlanError("travel_time must be >= 0")
        if self.waiting_time < 0:
            raise InvalidRoutePlanError("waiting_time must be >= 0")
        if self.lateness < 0:
            raise InvalidRoutePlanError("lateness must be >= 0")
        if self.estimated_complete_route_duration < self.travel_time:
            raise InvalidRoutePlanError(
                "estimated_complete_route_duration cannot be shorter than the first leg"
            )
        for field_name in (
            "complete_travel_time",
            "complete_waiting_time",
            "total_service_time",
            "max_lateness",
        ):
            value = getattr(self, field_name)
            if value < 0:
                raise InvalidRoutePlanError(f"{field_name} must be >= 0")

        object.__setattr__(self, "violating_stop_ids", tuple(self.violating_stop_ids))
        # v2 section 14 / D32: feasibility is a property of the COMPLETE route, never of the first
        # leg alone. The first stop's lateness is still reported above, but it cannot make a route
        # with a later hard-window miss look feasible.
        if self.feasible != (not self.violating_stop_ids):
            raise InvalidRoutePlanError(
                "feasible must be exactly 'the complete route has no violating stop' (v2 section "
                "14); the first stop's lateness is a reported metric, not the feasibility answer"
            )
        # One complete route has exactly one set of measurements: the reported complete-route
        # figures and the scored breakdown are the same route's, and a mismatch is an invalid
        # candidate rather than a candidate with two different travel times.
        if self.metrics is not None and self.complete_travel_time != self.metrics.travel_sec:
            raise InvalidRoutePlanError(
                "complete_travel_time and metrics.travel_sec must describe the same complete route"
            )
        if self.metrics is not None and self.complete_waiting_time != self.metrics.waiting_sec:
            raise InvalidRoutePlanError(
                "complete_waiting_time and metrics.waiting_sec must describe the same complete route"
            )
        for field_name in ("estimated_finish", "estimated_service_start"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, ensure_utc(value, field_name=field_name)
                )


@dataclass(frozen=True)
class FirstStopRecommendation:
    """Derived recommendation: what the engine suggests, never what the driver chose (D32).

    A recommendation for ``S73`` with ``FirstStopIntent.selected_stop_id = None`` is a perfectly
    valid state - it is the normal state before the driver decides.
    """

    status: RecommendationStatus
    recommended_stop_id: StopId | None = None
    ranked: tuple[FirstStopCandidate, ...] = field(default_factory=tuple)
    resolved_at: Instant | None = None
    inputs_fingerprint: str | None = None
    diagnostics: tuple[CandidateDiagnostic, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _coerce(RecommendationStatus, self.status, "status"))
        object.__setattr__(self, "ranked", tuple(self.ranked))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if self.resolved_at is not None:
            object.__setattr__(
                self, "resolved_at", ensure_utc(self.resolved_at, field_name="resolved_at")
            )

        if self.status is RecommendationStatus.RECOMMENDED:
            if self.recommended_stop_id is None:
                raise InvalidRoutePlanError("a 'recommended' outcome needs recommended_stop_id")
            if self.resolved_at is None:
                raise InvalidRoutePlanError("a 'recommended' outcome needs resolved_at")
            if not self.inputs_fingerprint:
                raise InvalidRoutePlanError(
                    "a 'recommended' outcome needs inputs_fingerprint so a stale recommendation "
                    "can be detected (D4)"
                )
            ranked_ids = [candidate.stop_id for candidate in self.ranked]
            if not ranked_ids:
                raise InvalidRoutePlanError(
                    "a 'recommended' outcome must carry the ranked candidates it recommends from"
                )
            if self.recommended_stop_id not in ranked_ids:
                raise InvalidRoutePlanError(
                    "recommended_stop_id must be one of the ranked candidates"
                )
        else:
            if self.recommended_stop_id is not None:
                raise InvalidRoutePlanError(
                    f"status={self.status.value!r} must not carry recommended_stop_id: a "
                    "recommendation that does not exist is not masked with a plausible stop (D9)"
                )
            if self.ranked:
                raise InvalidRoutePlanError(
                    f"status={self.status.value!r} must not carry ranked candidates"
                )

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def recommended(
        cls,
        stop_id: StopId,
        *,
        ranked: tuple[FirstStopCandidate, ...],
        resolved_at: Instant,
        inputs_fingerprint: str,
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopRecommendation":
        return cls(
            RecommendationStatus.RECOMMENDED,
            stop_id,
            ranked,
            resolved_at,
            inputs_fingerprint,
            diagnostics,
        )

    @classmethod
    def none(
        cls,
        status: RecommendationStatus,
        *,
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopRecommendation":
        """No candidate could be recommended (D9: a distinct reason, never a placeholder stop)."""
        if status is RecommendationStatus.RECOMMENDED:
            raise InvalidRoutePlanError(
                "use FirstStopRecommendation.recommended() for status='recommended'"
            )
        return cls(status=status, diagnostics=diagnostics)

    # ---- queries ------------------------------------------------------- #
    @property
    def is_available(self) -> bool:
        return self.status is RecommendationStatus.RECOMMENDED

    def top(self, count: int) -> tuple[FirstStopCandidate, ...]:
        return self.ranked[:count]

    def find(self, stop_id: StopId) -> FirstStopCandidate | None:
        for candidate in self.ranked:
            if candidate.stop_id == stop_id:
                return candidate
        return None

    def rank_of(self, stop_id: StopId) -> int | None:
        for position, candidate in enumerate(self.ranked, start=1):
            if candidate.stop_id == stop_id:
                return position
        return None

    def explanation(self) -> Mapping[str, float]:
        """Deterministic cost breakdown of the recommended candidate (spec section 9)."""
        if self.recommended_stop_id is None:
            return {}
        candidate = self.find(self.recommended_stop_id)
        return dict(candidate.explanation) if candidate is not None else {}
