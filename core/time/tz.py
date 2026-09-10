"""Strict local wall-clock resolution (decision D3, spec section 21).

A customer's opening hours are local wall-clock values ("08:00"). Turning them into absolute
instants is where DST bugs are born, so this module refuses to guess:

* a local time inside a **DST gap** does not exist -> :class:`NonexistentLocalTimeError`;
* a local time that **occurs twice** is ambiguous -> :class:`AmbiguousLocalTimeError`, which
  carries both candidate instants so a future explicit-disambiguation API can build on it.

Neither case is silently shifted, and ``fold`` is never chosen implicitly. Detection uses only
``datetime``/``zoneinfo`` semantics (round-trip through UTC, and the two fold offsets) - there
are no hand-written DST tables anywhere in RoutePilot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from core.model.service_window import ServiceWindow
from core.model.value_objects import Instant, ensure_utc
from core.validation.errors import (
    AmbiguousLocalTimeError,
    InvalidServiceWindowError,
    NonexistentLocalTimeError,
)

__all__ = [
    "LocalWindowInstants",
    "local_date_of",
    "resolve_local_datetime",
    "resolve_service_window",
    "to_local",
]


@dataclass(frozen=True)
class LocalWindowInstants:
    """A fixed service window resolved to absolute UTC instants for one service date."""

    open_at: Instant
    close_at: Instant

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_at", ensure_utc(self.open_at, field_name="open_at"))
        object.__setattr__(self, "close_at", ensure_utc(self.close_at, field_name="close_at"))
        if self.open_at >= self.close_at:
            raise InvalidServiceWindowError(
                f"resolved window is empty: {self.open_at.isoformat()} >= "
                f"{self.close_at.isoformat()}"
            )


def resolve_local_datetime(local_date: date, local_time: time, tz: ZoneInfo) -> Instant:
    """Resolve a local wall-clock time on a date to an absolute UTC instant, strictly.

    Raises:
        NonexistentLocalTimeError: the local time falls into a DST gap on that date.
        AmbiguousLocalTimeError: the local time occurs twice on that date.
    """
    if not isinstance(local_date, date):
        raise InvalidServiceWindowError(
            f"service date must be a date, got {type(local_date).__name__}"
        )
    if not isinstance(local_time, time) or local_time.tzinfo is not None:
        raise InvalidServiceWindowError(
            "a service window time must be a local wall-clock time without tzinfo (D2)"
        )

    naive = datetime.combine(local_date, local_time)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)

    # A time inside a DST gap does not survive a round trip through UTC with fold=0.
    round_tripped = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if round_tripped != naive:
        raise NonexistentLocalTimeError(naive, tz.key)

    # Two different offsets for the same wall clock means the time occurs twice.
    if first.utcoffset() != second.utcoffset():
        raise AmbiguousLocalTimeError(
            naive,
            tz.key,
            candidates=(
                first.astimezone(timezone.utc),
                second.astimezone(timezone.utc),
            ),
        )

    return first.astimezone(timezone.utc)


def resolve_service_window(
    window: ServiceWindow,
    service_date: date,
    tz: ZoneInfo,
) -> LocalWindowInstants | None:
    """Resolve a service window for one service date.

    Returns ``None`` for ``unrestricted`` and ``unknown`` windows: there is nothing to resolve
    and nothing to invent.
    """
    if not window.is_fixed:
        return None
    if window.start_local is None or window.end_local is None:  # pragma: no cover - model guard
        raise InvalidServiceWindowError("a fixed window must carry start_local and end_local")
    return LocalWindowInstants(
        open_at=resolve_local_datetime(service_date, window.start_local, tz),
        close_at=resolve_local_datetime(service_date, window.end_local, tz),
    )


def local_date_of(instant: Instant, tz: ZoneInfo) -> date:
    """Local calendar date of an instant in the plan's time zone.

    The service date of a stop is the local date of its estimated arrival: that is the day whose
    opening hours apply to the visit.
    """
    return ensure_utc(instant).astimezone(tz).date()


def to_local(instant: Instant, tz: ZoneInfo) -> datetime:
    """Present an instant as local time (display only)."""
    return ensure_utc(instant).astimezone(tz)
