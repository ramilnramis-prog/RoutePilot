"""Service window value object (decision D28, spec sections 7 and 17).

A customer's business hours are never invented. The window is therefore an explicit
discriminated value object:

* ``fixed``        - the customer opens and closes at known local wall-clock times;
* ``unrestricted`` - known to be always accessible (no opening constraint);
* ``unknown``      - business hours are not known. Treated as "no window constraint" for
  computation, but flagged so the driver sees missing data instead of a silent assumption.

``unrestricted`` and ``unknown`` are deliberately *different* states even though they
compute identically: one states a fact about access, the other admits missing information.

Local times here are wall-clock values **without** a timezone. They are resolved to
absolute instants per service date, under strict DST validation, by :mod:`core.time.tz`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum

from core.validation.errors import InvalidServiceWindowError

__all__ = ["ServiceWindow", "WindowKind"]


class WindowKind(str, Enum):
    """Discriminator for :class:`ServiceWindow`."""

    FIXED = "fixed"
    UNRESTRICTED = "unrestricted"
    UNKNOWN = "unknown"


def _write_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


@dataclass(frozen=True)
class ServiceWindow:
    """Opening hours of a service stop.

    Build instances through :meth:`fixed`, :meth:`unrestricted` or :meth:`unknown` rather
    than through the constructor directly.
    """

    window_kind: WindowKind
    start_local: time | None = None
    end_local: time | None = None

    def __post_init__(self) -> None:
        kind = self.window_kind
        if not isinstance(kind, WindowKind):
            try:
                kind = WindowKind(kind)
            except ValueError:
                raise InvalidServiceWindowError(
                    f"unknown window_kind {self.window_kind!r}; "
                    f"expected one of {[k.value for k in WindowKind]}"
                ) from None
            object.__setattr__(self, "window_kind", kind)

        if kind is WindowKind.FIXED:
            if self.start_local is None or self.end_local is None:
                raise InvalidServiceWindowError(
                    "a fixed service window requires both start_local and end_local"
                )
            for field_name, value in (
                ("start_local", self.start_local),
                ("end_local", self.end_local),
            ):
                if not isinstance(value, time):
                    raise InvalidServiceWindowError(
                        f"{field_name} must be a datetime.time, got {type(value).__name__}"
                    )
                if value.tzinfo is not None:
                    raise InvalidServiceWindowError(
                        f"{field_name} must be a local wall-clock time without tzinfo; "
                        "the plan's IANA time zone resolves it (D2)"
                    )
            if self.start_local >= self.end_local:
                raise InvalidServiceWindowError(
                    f"a fixed window must satisfy start_local < end_local, got "
                    f"{_write_hhmm(self.start_local)} >= {_write_hhmm(self.end_local)}. "
                    "Overnight windows are not supported yet (see DECISIONS.md, open item 1) "
                    "and are rejected instead of being interpreted silently."
                )
        else:
            if self.start_local is not None or self.end_local is not None:
                raise InvalidServiceWindowError(
                    f"window_kind={kind.value!r} must not carry start_local/end_local: "
                    "hours are not known and RoutePilot does not invent them"
                )

    # ------------------------------------------------------------------ #
    # constructors
    # ------------------------------------------------------------------ #
    @classmethod
    def fixed(cls, start_local: time, end_local: time) -> "ServiceWindow":
        """A known opening window, e.g. 08:00-18:00 local time."""
        return cls(WindowKind.FIXED, start_local, end_local)

    @classmethod
    def unrestricted(cls) -> "ServiceWindow":
        """Known to be always accessible."""
        return cls(WindowKind.UNRESTRICTED, None, None)

    @classmethod
    def unknown(cls) -> "ServiceWindow":
        """Business hours unknown - never replaced by guessed hours."""
        return cls(WindowKind.UNKNOWN, None, None)

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    @property
    def is_fixed(self) -> bool:
        return self.window_kind is WindowKind.FIXED

    @property
    def is_known(self) -> bool:
        """True when something definite is known (fixed hours or always accessible)."""
        return self.window_kind in (WindowKind.FIXED, WindowKind.UNRESTRICTED)

    def describe(self) -> str:
        if self.window_kind is WindowKind.FIXED:
            assert self.start_local is not None and self.end_local is not None
            return f"{_write_hhmm(self.start_local)}-{_write_hhmm(self.end_local)}"
        if self.window_kind is WindowKind.UNRESTRICTED:
            return "always accessible"
        return "hours unknown"
