"""SQLite adapter for RoutePilot storage (stdlib ``sqlite3`` only - no ORM, D38).

Dependency rule: ``storage/`` depends on ``core/``; ``core/`` never imports ``storage/``
(``tests/test_core_isolation.py`` and ``tools/doctor.py`` enforce it).

Contents:

* :mod:`storage.sqlite.database` - connection helper and the ordered, idempotent migration
  runner;
* ``storage/sqlite/migrations/0001_init.sql`` - the initial DDL implementing sections 2-6 of the
  approved schema ``docs/STORAGE_SCHEMA.md``.

Repository implementations (plan/stop persistence, run history, settings) are later units
(U10/U11) and deliberately do not exist yet.

Nothing in this package creates a database file: the caller passes a path or ``:memory:``, and
the migration file discovered here is schema only.
"""

from __future__ import annotations

__all__: list[str] = []
