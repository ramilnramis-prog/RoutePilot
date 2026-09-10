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
from typing import Mapping

from core.model.ids import StopId
from core.model.value_objects import DurationSec, Instant, ensure_utc
from core.validation.errors import InvalidRoutePlanError

__all__ = [
    "CandidateDiagnostic",
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
    """Why a candidate stop was rejected (D9: diagnostics, not a bare ``None``)."""

    stop_id: StopId
    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code:
            raise InvalidRoutePlanError("a candidate diagnostic needs a non-empty code")


@dataclass(frozen=True)
class FirstStopCandidate:
    """One ranked first-stop alternative with its deterministic explanation inputs (spec 9).

    ``feasible`` currently describes the **first leg**. From Stage 2 it must describe the
    **complete** route outcome (D32), because a candidate whose remainder contains an
    impossible hard window must not look feasible.
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
        # A candidate that would be served outside its window is infeasible, and that is exactly
        # what lateness > 0 means (D13 amendment, D29).
        if self.feasible == (self.lateness > 0):
            raise InvalidRoutePlanError(
                "a candidate with lateness > 0 must be infeasible and vice versa"
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
