"""SQLite connection helper and the ordered, idempotent migration runner (Stage 3 U9; D38).

stdlib ``sqlite3`` only - no ORM and no third-party dependency. The DDL itself lives in
``storage/sqlite/migrations/NNNN_name.sql`` and implements the APPROVED schema
``docs/STORAGE_SCHEMA.md`` sections 2-6.

Conventions enforced here:

* **Foreign keys**: ``PRAGMA foreign_keys = ON`` is issued on *every* connection. SQLite defaults
  it to OFF per connection, so a repository that forgot it would silently accept orphan rows;
* **UTC ISO-8601 with ``Z``**: ``applied_at_utc`` is written by :func:`utc_now_iso` as
  ``2026-09-11T01:00:00Z``;
* **Ordering is pure**: :func:`plan_migrations` takes file *names* and returns the ordered plan,
  so ordering, gaps and duplicates are testable without the filesystem;
* **Idempotent and non-destructive**: only versions greater than the current
  ``schema_migrations.MAX(version)`` are applied, a second call applies nothing, and a recorded
  version is never re-applied. ``schema_migrations`` records only ``(version, applied_at_utc)``, so
  an already-applied migration whose file was modified afterwards is **not** detected by this
  mechanism - a released migration is never edited by convention, not by an enforced check;
* **Atomic per migration**: see :meth:`MigrationRunner.apply`.

Nothing in this module creates a database file of its own; the caller passes ``":memory:"`` or an
explicit path, and no default path points inside the repository.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from storage import StorageError, StorageMigrationError

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationRunner",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_TABLE",
    "applied_versions",
    "connect",
    "current_version",
    "discover_migrations",
    "migrations_dir",
    "migrate",
    "plan_migrations",
    "utc_now_iso",
]

#: The migration directory shipped with the package.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: Highest schema version this package knows how to apply.
SCHEMA_VERSION = 1

#: The bookkeeping table created by ``0001_init.sql``.
SCHEMA_VERSION_TABLE = "schema_migrations"

#: ``NNNN_name.sql`` - a 4-digit zero-padded version, a non-empty name, a ``.sql`` suffix.
_MIGRATION_FILE_RE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\.sql$")


@dataclass(frozen=True)
class Migration:
    """One ordered migration file."""

    version: int
    name: str
    filename: str

    def describe(self) -> str:
        return f"{self.filename} (version {self.version}, {self.name!r})"


def utc_now_iso(moment: datetime | None = None) -> str:
    """UTC ISO-8601 text with a trailing ``Z`` (the storage timestamp convention, schema section 1).

    ``moment`` is injectable so callers and tests stay deterministic; a naive ``datetime`` is
    rejected instead of being assumed to be UTC.
    """
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.tzinfo is None:
        raise ValueError("utc_now_iso requires a timezone-aware datetime (storage stores UTC only)")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrations_dir(directory: Path | str | None = None) -> Path:
    """The migration directory: the packaged one unless an explicit directory is given."""
    return Path(directory) if directory is not None else MIGRATIONS_DIR


def plan_migrations(filenames: Iterable[str]) -> tuple[Migration, ...]:
    """Pure ordering/validation helper: file names in, an ascending migration plan out.

    This is deliberately filesystem-free so ordering, gaps and duplicates are testable without
    touching disk. It refuses, loudly and instead of guessing:

    * a name that is not ``NNNN_name.sql``;
    * a duplicate version (even under two different names);
    * a gap in the sequence;
    * a first migration that is not version 1 (the schema starts from an empty database).

    Every violation raises :class:`storage.StorageMigrationError` with all offending entries named.
    """
    migrations: list[Migration] = []
    malformed: list[str] = []
    for filename in filenames:
        match = _MIGRATION_FILE_RE.match(str(filename))
        if match is None:
            malformed.append(str(filename))
            continue
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                filename=str(filename),
            )
        )
    if malformed:
        raise StorageMigrationError(
            "migration file name(s) must look like NNNN_name.sql: " + ", ".join(sorted(malformed))
        )

    by_version: dict[int, list[Migration]] = {}
    for migration in migrations:
        by_version.setdefault(migration.version, []).append(migration)
    duplicates = sorted(
        (
            version,
            tuple(migration.filename for migration in by_version[version]),
        )
        for version in by_version
        if len(by_version[version]) > 1
    )
    if duplicates:
        described = "; ".join(
            f"version {version} declared by {', '.join(named)}" for version, named in duplicates
        )
        raise StorageMigrationError(
            "duplicate migration version(s) - each version may exist once: " + described
        )

    ordered = tuple(by_version[version][0] for version in sorted(by_version))
    if ordered and ordered[0].version != 1:
        raise StorageMigrationError(
            f"the migration sequence must start at version 1, not {ordered[0].version:04d} "
            f"({ordered[0].filename}); the schema starts from an empty database"
        )
    expected = list(range(1, len(ordered) + 1))
    actual = [migration.version for migration in ordered]
    if actual != expected:
        gaps = [version for version in expected if version not in by_version]
        raise StorageMigrationError(
            "gap in the migration sequence: version(s) "
            + ", ".join(f"{version:04d}" for version in gaps)
            + " missing between 0001 and "
            + f"{actual[-1]:04d}"
        )
    return ordered


def discover_migrations(directory: Path | str | None = None) -> tuple[Migration, ...]:
    """Read ``NNNN_name.sql`` files from ``directory`` and return the validated ordered plan."""
    root = migrations_dir(directory)
    if not root.is_dir():
        raise StorageMigrationError(
            f"migration directory not found: {root}. The packaged migrations ship with "
            "storage/sqlite/migrations."
        )
    filenames = sorted(path.name for path in root.glob("*.sql") if path.is_file())
    if not filenames:
        raise StorageMigrationError(f"no .sql migrations found in {root}")
    return plan_migrations(filenames)


def connect(database: Path | str = ":memory:") -> sqlite3.Connection:
    """Open a connection with RoutePilot's required pragmas.

    ``database`` is ``":memory:"`` (the default) or an explicit file path; this module never
    invents a path. ``PRAGMA foreign_keys = ON`` is issued here, so every connection enforces the
    declared foreign keys, and the setting is verified rather than assumed.
    """
    identifier = str(database)
    connection = sqlite3.connect(identifier)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if not enabled:
        connection.close()
        raise StorageError(
            "could not enable PRAGMA foreign_keys on this connection; RoutePilot refuses to run "
            "without foreign key enforcement"
        )
    return connection


def _migration_table_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone()
    return row is not None


def applied_versions(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Recorded applied versions, ascending; ``()`` on a database that has not been migrated."""
    if not _migration_table_exists(connection):
        return ()
    cursor = connection.execute(
        f"SELECT version FROM {SCHEMA_VERSION_TABLE} ORDER BY version ASC"  # noqa: S608 - fixed name
    )
    return tuple(int(row[0]) for row in cursor.fetchall())


def current_version(connection: sqlite3.Connection) -> int:
    """Current schema version: ``0`` for an unmigrated database."""
    versions = applied_versions(connection)
    return versions[-1] if versions else 0


class MigrationRunner:
    """Applies ordered migrations to one database, or verifies that it is already current.

    Read-only reporting (``applied``, ``pending``, ``current_version``) works without an
    explicit directory; applying requires one.
    """

    def __init__(
        self,
        migrations_directory: Path | str | None = None,
        *,
        migrations: Sequence[Migration] | None = None,
    ) -> None:
        if migrations is None and migrations_directory is None:
            self.migrations = discover_migrations()
        else:
            self.migrations = tuple(
                migrations if migrations is not None else discover_migrations(migrations_directory)
            )
        self.directory = Path(migrations_directory) if migrations_directory is not None else None

    @classmethod
    def from_filenames(cls, filenames: Iterable[str]) -> "MigrationRunner":
        """A runner over an explicit, already-validated plan (no filesystem access at all)."""
        return cls(migrations=plan_migrations(filenames))

    @property
    def known_versions(self) -> tuple[int, ...]:
        return tuple(migration.version for migration in self.migrations)

    @property
    def target_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    def applied(self, connection: sqlite3.Connection) -> tuple[int, ...]:
        return applied_versions(connection)

    def current_version(self, connection: sqlite3.Connection) -> int:
        return current_version(connection)

    def pending(self, connection: sqlite3.Connection) -> tuple[Migration, ...]:
        """The migrations that :meth:`apply` would apply, in ascending version order."""
        applied = set(self.applied(connection))
        return tuple(
            migration for migration in self.migrations if migration.version not in applied
        )

    def apply(self, connection: sqlite3.Connection) -> tuple[int, ...]:
        """Apply every pending migration; returns the versions actually applied (ascending).

        Idempotent: on a current database the result is ``()`` and no row is duplicated.

        Atomicity: ``sqlite3.executescript`` would commit any open transaction before running,
        which would split the DDL from its version row. Each migration is therefore executed as
        one script that opens and closes its **own** transaction (``BEGIN IMMEDIATE`` ... the DDL
        ... the ``schema_migrations`` insert ... ``COMMIT``), so a failure mid-migration leaves
        neither partial schema nor a recorded version; if any statement fails, the transaction is
        rolled back before the error propagates (see :meth:`_execute_script`). The two interpolated
        values are generated here: a validated integer and a fixed-format UTC ISO-8601 timestamp
        (no caller data is interpolated).
        """
        applied = self.applied(connection)
        unknown = sorted(set(applied) - set(self.known_versions))
        if unknown:
            raise StorageMigrationError(
                "database records migration version(s) this build does not know: "
                + ", ".join(f"{version:04d}" for version in unknown)
                + f" (known: {', '.join(f'{v:04d}' for v in self.known_versions) or 'none'}). "
                "Refusing to guess: a newer schema must not be downgraded or re-used."
            )
        if applied and applied != tuple(range(1, len(applied) + 1)):
            raise StorageMigrationError(
                "out-of-sequence migration state: the database records version(s) "
                + ", ".join(f"{version:04d}" for version in applied)
                + "; applied migrations must form the contiguous sequence 0001..N"
            )

        executed: list[int] = []
        for migration in self.pending(connection):
            self._apply_one(connection, migration)
            executed.append(migration.version)
        return tuple(executed)

    def _apply_one(self, connection: sqlite3.Connection, migration: Migration) -> None:
        script_path = self._path_for(migration)
        if not script_path.is_file():
            raise StorageMigrationError(
                f"migration file for version {migration.version:04d} is missing: {script_path}"
            )
        sql = script_path.read_text(encoding="utf-8")
        applied_at = utc_now_iso()
        self._execute_script(
            connection,
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at_utc) "
            f"VALUES ({int(migration.version)}, '{applied_at}');\n"
            "COMMIT;\n",
        )

    def _execute_script(self, connection: sqlite3.Connection, script: str) -> None:
        """Run one multi-statement migration script atomically, or leave nothing behind.

        The script opens and closes its own transaction (``BEGIN IMMEDIATE`` ... DDL ...
        ``schema_migrations`` insert ... ``COMMIT``). ``executescript`` runs statement by
        statement, so a failure part-way would leave that transaction open holding the DDL already
        created - a later caller ``commit()`` would then persist a partial schema. This wrapper
        therefore rolls the transaction back on any :class:`sqlite3.Error` before re-raising the
        original error (:meth:`_apply_one` composes the script, and the same entrance lets a test
        drive a deliberately failing script with no file or scratch directory).
        """
        try:
            connection.executescript(script)
        except sqlite3.Error as error:
            connection.rollback()
            raise error from error

    def _path_for(self, migration: Migration) -> Path:
        if self.directory is not None:
            return self.directory / migration.filename
        return MIGRATIONS_DIR / migration.filename


def migrate(
    connection: sqlite3.Connection,
    migrations_directory: Path | str | None = None,
    *,
    migrations: Sequence[Migration] | None = None,
) -> int:
    """Bring ``connection`` to the current schema version; returns that version.

    Idempotent: a second call applies nothing, duplicates no row and does not raise.
    """
    runner = MigrationRunner(migrations_directory, migrations=migrations)
    runner.apply(connection)
    return runner.current_version(connection)
