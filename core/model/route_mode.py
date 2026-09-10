"""Route modes (decision D19, spec section 19).

A route mode is a *different axis* from the first-stop mode (``auto`` / ``manual``, spec
section 3): the first-stop mode says who picks the first stop, the route mode says what the
route is optimized for.

Every mode carries an explicit implementation status. Declared-but-unimplemented modes raise
:class:`~core.validation.errors.UnsupportedRouteModeError` instead of silently behaving like
``SMART_ROUTE`` (D16).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from core.validation.errors import UnsupportedRouteModeError

__all__ = [
    "DEFAULT_ROUTE_MODE",
    "ROUTE_MODE_STATUS",
    "RouteMode",
    "is_implemented",
    "mode_status",
    "require_implemented",
]


class RouteMode(str, Enum):
    """Route optimization modes named by spec section 19."""

    FASTEST = "FASTEST"
    SHORTEST = "SHORTEST"
    MINIMUM_TURNS = "MINIMUM_TURNS"
    ON_THE_WAY = "ON_THE_WAY"
    START_TO_FINISH = "START_TO_FINISH"
    SMART_ROUTE = "SMART_ROUTE"


#: Implementation status of every mode. Stage 0 has no optimizer at all, so every mode is
#: "planned" - nothing is presented as working before it exists.
ROUTE_MODE_STATUS: Mapping[RouteMode, str] = {mode: "planned" for mode in RouteMode}

#: The mode the product is heading for; declared, not yet implemented.
DEFAULT_ROUTE_MODE = RouteMode.SMART_ROUTE


def mode_status(mode: RouteMode) -> str:
    return ROUTE_MODE_STATUS[mode]


def is_implemented(mode: RouteMode) -> bool:
    return ROUTE_MODE_STATUS[mode] == "implemented"


def require_implemented(mode: RouteMode) -> None:
    """Raise when a mode is declared in the domain but not implemented yet."""
    if not is_implemented(mode):
        raise UnsupportedRouteModeError(
            f"route mode {mode.value!r} (status={mode_status(mode)})",
            planned_stage="the optimizer stage",
        )
