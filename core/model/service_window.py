"""Service window value object (decisions D28 and D29, spec sections 7 and 17).

A customer's business hours are never invented. The window is therefore an explicit
discriminated value object:

* ``fixed``        - the customer opens and closes at known local wall-clock times;
* ``unrestricted`` - known to be always accessible (no opening constraint);
* ``unknown``      - business hours are not known. Treated as "no window constraint" for
  computation, but flagged so the driver sees missing data instead of a silent assumption.

``unrestricted`` and ``unknown`` are deliberately *different* states even though they
compute identically: one states a fact about access, the other admits missing information.

The meaning of the window **end** is an explicit choice, not a hidden assumption (D29):
``window_end_policy`` says whether service must *begin* or *finish* before closing. A stop may
carry its own policy (``None`` means "inherit the plan default"), which is how a specific
customer or provider can opt into the looser interpretation later without a redesign.

Local times here are wall-clock values **without** a timezone. They are resolved to
absolute instants per service date, under strict DST validation, by :mod:`core.time.tz`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum

from core.validation.errors import InvalidServiceWindowError

__all__ = [
    "DEFAULT_WINDOW_END_POLICY",
    "ServiceWindow",
    "WindowEndPolicy",
    "WindowKind",
]


class WindowKind(str, Enum):
    """Discriminator for :class:`ServiceWindow`."""

    FIXED = "fixed"
    UNRESTRICTED = "unrestricted"
    UNKNOWN = "unknown"


class WindowEndPolicy(str, Enum):
    """What the end of a service window means (D29).

    * ``SERVICE_START_BEFORE_END`` - it is enough that service *begins* before closing. Service
      that runs past closing time is recorded as ``finish_overtime`` but is not infeasible.
    * ``SERVICE_FINISH_BEFORE_END`` - service must *finish* before closing. The conservative
      interpretation and the default for the MVP and the demo.
    """

    SERVICE_START_BEFORE_END = "service_start_before_end"
    SERVICE_FINISH_BEFORE_END = "service_finish_before_end"


#: The conservative default: the customer closes at ``end_local``, so service must be over by then.
DEFAULT_WINDOW_END_POLICY = WindowEndPolicy.SERVICE_FINISH_BEFORE_END


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
    #: ``None`` means "inherit the plan's default policy" (D29).
    window_end_policy: WindowEndPolicy | None = None

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

        if self.window_end_policy is not None and not isinstance(
            self.window_end_policy, WindowEndPolicy
        ):
            try:
                object.__setattr__(
                    self, "window_end_policy", WindowEndPolicy(self.window_end_policy)
                )
            except ValueError:
                raise InvalidServiceWindowError(
                    f"unknown window_end_policy {self.window_end_policy!r}; expected one of "
                    f"{[policy.value for policy in WindowEndPolicy]}"
                ) from None

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
            if self.window_end_policy is not None:
                raise InvalidServiceWindowError(
                    f"window_kind={kind.value!r} has no end to interpret, so it must not carry "
                    "window_end_policy (D29)"
                )

    # ------------------------------------------------------------------ #
    # constructors
    # ------------------------------------------------------------------ #
    @classmethod
    def fixed(
        cls,
        start_local: time,
        end_local: time,
        *,
        window_end_policy: WindowEndPolicy | None = None,
    ) -> "ServiceWindow":
        """A known opening window, e.g. 08:00-18:00 local time.

        ``window_end_policy`` overrides the plan default for this customer only (D29).
        """
        return cls(WindowKind.FIXED, start_local, end_local, window_end_policy)

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

    def effective_end_policy(self, plan_default: WindowEndPolicy) -> WindowEndPolicy:
        """The policy that applies to this window: the stop's own override, else the plan default.

        Only fixed windows have an end to interpret, so the result is only meaningful then.
        """
        if self.window_end_policy is not None:
            return self.window_end_policy
        return plan_default

    def describe_end_policy(self, plan_default: WindowEndPolicy) -> str:
        policy = self.effective_end_policy(plan_default)
        origin = "stop override" if self.window_end_policy is not None else "plan default"
        if policy is WindowEndPolicy.SERVICE_FINISH_BEFORE_END:
            return f"service must finish before closing ({origin})"
        return f"service must start before closing ({origin})"
