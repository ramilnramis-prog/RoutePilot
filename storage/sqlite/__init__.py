"""SQLite adapter for RoutePilot storage (stdlib ``sqlite3`` only - no ORM, D38).

Dependency rule: ``storage/`` depends on ``core/``; ``core/`` never imports ``storage/``
(``tests/test_core_isolation.py`` and ``tools/doctor.py`` enforce it).

Contents:

* :mod:`storage.sqlite.database` - connection helper and the ordered, idempotent migration
  runner;
* ``storage/sqlite/migrations/0001_init.sql`` - the initial DDL implementing sections 2-6 of the
  approved schema ``docs/STORAGE_SCHEMA.md`` (byte-unchanged from approval);
* :mod:`storage.sqlite.route_plan_repository` - ``SqliteRoutePlanRepository``: plan and stop
  persistence with an exact round-trip (U10);
* :mod:`storage.sqlite.optimization_run_repository` - ``SqliteRouteOptimizationRunRepository``:
  append-only immutable run history (U11);
* :mod:`storage.sqlite.app_settings_repository` - ``SqliteAppSettingsRepository``: the
  ``app_settings`` key/value store (U11).

All three repositories implement the pure Protocols declared in :mod:`core.repositories`;
:mod:`demo.storage_roundtrip` exercises them end to end (U12).

Nothing in this package creates a database file: the caller passes a path or ``:memory:``, and
the migration file discovered here is schema only.
"""

from __future__ import annotations

__all__: list[str] = []
