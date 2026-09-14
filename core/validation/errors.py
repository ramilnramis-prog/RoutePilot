"""RoutePilot error taxonomy (decision D26).

Two different things must never be conflated:

* **Errors** — invalid input or configuration. The request cannot be answered, so the
  domain raises. Errors live in this module.
* **Violations** — valid input whose outcome is infeasible (for example a hard customer
  service window that cannot be met). A violation is *data* carried by a timeline and a
  solution (see ``core.model.solution``), never an exception.

Taxonomy::

    RoutePilotError
    +-- ValidationError
    |   +-- InvalidTimezoneNameError
    |   +-- UnknownTimezoneError
    |   +-- DSTValidationError
    |   |   +-- NonexistentLocalTimeError
    |   |   +-- AmbiguousLocalTimeError
    |   +-- InvalidServiceWindowError
    |   +-- InvalidRouteStopError
    |   +-- InvalidRoutePlanError
    |   +-- InvalidOptimizationRunError
    |   +-- InvalidOrderError
    |   +-- StopNotGeocodedError
    |   +-- MissingServiceDurationError
    |   +-- UnsupportedFeatureError
    |       +-- UnsupportedConstraintError
    |       +-- UnsupportedRouteModeError
    +-- ConfigurationError
        +-- TimezoneDataMissingError

Messages are written to be shown to a driver or a developer as-is, and every environment
failure carries the exact command that fixes it (D12).
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Sequence

__all__ = [
    "TZDATA_INSTALL_COMMAND",
    "RoutePilotError",
    "ConfigurationError",
    "TimezoneDataMissingError",
    "ValidationError",
    "InvalidTimezoneNameError",
    "UnknownTimezoneError",
    "DSTValidationError",
    "NonexistentLocalTimeError",
    "AmbiguousLocalTimeError",
    "InvalidServiceWindowError",
    "InvalidRouteStopError",
    "InvalidRoutePlanError",
    "InvalidOptimizationRunError",
    "InvalidCostPolicyError",
    "InvalidOrderError",
    "StopNotGeocodedError",
    "MissingServiceDurationError",
    "UnsupportedFeatureError",
    "UnsupportedConstraintError",
    "UnsupportedRouteModeError",
]

#: The one command that installs the IANA time zone database. Doctor prints exactly this.
TZDATA_INSTALL_COMMAND = "python -m pip install tzdata"


class RoutePilotError(Exception):
    """Base class of every RoutePilot error."""


# --------------------------------------------------------------------------- #
# configuration / environment
# --------------------------------------------------------------------------- #
class ConfigurationError(RoutePilotError):
    """The environment is not able to run RoutePilot correctly."""


class TimezoneDataMissingError(ConfigurationError):
    """No IANA time zone database is available.

    Raised when a local wall-clock time (or an IANA zone) actually has to be resolved and
    neither the ``tzdata`` package nor a system TZif tree can be found. The message always
    contains the exact fix.
    """

    def __init__(self, detail: str = "", *, fallback_hint: str | None = None) -> None:
        self.detail = detail
        self.fallback_hint = fallback_hint
        self.install_command = TZDATA_INSTALL_COMMAND
        message = ["No IANA time zone database is available."]
        if detail:
            message.append(detail)
        message.append(f"Install it with: {TZDATA_INSTALL_COMMAND}")
        if fallback_hint:
            message.append(fallback_hint)
        super().__init__(" ".join(message))


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #
class ValidationError(RoutePilotError):
    """Input or configuration violates a domain rule."""


class InvalidTimezoneNameError(ValidationError):
    """A string is not a syntactically valid IANA time zone identifier."""

    def __init__(self, value: object, reason: str = "") -> None:
        self.value = value
        self.reason = reason
        suffix = f" ({reason})" if reason else ""
        super().__init__(
            f"{value!r} is not a valid IANA time zone identifier{suffix}. "
            "Expected something like 'Europe/Moscow' or 'UTC'."
        )


class UnknownTimezoneError(ValidationError):
    """The time zone database has no zone with that name."""

    def __init__(self, timezone_name: str, *, available: bool = True) -> None:
        self.timezone_name = timezone_name
        if available:
            message = (
                f"Unknown IANA time zone {timezone_name!r}. "
                "Use a name from the IANA time zone database, for example 'Europe/Moscow'."
            )
        else:
            message = (
                f"Cannot verify IANA time zone {timezone_name!r}: no time zone database "
                f"is available. Install it with: {TZDATA_INSTALL_COMMAND}"
            )
        super().__init__(message)


class DSTValidationError(ValidationError):
    """A local wall-clock time cannot be resolved unambiguously (D3, spec section 21)."""

    def __init__(self, local_value: datetime | time, timezone_name: str, reason: str) -> None:
        self.local_value = local_value
        self.timezone_name = timezone_name
        self.reason = reason
        super().__init__(
            f"{local_value} {reason} in time zone {timezone_name!r}. "
            "RoutePilot never shifts a customer's service window silently: "
            "correct the local time or disambiguate it explicitly."
        )


class NonexistentLocalTimeError(DSTValidationError):
    """The local time falls into a DST gap and does not exist on that date."""

    def __init__(self, local_value: datetime | time, timezone_name: str) -> None:
        super().__init__(local_value, timezone_name, "does not exist (DST gap)")


class AmbiguousLocalTimeError(DSTValidationError):
    """The local time occurs twice on that date and must be disambiguated explicitly.

    ``candidates`` holds both possible absolute instants (UTC), earliest first, so that a
    future explicit-disambiguation API can be built on top of this error unchanged.
    """

    def __init__(
        self,
        local_value: datetime | time,
        timezone_name: str,
        candidates: Sequence[datetime],
    ) -> None:
        self.candidates: tuple[datetime, ...] = tuple(candidates)
        super().__init__(local_value, timezone_name, "occurs twice (ambiguous)")


class InvalidServiceWindowError(ValidationError):
    """A ``ServiceWindow`` violates the shape rules of D28."""


class InvalidRouteStopError(ValidationError):
    """A ``RouteStop`` violates the stop model rules (spec section 17)."""


class InvalidRoutePlanError(ValidationError):
    """A ``RoutePlan`` violates a structural invariant (D10, D21)."""


class InvalidOptimizationRunError(ValidationError):
    """An optimization-run record or one of its stored payloads violates a run rule (U11; D38).

    Raised by :mod:`core.model.optimization_run` for a run that cannot exist: a missing id or
    fingerprint, an unknown ``run_kind``/``status``/``data_provenance``, a metric that is not a
    whole non-negative second, a status that disagrees with the stored order or violations, top-K
    candidates that are not the recommendation's own ranked ones, or a stored JSON payload whose
    shape is not the approved one. Storage raises its own errors for malformed *stored bytes*
    (``StoredRunError``); content that reaches a real value object raises that object's own error.
    """


class InvalidCostPolicyError(ValidationError):
    """A ``RouteCostPolicy`` is incomplete or violates the capability rules (D13, D16)."""


class InvalidOrderError(ValidationError):
    """An ordered list of stops is not a valid permutation of the enabled stops."""


class StopNotGeocodedError(ValidationError):
    """A stop that must be routed has no coordinates.

    Raised instead of guessing a location (spec section 16).
    """

    def __init__(self, stop_id: object, geocode_status: object) -> None:
        self.stop_id = stop_id
        self.geocode_status = geocode_status
        super().__init__(
            f"Stop {stop_id!r} cannot be routed: geocode_status is {geocode_status!r} "
            "and it has no coordinates. Resolve the address first - RoutePilot never "
            "guesses a location."
        )


class MissingServiceDurationError(ValidationError):
    """Neither the stop nor the plan defines how long service takes."""

    def __init__(self, stop_id: object) -> None:
        self.stop_id = stop_id
        super().__init__(
            f"Stop {stop_id!r} has no service_duration and the plan has no "
            "default_service_duration. RoutePilot never invents service time."
        )


class UnsupportedFeatureError(ValidationError):
    """A feature is representable in the domain but not implemented yet (D16).

    Raised instead of silently ignoring a declared-but-unimplemented capability, so an
    unimplemented feature is never presented as working.
    """

    def __init__(self, feature: str, *, planned_stage: str | None = None) -> None:
        self.feature = feature
        self.planned_stage = planned_stage
        stage = f" Planned for {planned_stage}." if planned_stage else ""
        super().__init__(
            f"{feature} is not implemented in this version of RoutePilot.{stage} "
            "It is declared in the domain but is not silently ignored."
        )


class UnsupportedConstraintError(UnsupportedFeatureError):
    """An order constraint kind is representable but not implemented (D21)."""


class UnsupportedRouteModeError(UnsupportedFeatureError):
    """A route mode is declared in the domain but not implemented yet (D19)."""
