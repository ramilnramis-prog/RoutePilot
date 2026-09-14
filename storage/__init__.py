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

__all__ = [
    "StorageError",
    "StorageMigrationError",
    "StoredPlanError",
    "StoredRunError",
    "StoredSettingsError",
]


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


class StoredPlanError(StorageError):
    """A stored plan row is not a faithful serialisation of a domain plan (Stage 3 U10; D38).

    Raised by :class:`storage.sqlite.route_plan_repository.SqliteRoutePlanRepository` while loading
    when the *stored bytes* are unusable: malformed JSON in a ``*_json`` column, a missing or
    unknown JSON key, an unknown order-override envelope version, a wall-clock column holding a
    resolved instant, an ``enabled`` flag that is not 0/1, a timestamp that is not UTC ISO-8601
    with ``Z``, a missing NOT NULL value, or a ``data_provenance`` this repository cannot accept.

    This is deliberately **not** a substitute for domain validation. Content problems are raised by
    the domain's own errors, because the load path constructs real domain objects: an unknown IANA
    zone raises :class:`~core.validation.errors.UnknownTimezoneError`, an unknown cost-policy name
    raises :class:`~core.validation.errors.InvalidCostPolicyError`, a rejected ``geocode_status`` /
    ``service_status`` or a non-integer ``service_duration_sec`` raises
    :class:`~core.validation.errors.InvalidRouteStopError`, and an inconsistent first-stop decision
    raises :class:`~core.validation.errors.InvalidRoutePlanError`. Nothing is ever repaired
    silently and no default is ever substituted (D38 acceptance item 5).
    """


class StoredRunError(StorageError):
    """A stored optimization-run row is not a faithful serialisation of a domain run (U11; D38).

    Raised by :class:`storage.sqlite.optimization_run_repository.SqliteRouteOptimizationRunRepository`
    while loading when the *stored bytes* of ``route_optimization_runs`` are unusable: a timestamp
    that is not UTC ISO-8601 with ``Z`` or that names no real calendar instant, a missing NOT NULL
    value, a stored cost-policy payload whose name is not a non-empty string, or a ``*_json`` column
    that is not TEXT.

    Payload *shape* problems raise
    :class:`~core.validation.errors.InvalidOptimizationRunError`, because the domain owns the stored
    shape and the run rules, and content that reaches a real value object (``RouteMetrics``,
    ``Violation``, ``FirstStopCandidate``, ``RouteCostPolicy``) raises that object's own error - a
    stored cost-policy name this build does not implement raises
    :class:`~core.validation.errors.InvalidCostPolicyError`. This is the same division of labour as
    :class:`StoredPlanError` (D26/D38).
    """


class StoredSettingsError(StorageError):
    """A stored ``app_settings`` row is unusable, or a setting cannot be stored (U11; D38).

    Raised when the key is empty or blank, when a value cannot be serialised as JSON at all, or when
    a stored ``value_json`` is not valid JSON. The settings repository owns no setting's *meaning*:
    it stores and returns JSON values, and the intended keys of schema section 6 are documented, not
    policed.
    """
