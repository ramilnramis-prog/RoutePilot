"""First service stop: intent, resolution and candidates (decisions D4-D9, D11).

Selection provenance and pinning are separate concepts (D6):

* ``selection_source`` - **how** the stop was selected (``auto_recommendation`` or ``driver``);
* ``pinned`` / ``pinned_via`` - **whether** that selection is currently locked, and by what.

Locking an automatically recommended stop must therefore **not** rewrite its provenance into
``driver``. There is deliberately no invariant such as
``selection_source == 'auto_recommendation' iff pinned == false`` - it is false by design.

Intent (persisted, the user's decision) and resolution (derived, cached, recomputable) are
different types: AUTO is dynamic, so its selection is *data about a computation*, not a user
decision (D11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from core.model.ids import StopId
from core.model.value_objects import DurationSec, Instant
from core.validation.errors import InvalidRoutePlanError

__all__ = [
    "CandidateDiagnostic",
    "FirstStopCandidate",
    "FirstStopIntent",
    "FirstStopMode",
    "FirstStopResolution",
    "FirstStopStatus",
    "PinnedVia",
    "SelectionSource",
    "UNRESOLVED_FIRST_STOP_STATUSES",
]


class FirstStopMode(str, Enum):
    """How the first service stop is chosen (spec section 3).

    Not to be confused with :class:`~core.model.route_mode.RouteMode`, which is a different
    axis (spec section 19).
    """

    AUTO = "auto"
    MANUAL = "manual"


class SelectionSource(str, Enum):
    """How the applied first stop was selected (D6)."""

    AUTO_RECOMMENDATION = "auto_recommendation"
    DRIVER = "driver"


class PinnedVia(str, Enum):
    """What locked the current first stop (D6)."""

    LOCK = "lock"
    OVERRIDE = "override"
    MANUAL_MODE = "manual_mode"


class FirstStopStatus(str, Enum):
    """Resolution state, including the legitimate "no first stop yet" states (D9)."""

    RESOLVED = "resolved"
    UNRESOLVED_EMPTY_PLAN = "unresolved_empty_plan"
    UNRESOLVED_MANUAL_AWAITING_CHOICE = "unresolved_manual_awaiting_choice"
    NO_ACTIVE_STOPS = "no_active_stops"
    NO_FEASIBLE_FIRST_STOP = "no_feasible_first_stop"


#: Statuses that legitimately have ``selected_stop_id is None``. A fake StopId is never
#: substituted for these states (D9).
UNRESOLVED_FIRST_STOP_STATUSES = frozenset(
    {
        FirstStopStatus.UNRESOLVED_EMPTY_PLAN,
        FirstStopStatus.UNRESOLVED_MANUAL_AWAITING_CHOICE,
        FirstStopStatus.NO_ACTIVE_STOPS,
        FirstStopStatus.NO_FEASIBLE_FIRST_STOP,
    }
)


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
    """Persisted user intent about the first service stop (D11).

    * AUTO, not pinned: the optimizer picks and may change it (``pinned_stop_id is None``);
    * AUTO, pinned: the driver locked the recommendation, or overrode it;
    * MANUAL, pinned: the driver chose the stop explicitly.
    """

    mode: FirstStopMode = FirstStopMode.AUTO
    pinned: bool = False
    pinned_stop_id: StopId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _coerce(FirstStopMode, self.mode, "mode"))
        has_id = self.pinned_stop_id is not None
        if self.pinned and not has_id:
            raise InvalidRoutePlanError(
                "a pinned first stop must have pinned_stop_id; pinned means an explicit "
                "user fixation of a concrete stop (D5)"
            )
        if has_id and not self.pinned:
            raise InvalidRoutePlanError(
                "pinned_stop_id is set but pinned is False; an unpinned first stop has no "
                "fixed choice (D5)"
            )

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def auto(cls) -> "FirstStopIntent":
        """AUTO with a dynamic (unpinned) selection."""
        return cls(FirstStopMode.AUTO, False, None)

    @classmethod
    def auto_locked(cls, stop_id: StopId) -> "FirstStopIntent":
        """The driver pressed "Lock" on the current AUTO recommendation (D6)."""
        return cls(FirstStopMode.AUTO, True, stop_id)

    @classmethod
    def auto_overridden(cls, stop_id: StopId) -> "FirstStopIntent":
        """The driver overrode AUTO and picked another first stop."""
        return cls(FirstStopMode.AUTO, True, stop_id)

    @classmethod
    def manual(cls, stop_id: StopId) -> "FirstStopIntent":
        """MANUAL selection: the driver explicitly chose the first stop."""
        return cls(FirstStopMode.MANUAL, True, stop_id)

    @property
    def is_dynamic(self) -> bool:
        """True when AUTO is allowed to re-evaluate the first stop (D4)."""
        return self.mode is FirstStopMode.AUTO and not self.pinned

    def describe(self) -> str:
        if self.pinned:
            return f"{self.mode.value}, pinned to {self.pinned_stop_id}"
        return f"{self.mode.value}, dynamic"


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
    """One ranked first-stop alternative, with the deterministic explanation inputs (spec 9)."""

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
        # A candidate that begins service after the window closes is infeasible, and that is
        # exactly what lateness > 0 means (D13 amendment).
        if self.feasible == (self.lateness > 0):
            raise InvalidRoutePlanError(
                "a candidate with lateness > 0 must be infeasible and vice versa"
            )


@dataclass(frozen=True)
class FirstStopResolution:
    """Derived first-stop result: which stop, why, and whether it is currently locked (D11).

    Factories below encode the canonical combinations of D6 so callers cannot drift from them.
    """

    status: FirstStopStatus
    selected_stop_id: StopId | None = None
    selection_source: SelectionSource | None = None
    pinned_via: PinnedVia | None = None
    resolved_at: Instant | None = None
    inputs_fingerprint: str | None = None
    diagnostics: tuple[CandidateDiagnostic, ...] = field(default_factory=tuple)
    top_k: tuple[FirstStopCandidate, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _coerce(FirstStopStatus, self.status, "status"))
        if self.selection_source is not None:
            object.__setattr__(
                self,
                "selection_source",
                _coerce(SelectionSource, self.selection_source, "selection_source"),
            )
        if self.pinned_via is not None:
            object.__setattr__(
                self, "pinned_via", _coerce(PinnedVia, self.pinned_via, "pinned_via")
            )
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "top_k", tuple(self.top_k))

        if self.resolved_at is not None:
            from core.model.value_objects import ensure_utc

            object.__setattr__(
                self, "resolved_at", ensure_utc(self.resolved_at, field_name="resolved_at")
            )

        if self.status is FirstStopStatus.RESOLVED:
            if self.selected_stop_id is None:
                raise InvalidRoutePlanError("a resolved first stop needs selected_stop_id")
            if self.selection_source is None:
                raise InvalidRoutePlanError("a resolved first stop needs selection_source")
            if self.resolved_at is None:
                raise InvalidRoutePlanError("a resolved first stop needs resolved_at")
            if not self.inputs_fingerprint:
                raise InvalidRoutePlanError(
                    "a resolved first stop needs inputs_fingerprint; without it AUTO cannot "
                    "notice that its recommendation went stale (D4)"
                )
        else:
            if self.selected_stop_id is not None:
                raise InvalidRoutePlanError(
                    f"status={self.status.value!r} must not carry selected_stop_id; "
                    "unresolved states are not masked with a plausible-looking stop (D9)"
                )
            if self.selection_source is not None:
                raise InvalidRoutePlanError(
                    f"status={self.status.value!r} must not carry selection_source"
                )
            if self.pinned_via is not None:
                raise InvalidRoutePlanError(
                    f"status={self.status.value!r} must not carry pinned_via"
                )

        # Provenance/pinning consistency (D6). Note the absence of any rule tying
        # selection_source to pinned: a locked auto recommendation keeps its provenance.
        if self.pinned_via is not None:
            if self.pinned_via is PinnedVia.LOCK:
                if self.selection_source is not SelectionSource.AUTO_RECOMMENDATION:
                    raise InvalidRoutePlanError(
                        "pinned_via='lock' describes locking an auto recommendation, so "
                        "selection_source must stay 'auto_recommendation' (D6)"
                    )
            elif self.selection_source is not SelectionSource.DRIVER:
                raise InvalidRoutePlanError(
                    f"pinned_via={self.pinned_via.value!r} means the driver fixed this stop, "
                    "so selection_source must be 'driver'"
                )

    # ---- canonical states (D6) ----------------------------------------- #
    @classmethod
    def auto_recommendation(
        cls,
        stop_id: StopId,
        *,
        resolved_at: Instant,
        inputs_fingerprint: str,
        top_k: tuple[FirstStopCandidate, ...] = (),
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopResolution":
        """AUTO applied a recommendation; it stays dynamic (``pinned = false``)."""
        return cls(
            status=FirstStopStatus.RESOLVED,
            selected_stop_id=stop_id,
            selection_source=SelectionSource.AUTO_RECOMMENDATION,
            pinned_via=None,
            resolved_at=resolved_at,
            inputs_fingerprint=inputs_fingerprint,
            diagnostics=diagnostics,
            top_k=top_k,
        )

    @classmethod
    def locked_auto_recommendation(
        cls,
        stop_id: StopId,
        *,
        resolved_at: Instant,
        inputs_fingerprint: str,
        top_k: tuple[FirstStopCandidate, ...] = (),
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopResolution":
        """The driver locked an auto recommendation: provenance stays ``auto_recommendation``."""
        return cls(
            status=FirstStopStatus.RESOLVED,
            selected_stop_id=stop_id,
            selection_source=SelectionSource.AUTO_RECOMMENDATION,
            pinned_via=PinnedVia.LOCK,
            resolved_at=resolved_at,
            inputs_fingerprint=inputs_fingerprint,
            diagnostics=diagnostics,
            top_k=top_k,
        )

    @classmethod
    def driver_override(
        cls,
        stop_id: StopId,
        *,
        resolved_at: Instant,
        inputs_fingerprint: str,
        top_k: tuple[FirstStopCandidate, ...] = (),
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopResolution":
        """The driver overrode AUTO and picked another stop."""
        return cls(
            status=FirstStopStatus.RESOLVED,
            selected_stop_id=stop_id,
            selection_source=SelectionSource.DRIVER,
            pinned_via=PinnedVia.OVERRIDE,
            resolved_at=resolved_at,
            inputs_fingerprint=inputs_fingerprint,
            diagnostics=diagnostics,
            top_k=top_k,
        )

    @classmethod
    def manual_selection(
        cls,
        stop_id: StopId,
        *,
        resolved_at: Instant,
        inputs_fingerprint: str,
        top_k: tuple[FirstStopCandidate, ...] = (),
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopResolution":
        """MANUAL mode: the driver chose the first stop."""
        return cls(
            status=FirstStopStatus.RESOLVED,
            selected_stop_id=stop_id,
            selection_source=SelectionSource.DRIVER,
            pinned_via=PinnedVia.MANUAL_MODE,
            resolved_at=resolved_at,
            inputs_fingerprint=inputs_fingerprint,
            diagnostics=diagnostics,
            top_k=top_k,
        )

    @classmethod
    def unresolved(
        cls,
        status: FirstStopStatus,
        *,
        diagnostics: tuple[CandidateDiagnostic, ...] = (),
    ) -> "FirstStopResolution":
        """A legal "no first stop" state (D9). Never carries a placeholder stop."""
        if status is FirstStopStatus.RESOLVED:
            raise InvalidRoutePlanError(
                "use one of the resolved factories for status='resolved'"
            )
        return cls(status=status, diagnostics=diagnostics)

    @property
    def is_resolved(self) -> bool:
        return self.status is FirstStopStatus.RESOLVED

    @property
    def is_pinned(self) -> bool:
        """True when the current selection is locked by the user (D6)."""
        return self.pinned_via is not None

    def explanation(self) -> Mapping[str, float]:
        """Deterministic cost breakdown of the selected candidate (spec section 9)."""
        for candidate in self.top_k:
            if candidate.stop_id == self.selected_stop_id:
                return dict(candidate.explanation)
        return {}
