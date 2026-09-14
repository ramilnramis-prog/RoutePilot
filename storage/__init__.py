"""RoutePilot storage package (Stage 3, decision D38).

The dependency direction is one-way and is not negotiable:

    storage/  depends on  core/        (the domain model, time layer and engine)
    core/     NEVER imports storage/   (enforced by ``tests/test_core_isolation.py``)

``core/`` holds the domain and declares its ports as Protocols; this package holds the adapters.
Nothing here may leak into the domain: no domain value object is redefined, no product semantics
are decided here, and no SQL detail is visible from ``core/``.

The approved implementation schema is ``docs/STORAGE_SCHEMA.md`` (APPROVED by the owner on
2026-09-11, authorized under D38). The concrete SQLite adapter lives in :mod:`storage.sqlite`;
this module intentionally re-exports only the storage error base class and carries no I/O so that
importing ``storage`` can never open a database.
"""

from __future__ import annotations

from core.validation.errors import RoutePilotError

__all__ = ["StorageError", "StorageMigrationError"]


class StorageError(RoutePilotError):
    """A storage-level failure: the database cannot be opened, read or migrated.

    Storage errors are *errors* in the sense of D26 (invalid configuration or invalid stored
    state), never *violations* (valid input whose outcome is infeasible).
    """


class StorageMigrationError(StorageError):
    """A migration set is inconsistent, or the database records an unknown schema version.

    Raised instead of guessing: a duplicate version, a gap in the sequence, an unparsable
    migration file name, an out-of-sequence new migration or a recorded version this build does
    not know are all refused loudly (D38 acceptance item 4). A modified already-applied migration
    is **not** one of these: ``schema_migrations`` stores only ``(version, applied_at_utc)``, so
    this mechanism records applied versions and never re-applies them, but it cannot detect that a
    released migration file was edited afterwards.
    """
