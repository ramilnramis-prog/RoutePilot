"""Repository ports (Protocols) declared by ``core/`` and implemented under ``storage/``.

The dependency direction of Stage 3 (D14/D38, approved schema ``docs/STORAGE_SCHEMA.md`` section 7)
is one-way and is not negotiable:

    ``storage/``  depends on  ``core/``        (adapters depend on the domain)
    ``core/``     NEVER imports  ``storage/``  (enforced by ``tests/test_core_isolation.py`` and
                                                ``tools/doctor.py``)

This module is therefore **pure by construction**: it imports only ``core`` types plus
``typing``/``collections.abc``. There is no ``sqlite3``, no file I/O and no import of
``storage`` anywhere in it. The concrete implementations live in :mod:`storage.sqlite`
(``SqliteRoutePlanRepository`` in ``storage/sqlite/route_plan_repository.py``); ``core`` neither
imports them nor knows they exist, which is what keeps the domain testable without a database and
keeps SQL out of the product logic.

Only the ports authorized for the current unit are declared here. The run-history and settings
ports of schema section 7 are Stage 3 unit U11 and deliberately do not exist yet.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.model.ids import PlanId
from core.model.route_plan import RoutePlan

__all__ = ["RoutePlanRepository"]


@runtime_checkable
class RoutePlanRepository(Protocol):
    """Persistence port for :class:`~core.model.route_plan.RoutePlan` (schema section 7).

    The port speaks **domain objects only**: no row, no column and no SQL type appears in these
    signatures, so a caller can depend on the port without depending on storage's representation.
    Implementations live under ``storage/``; ``core/`` never imports them.

    Every method must preserve the domain's meaning exactly:

    * a saved-then-loaded plan is equal to the saved plan (``loaded == plan``, domain value
      equality), because storage is a representation of the plan, never a second model of it;
    * START and FINISH are plan locations and are never persisted as service stops (I1/I2);
    * ``input_position`` is immutable input-order provenance and is never rewritten, renumbered or
      compacted by storage (D33, v2 sections 25/30);
    * the driver's first-stop **decision** is persisted; a recommendation is derived and
      recomputable and is never stored as plan state (D4/D11/D32);
    * loading re-validates every value object through the domain, so hand-edited stored data fails
      loudly instead of producing a half-valid plan (D38 acceptance item 5).
    """

    def save(self, plan: RoutePlan) -> None:
        """Insert or update ``plan`` together with its stops, atomically.

        Implementations must write the plan and its stop set in one transaction: a plan whose
        stops were only partly written (or a stop set left behind from an earlier version of the
        plan) is never observable.
        """
        ...

    def get(self, plan_id: PlanId) -> RoutePlan | None:
        """The stored plan, or ``None`` when no plan with that id exists.

        Absence is a normal answer, not an error: the caller asked for a specific plan and there
        is none. Every other failure to reconstruct the plan is an error and must be raised, never
        softened into ``None`` (D26).
        """
        ...

    def list(self) -> tuple[RoutePlan, ...]:
        """Every stored plan, in a defined, deterministic order.

        Documented per implementation (the SQLite one orders by ``created_at_utc``, then ``id``).
        """
        ...

    def delete(self, plan_id: PlanId) -> bool:
        """Remove the plan and everything that belongs to it; return whether a plan was removed.

        ``False`` means there was no such plan (nothing to do). Deleting a plan removes its stored
        stops and its stored optimization runs, so no orphan row survives (schema sections 4-5).
        """
        ...
