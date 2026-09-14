"""Repository ports (Protocols) declared by ``core/`` and implemented under ``storage/``.

The dependency direction of Stage 3 (D14/D38, approved schema ``docs/STORAGE_SCHEMA.md`` section 7)
is one-way and is not negotiable:

    ``storage/``  depends on  ``core/``        (adapters depend on the domain)
    ``core/``     NEVER imports  ``storage/``  (enforced by ``tests/test_core_isolation.py`` and
                                                ``tools/doctor.py``)

This module is therefore **pure by construction**: it imports only ``core`` types plus
``typing``/``collections.abc``. There is no ``sqlite3``, no file I/O and no import of
``storage`` anywhere in it. The concrete implementations live in :mod:`storage.sqlite`
(``SqliteRoutePlanRepository`` in ``storage/sqlite/route_plan_repository.py``,
``SqliteRouteOptimizationRunRepository`` in ``storage/sqlite/optimization_run_repository.py`` and
``SqliteAppSettingsRepository`` in ``storage/sqlite/app_settings_repository.py``); ``core`` neither
imports them nor knows they exist, which is what keeps the domain testable without a database and
keeps SQL out of the product logic.

All three ports of the approved schema (section 7) are declared here: the plan, the immutable run
history and the settings.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from core.model.ids import PlanId
from core.model.optimization_run import OptimizationRun
from core.model.route_plan import RoutePlan

__all__ = ["AppSettingsRepository", "RouteOptimizationRunRepository", "RoutePlanRepository"]


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


@runtime_checkable
class RouteOptimizationRunRepository(Protocol):
    """Persistence port for the **immutable** run history (schema sections 5 and 7, D38).

    The port speaks domain objects only. Its whole shape is deliberate:

    * **append-only.** There is no ``update`` and no ``delete``: a run is a historical fact, and the
      port offers no way to rewrite history. Retention is a product decision that does not exist for
      the MVP ("KEEP ALL RUNS", D38);
    * **one row per call.** ``append`` writes exactly one run row in one transaction, so a partially
      written run is never observable;
    * **a run never creates a plan.** Appending a run for a plan that does not exist fails loudly
      instead of inventing the plan it belongs to (schema section 5: ``plan_id`` is a foreign key);
    * **the payloads are validated on load, never trusted.** A recommendation read back here is
      history - what the engine showed at that moment - and never plan state (D4/D11/D32).
    """

    def append(self, run: OptimizationRun) -> None:
        """Append ``run`` as exactly one new historical row.

        Implementations must never update an existing row: two runs with the same id are an error,
        not an overwrite.
        """
        ...

    def list_for_plan(self, plan_id: PlanId) -> tuple[OptimizationRun, ...]:
        """Every stored run of one plan, oldest first, in a defined deterministic order.

        Documented per implementation (the SQLite one orders by ``created_at_utc``, then insertion
        order, so runs sharing a second still come back in the order they were appended).
        """
        ...

    def latest(self, plan_id: PlanId) -> OptimizationRun | None:
        """The newest stored run of one plan, or ``None`` when the plan has no runs.

        Absence is a normal answer, not an error: the caller asked for the latest run of a plan and
        there is none. Every other failure to reconstruct a run is raised (D26).
        """
        ...


@runtime_checkable
class AppSettingsRepository(Protocol):
    """Persistence port for the application settings (schema sections 6 and 7, D38).

    A settings store, not a policy engine: this port reads and writes whole JSON values by key and
    owns **no setting's semantics**. The intended keys of schema section 6 (``default_timezone``,
    ``doctor_mode``, ``default_data_provenance``, ``tile_url``, ``tile_attribution``,
    ``tile_max_zoom``) are documented in the implementation; which keys exist, what their values
    mean and what a missing one should fall back to is decided by the code that owns each setting,
    never here. Tile configuration lives in settings precisely so a map vendor stays
    configuration-isolated (D15) and never leaks into ``core/``.
    """

    def get(self, key: str) -> Any:
        """The stored value of ``key``, or ``None`` when no such key is stored.

        ``None`` means "this key is absent" and is documented, so a caller can tell an unset setting
        from a value it stored itself.
        """
        ...

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``, replacing any previous value (upsert).

        Implementations manage their own ``updated_at_utc``. A key that is empty or blank, or a
        value that cannot be represented as JSON at all, is refused loudly instead of being stored
        in a lossy way.
        """
        ...
