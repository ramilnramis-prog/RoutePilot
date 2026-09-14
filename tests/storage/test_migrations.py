"""Stage 3 U9 (D38): storage skeleton, ordered idempotent migrations and the 0001 schema.

The expectations in this module are written out literally (table names, column names, index
names, constraint violations) instead of being read back from ``docs/STORAGE_SCHEMA.md``, so the
test fails if the DDL and the approved schema drift apart.

Every database here is ``:memory:`` - the sandbox has no usable temporary-directory facility - so
the suite cannot create a scratch directory or a database file in the repository.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from storage import StorageError, StorageMigrationError
from storage.sqlite import database
from storage.sqlite.database import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_TABLE,
    MigrationRunner,
    connect,
    current_version,
    discover_migrations,
    migrate,
    plan_migrations,
    utc_now_iso,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Absolute timestamps are UTC ISO-8601 with a trailing ``Z`` (schema section 1).
UTC_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _pragma_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(str(row["name"]) for row in rows)


def _index_names(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA index_list({table})").fetchall()
    return tuple(str(row["name"]) for row in rows)


# --------------------------------------------------------------------------- #
# literal expectation lists (section 2-6 of the approved schema)
# --------------------------------------------------------------------------- #
EXPECTED_TABLES: dict[str, tuple[str, ...]] = {
    "schema_migrations": ("version", "applied_at_utc"),
    "route_plans": (
        "id",
        "name",
        "timezone",
        "departure_label",
        "departure_latitude",
        "departure_longitude",
        "departure_time_utc",
        "finish_label",
        "finish_latitude",
        "finish_longitude",
        "route_mode",
        "first_stop_mode",
        "first_stop_selected_stop_id",
        "first_stop_selection_source",
        "first_stop_pinned",
        "window_end_policy",
        "order_overrides_json",
        "cost_policy_json",
        "default_service_duration_sec",
        "inputs_fingerprint",
        "data_provenance",
        "created_at_utc",
        "updated_at_utc",
    ),
    "route_stops": (
        "id",
        "plan_id",
        "input_position",
        "raw_address",
        "normalized_address",
        "latitude",
        "longitude",
        "geocode_status",
        "geocode_provider",
        "geocode_checked_at_utc",
        "service_window_kind",
        "service_window_start",
        "service_window_end",
        "window_end_policy",
        "service_duration_sec",
        "priority",
        "service_status",
        "enabled",
        "notes",
        "created_at_utc",
        "updated_at_utc",
    ),
    "route_optimization_runs": (
        "id",
        "plan_id",
        "run_kind",
        "algorithm",
        "algorithm_version",
        "inputs_fingerprint",
        "route_fingerprint",
        "tzdata_version",
        "cost_policy_json",
        "data_provenance",
        "status",
        "order_json",
        "first_stop_recommendation_json",
        "top_k_json",
        "violations_json",
        "metrics_json",
        "created_at_utc",
    ),
    "app_settings": ("key", "value_json", "updated_at_utc"),
}

EXPECTED_INDEXES: dict[str, tuple[str, ...]] = {
    "schema_migrations": (),
    "route_plans": (),
    "route_stops": ("idx_route_stops_plan", "idx_route_stops_plan_enabled"),
    "route_optimization_runs": ("idx_runs_plan_created",),
    "app_settings": (),
}

ORDER_OVERRIDES_DEFAULT = '{"version": 1, "constraints": []}'

NOW_UTC = "2026-09-11T01:00:00Z"


def _insert_plan(connection: sqlite3.Connection, plan_id: str = "plan-1") -> None:
    """A minimal row satisfying every NOT NULL plan column; individual tests override fields."""
    connection.execute(
        """
        INSERT INTO route_plans (
            id, name, timezone, departure_label, departure_latitude, departure_longitude,
            departure_time_utc, finish_label, finish_latitude, finish_longitude, route_mode,
            first_stop_mode, data_provenance, cost_policy_json, created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            "Demo plan",
            "Europe/Moscow",
            "Warehouse",
            55.75,
            37.62,
            NOW_UTC,
            "Depot",
            55.70,
            37.55,
            "SMART_ROUTE",
            "recommend",
            "DEMO_SYNTHETIC",
            '{"name": "smart_route_elapsed_v1", "weights": {}, "provisional": false}',
            NOW_UTC,
            NOW_UTC,
        ),
    )


def _insert_stop(
    connection: sqlite3.Connection, stop_id: str = "S01", plan_id: str = "plan-1", **overrides
) -> None:
    """A minimal row satisfying every NOT NULL stop column; tests override one field at a time."""
    values: dict[str, object] = {
        "id": stop_id,
        "plan_id": plan_id,
        "input_position": 0,
        "raw_address": "S01 street 1",
        "geocode_status": "pending",
        "service_window_kind": "unrestricted",
        "created_at_utc": NOW_UTC,
        "updated_at_utc": NOW_UTC,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO route_stops ({columns}) VALUES ({placeholders})", tuple(values.values())
    )


def _insert_run(
    connection: sqlite3.Connection, run_id: str = "run-1", plan_id: str = "plan-1", **overrides
) -> None:
    values: dict[str, object] = {
        "id": run_id,
        "plan_id": plan_id,
        "run_kind": "optimize",
        "algorithm": "greedy_seed+2opt",
        "algorithm_version": "1",
        "inputs_fingerprint": "inputs-digest",
        "route_fingerprint": "route-digest",
        "cost_policy_json": "{}",
        "data_provenance": "DEMO_SYNTHETIC",
        "status": "ok",
        "order_json": '{"order": []}',
        "first_stop_recommendation_json": '{"recommended_stop_id": null}',
        "violations_json": "[]",
        "metrics_json": "{}",
        "created_at_utc": NOW_UTC,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO route_optimization_runs ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


class MigrationPlanHelperTests(unittest.TestCase):
    """The ordering/validation helper is pure: names in, plan out (no filesystem)."""

    def test_returns_ascending_plan_for_unsorted_names(self) -> None:
        plan = plan_migrations(["0003_add_runs.sql", "0001_init.sql", "0002_add_settings.sql"])
        self.assertEqual([migration.version for migration in plan], [1, 2, 3])
        self.assertEqual(
            [migration.filename for migration in plan],
            ["0001_init.sql", "0002_add_settings.sql", "0003_add_runs.sql"],
        )
        self.assertEqual(plan[0].name, "init")

    def test_empty_sequence_is_an_empty_plan(self) -> None:
        self.assertEqual(plan_migrations([]), ())

    def test_refuses_a_duplicate_version(self) -> None:
        with self.assertRaises(StorageMigrationError) as caught:
            plan_migrations(["0001_init.sql", "0001_init_again.sql"])
        message = str(caught.exception)
        self.assertIn("duplicate", message)
        self.assertIn("0001_init.sql", message)
        self.assertIn("0001_init_again.sql", message)
        self.assertIsInstance(caught.exception, StorageError)

    def test_refuses_a_gap_in_the_sequence(self) -> None:
        with self.assertRaises(StorageMigrationError) as caught:
            plan_migrations(["0001_init.sql", "0003_add_runs.sql"])
        self.assertIn("gap", str(caught.exception))
        self.assertIn("0002", str(caught.exception))

    def test_refuses_a_malformed_file_name(self) -> None:
        for bad_name in ("init.sql", "1_init.sql", "0001-init.sql", "0001_init.SQL"):
            with self.subTest(name=bad_name):
                with self.assertRaises(StorageMigrationError):
                    plan_migrations(["0001_init.sql", bad_name])

    def test_refuses_a_sequence_that_does_not_start_at_one(self) -> None:
        with self.assertRaises(StorageMigrationError) as caught:
            plan_migrations(["0002_add_settings.sql"])
        self.assertIn("start at version 1", str(caught.exception))


class UtcTimestampTests(unittest.TestCase):
    def test_formats_utc_with_a_trailing_z(self) -> None:
        moment = datetime(2026, 9, 11, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(utc_now_iso(moment), NOW_UTC)
        self.assertRegex(utc_now_iso(moment), UTC_Z_RE)

    def test_now_is_utc_iso8601_with_z(self) -> None:
        self.assertRegex(utc_now_iso(), UTC_Z_RE)

    def test_refuses_a_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            utc_now_iso(datetime(2026, 9, 11, 1, 0, 0))


class MigrationTests(unittest.TestCase):
    """Fresh migration, idempotency, schema shape, constraints, foreign keys and cascades."""

    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        self.applied = migrate(self.connection)

    # -- migration bookkeeping ------------------------------------------------ #
    def test_fresh_memory_database_migrates_to_version_one(self) -> None:
        self.assertEqual(self.applied, 1)
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(current_version(self.connection), 1)
        rows = self.connection.execute(
            "SELECT version, applied_at_utc FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], 1)
        self.assertRegex(rows[0]["applied_at_utc"], UTC_Z_RE)

    def test_second_migration_is_a_noop(self) -> None:
        before = self.connection.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        ).fetchone()["n"]
        self.assertEqual(migrate(self.connection), 1)
        self.assertEqual(migrate(self.connection), 1)
        after = self.connection.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        ).fetchone()["n"]
        self.assertEqual(before, 1)
        self.assertEqual(after, 1)

    def test_pending_is_empty_once_current(self) -> None:
        runner = MigrationRunner()
        self.assertEqual(runner.known_versions, (1,))
        self.assertEqual(runner.target_version, 1)
        self.assertEqual(runner.pending(self.connection), ())

    def test_discovery_orders_the_packaged_migration(self) -> None:
        discovered = discover_migrations()
        self.assertEqual([migration.filename for migration in discovered], ["0001_init.sql"])

    def test_connection_enforces_foreign_keys(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_refuses_a_version_the_build_does_not_know(self) -> None:
        self.connection.execute(
            "INSERT INTO schema_migrations (version, applied_at_utc) VALUES (99, ?)", (NOW_UTC,)
        )
        with self.assertRaises(StorageMigrationError):
            migrate(self.connection)

    def test_a_modified_already_applied_migration_is_not_detected(self) -> None:
        """The honest limitation: ``schema_migrations`` records only (version, applied_at_utc).

        The gate is the recorded version, so a released migration whose file was edited
        afterwards is neither detected nor re-applied - detection cannot be added without changing
        the approved schema (no checksum column exists).
        """
        recorded = self.connection.execute(
            "SELECT version, applied_at_utc FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in recorded], [1])
        self.assertRegex(recorded[0]["applied_at_utc"], UTC_Z_RE)

        edited = MigrationRunner.from_filenames(["0001_init_edited_after_release.sql"])
        self.assertEqual(edited.pending(self.connection), ())
        self.assertEqual(edited.apply(self.connection), ())

        after = self.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        self.assertEqual([row["version"] for row in after], [1])

    # -- schema shape --------------------------------------------------------- #
    def test_every_expected_table_exists(self) -> None:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        self.assertEqual([row["name"] for row in rows], sorted(EXPECTED_TABLES))

    def test_every_expected_column_exists(self) -> None:
        for table, expected in EXPECTED_TABLES.items():
            with self.subTest(table=table):
                self.assertEqual(
                    set(_pragma_columns(self.connection, table)),
                    set(expected),
                    msg=f"{table} columns differ from the approved schema",
                )

    def test_declared_column_types_and_defaults(self) -> None:
        plans = {
            row["name"]: row
            for row in self.connection.execute("PRAGMA table_info(route_plans)").fetchall()
        }
        self.assertEqual(plans["departure_latitude"]["type"], "REAL")
        self.assertEqual(plans["first_stop_pinned"]["type"], "INTEGER")
        self.assertEqual(plans["first_stop_pinned"]["dflt_value"], "0")
        self.assertEqual(plans["window_end_policy"]["dflt_value"], "'service_finish_before_end'")
        self.assertEqual(plans["route_mode"]["notnull"], 1)
        self.assertEqual(plans["name"]["notnull"], 0)
        self.assertEqual(plans["default_service_duration_sec"]["notnull"], 0)

        stops = {
            row["name"]: row
            for row in self.connection.execute("PRAGMA table_info(route_stops)").fetchall()
        }
        self.assertEqual(stops["enabled"]["dflt_value"], "1")
        self.assertEqual(stops["service_status"]["dflt_value"], "'pending'")
        self.assertEqual(stops["input_position"]["dflt_value"], None)
        self.assertEqual(stops["input_position"]["notnull"], 1)
        self.assertEqual(stops["service_window_start"]["notnull"], 0)
        self.assertEqual(stops["window_end_policy"]["notnull"], 0)

        runs = {
            row["name"]: row
            for row in self.connection.execute(
                "PRAGMA table_info(route_optimization_runs)"
            ).fetchall()
        }
        self.assertEqual(runs["tzdata_version"]["notnull"], 0)
        self.assertEqual(runs["top_k_json"]["notnull"], 0)
        self.assertEqual(runs["metrics_json"]["notnull"], 1)

    def test_every_documented_index_exists(self) -> None:
        for table, expected in EXPECTED_INDEXES.items():
            with self.subTest(table=table):
                names = _index_names(self.connection, table)
                for index in expected:
                    self.assertIn(index, names, msg=f"{index} missing on {table}")
                self.assertEqual(
                    len([name for name in names if name in expected]), len(expected)
                )
        plan_index_columns = [
            row["name"]
            for row in self.connection.execute("PRAGMA index_info(idx_route_stops_plan)").fetchall()
        ]
        self.assertEqual(plan_index_columns, ["plan_id", "input_position"])
        enabled_index_columns = [
            row["name"]
            for row in self.connection.execute(
                "PRAGMA index_info(idx_route_stops_plan_enabled)"
            ).fetchall()
        ]
        self.assertEqual(enabled_index_columns, ["plan_id", "enabled"])
        run_index_columns = [
            row["name"]
            for row in self.connection.execute("PRAGMA index_info(idx_runs_plan_created)").fetchall()
        ]
        self.assertEqual(run_index_columns, ["plan_id", "created_at_utc"])

    def test_order_overrides_default_is_the_versioned_envelope(self) -> None:
        _insert_plan(self.connection)
        row = self.connection.execute(
            "SELECT order_overrides_json, first_stop_pinned, window_end_policy, "
            "first_stop_selected_stop_id FROM route_plans WHERE id = 'plan-1'"
        ).fetchone()
        self.assertEqual(row["order_overrides_json"], ORDER_OVERRIDES_DEFAULT)
        self.assertEqual(row["first_stop_pinned"], 0)
        self.assertEqual(row["window_end_policy"], "service_finish_before_end")
        self.assertIsNone(row["first_stop_selected_stop_id"])

    def test_no_recommended_stop_id_column_anywhere(self) -> None:
        for table in EXPECTED_TABLES:
            with self.subTest(table=table):
                self.assertNotIn("recommended_stop_id", _pragma_columns(self.connection, table))

    # -- CHECK constraints rejected by SQLite itself -------------------------- #
    def test_rejects_an_unknown_route_mode(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_plan(self.connection)
            self.connection.execute("UPDATE route_plans SET route_mode = 'AUTO'")

    def test_rejects_the_revoked_auto_first_stop_mode(self) -> None:
        # The AUTO model was revoked (D4): only 'recommend' and 'manual' are storable.
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE route_plans SET first_stop_mode = 'auto'")

    def test_rejects_unknown_enumerations(self) -> None:
        cases = (
            ("route_plans", "data_provenance = 'MADE_UP'"),
            ("route_plans", "first_stop_selection_source = 'engine_pick'"),
            ("route_plans", "window_end_policy = 'whatever'"),
            ("route_stops", "geocode_status = 'guessed'"),
            ("route_stops", "service_window_kind = 'sometimes'"),
            ("route_stops", "service_status = 'maybe'"),
        )
        for table, assignment in cases:
            with self.subTest(case=f"{table}: {assignment}"):
                with self.assertRaises(sqlite3.IntegrityError):
                    if table == "route_plans":
                        _insert_plan(self.connection)
                        self.connection.execute(f"UPDATE route_plans SET {assignment}")
                    else:
                        _insert_plan(self.connection)
                        _insert_stop(self.connection)
                        self.connection.execute(f"UPDATE route_stops SET {assignment}")

    def test_rejects_a_fixed_window_without_its_times(self) -> None:
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(
                self.connection,
                service_window_kind="fixed",
                latitude=55.75,
                longitude=37.62,
                geocode_status="resolved",
            )

    def test_rejects_times_on_a_non_fixed_window(self) -> None:
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(
                self.connection,
                service_window_kind="unrestricted",
                service_window_start="08:00:00",
                service_window_end="12:00:00",
            )

    def test_rejects_resolved_geocoding_without_coordinates(self) -> None:
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(self.connection, geocode_status="resolved")

    def test_rejects_a_fixed_window_without_resolved_coordinates(self) -> None:
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(
                self.connection,
                service_window_kind="fixed",
                service_window_start="08:00:00",
                service_window_end="12:00:00",
            )

    def test_rejects_a_window_end_policy_on_a_non_fixed_window(self) -> None:
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(
                self.connection,
                service_window_kind="unrestricted",
                window_end_policy="service_start_before_end",
            )

    def test_rejects_a_negative_input_position(self) -> None:
        _insert_plan(self.connection)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(self.connection, input_position=-1)

    def test_rejects_a_duplicate_plan_input_position(self) -> None:
        _insert_plan(self.connection)
        _insert_stop(self.connection, stop_id="S01", input_position=0)
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(self.connection, stop_id="S02", input_position=0)

    def test_allows_gaps_in_input_position(self) -> None:
        _insert_plan(self.connection)
        _insert_stop(self.connection, stop_id="S01", input_position=0)
        _insert_stop(self.connection, stop_id="S02", input_position=7)
        positions = [
            row["input_position"]
            for row in self.connection.execute(
                "SELECT input_position FROM route_stops ORDER BY input_position"
            ).fetchall()
        ]
        self.assertEqual(positions, [0, 7])

    def test_rejects_an_unknown_run_enumeration(self) -> None:
        _insert_plan(self.connection)
        for assignment in ("run_kind = 'sideways'", "status = 'finished'"):
            with self.subTest(case=assignment):
                with self.assertRaises(sqlite3.IntegrityError):
                    _insert_run(self.connection)
                    self.connection.execute(
                        f"UPDATE route_optimization_runs SET {assignment}"
                    )

    # -- foreign keys and cascades -------------------------------------------- #
    def test_unknown_plan_id_is_rejected_by_the_foreign_key(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_stop(self.connection, plan_id="no-such-plan")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_run(self.connection, plan_id="no-such-plan")

    def test_delete_cascades_to_stops_and_runs(self) -> None:
        _insert_plan(self.connection)
        _insert_stop(self.connection, stop_id="S01", input_position=0)
        _insert_stop(self.connection, stop_id="S02", input_position=1)
        _insert_run(self.connection)
        self.connection.execute("DELETE FROM route_plans WHERE id = 'plan-1'")
        stops = self.connection.execute("SELECT COUNT(*) AS n FROM route_stops").fetchone()["n"]
        runs = self.connection.execute(
            "SELECT COUNT(*) AS n FROM route_optimization_runs"
        ).fetchone()["n"]
        self.assertEqual((stops, runs), (0, 0))

    def test_foreign_keys_are_on_for_a_second_connection(self) -> None:
        # PRAGMA foreign_keys is per connection; the helper must not rely on a remembered setting.
        other = connect(":memory:")
        self.addCleanup(other.close)
        self.assertEqual(other.execute("PRAGMA foreign_keys").fetchone()[0], 1)


class MigrationScriptAtomicityTests(unittest.TestCase):
    """A failing multi-statement migration script leaves nothing behind (no files, no scratch dir).

    The migration runner wraps ``BEGIN IMMEDIATE`` ... DDL ... version insert ... ``COMMIT`` in a
    single script, and ``_execute_script`` rolls that transaction back when a statement fails.
    Driving ``_execute_script`` directly keeps the contract testable with a ``:memory:`` database.
    """

    #: Two identically named CREATE TABLE statements: the second always fails mid-script.
    FAILING_SCRIPT = (
        "BEGIN IMMEDIATE;\n"
        "CREATE TABLE probe_partial (id TEXT PRIMARY KEY);\n"
        "CREATE TABLE probe_partial (id TEXT PRIMARY KEY);\n"
        f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at_utc) VALUES (2, '{NOW_UTC}');\n"
        "COMMIT;\n"
    )

    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        self.runner = MigrationRunner()
        self.before = self._recorded_versions()

    def _recorded_versions(self) -> list[int]:
        rows = self.connection.execute(
            f"SELECT version FROM {SCHEMA_VERSION_TABLE} ORDER BY version"
        ).fetchall()
        return [int(row["version"]) for row in rows]

    def _table_exists(self, name: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    def test_partially_created_table_does_not_survive_a_later_commit(self) -> None:
        with self.assertRaises(sqlite3.Error):
            self.runner._execute_script(self.connection, self.FAILING_SCRIPT)
        self.assertFalse(self.connection.in_transaction)
        self.connection.commit()
        self.assertFalse(self._table_exists("probe_partial"))
        self.assertEqual(self._recorded_versions(), self.before)

    def test_failed_script_rolls_back_before_the_error_propagates(self) -> None:
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.runner._execute_script(self.connection, self.FAILING_SCRIPT)
        self.assertIn("probe_partial", str(caught.exception))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self._recorded_versions(), self.before)

    def test_successful_script_is_committed(self) -> None:
        self.runner._execute_script(
            self.connection, "BEGIN IMMEDIATE;\nCREATE TABLE probe_ok (id TEXT PRIMARY KEY);\nCOMMIT;\n"
        )
        self.assertFalse(self.connection.in_transaction)
        self.connection.commit()
        self.assertTrue(self._table_exists("probe_ok"))

    def test_partial_ddl_persists_if_the_caller_ignores_the_failure(self) -> None:
        """The failure mode the wrapper removes: without the rollback a commit() persists the DDL."""
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.Error):
            connection.executescript(self.FAILING_SCRIPT)
        self.assertTrue(connection.in_transaction)
        connection.commit()
        self.assertTrue(self._table_exists_on(connection, "probe_partial"))

    @staticmethod
    def _table_exists_on(connection: sqlite3.Connection, name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )


class SqliteIdentifierTests(unittest.TestCase):
    """A documented in-memory identifier must never become a filesystem artifact (U14 fix).

    ``file:name?mode=memory&cache=shared`` is SQLite's in-process URI form, and it is only in-memory
    when the connection is opened with URI semantics. Without them ``sqlite3`` treats the whole
    string as a *filename* - on Windows ``file:name?...`` is read as an NTFS alternate data stream,
    so the artifact that appeared was literally named ``file``.
    """

    #: A shared-cache in-memory database name. It is deliberately fixed: the database exists only
    #: while a connection to it is open, so two connections of this one test share it and nothing
    #: leaks into another test (or another module) once both are closed.
    MEMORY_URI = "file:routepilot-memory-identifier-tests?mode=memory&cache=shared"

    @classmethod
    def setUpClass(cls) -> None:
        cls.artifacts_before = cls._working_directory_entries()

    @staticmethod
    def _working_directory_entries() -> set[str]:
        """Every entry of the process working directory and the repository root (names only)."""
        entries = {path.name for path in Path.cwd().iterdir()}
        entries |= {path.name for path in REPO_ROOT.iterdir()}
        return entries

    def test_a_memory_uri_identifier_creates_no_file_at_all(self) -> None:
        first = connect(self.MEMORY_URI)
        second = connect(self.MEMORY_URI)
        try:
            migrate(first)
            first.execute(
                "INSERT INTO app_settings (key, value_json, updated_at_utc) "
                "VALUES ('probe', '1', '2026-09-11T00:00:00Z')"
            )
            first.commit()
            # The second connection sees the same in-process database: it really is one database.
            self.assertIsNotNone(
                second.execute(
                    "SELECT value_json FROM app_settings WHERE key = 'probe'"
                ).fetchone()
            )
        finally:
            first.close()
            second.close()
        self.assertFalse((Path.cwd() / "file").exists(), msg="a file named 'file' was created")
        self.assertFalse((REPO_ROOT / "file").exists(), msg="a file named 'file' was created")
        created = sorted(self._working_directory_entries() - self.artifacts_before)
        self.assertEqual(created, [], msg=f"the identifier created filesystem entries: {created}")

    def test_an_ordinary_file_identifier_still_uses_a_real_file(self) -> None:
        """Only a ``file:`` identifier gets URI semantics; a path is still opened as a path."""
        scratch = REPO_ROOT / "var" / "storage-identifier-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        target = scratch / "routepilot.db"
        connection = connect(str(target))
        try:
            migrate(connection)
            self.assertEqual(current_version(connection), SCHEMA_VERSION)
        finally:
            connection.close()
        self.assertTrue(target.is_file(), msg="the file-backed identifier wrote no database file")

    def test_a_windows_style_path_is_not_parsed_as_a_uri_scheme(self) -> None:
        """``C:\\...`` contains a colon, so ``uri=True`` must not be passed unconditionally.

        The identifier is only classified by its ``file:`` prefix. A drive-letter path is therefore
        handed to SQLite as a plain path - here an *unopenable* one, which fails as a path error
        instead of being silently reinterpreted as the URI scheme ``c``.
        """
        with self.assertRaises(sqlite3.OperationalError):
            connect("C:\\routepilot-does-not-exist\\nested\\routepilot.db")


class RepositoryCleanlinessTests(unittest.TestCase):
    """The suite must not leave a database file or a dirty working tree behind."""

    def _git_status(self) -> list[str]:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr.decode("utf-8", "replace"))
        return [
            line
            for line in completed.stdout.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]

    def test_no_database_artifacts_in_the_repository(self) -> None:
        patterns = ("*.db", "*.sqlite", "*.sqlite3", "*.db-wal", "*.db-shm", "*.db-journal")
        found: list[str] = []
        for pattern in patterns:
            for path in REPO_ROOT.rglob(pattern):
                found.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(found, [], msg="database artifacts must never be written to the repo")

    def test_memory_migration_creates_no_file(self) -> None:
        connection = connect(":memory:")
        try:
            migrate(connection)
        finally:
            connection.close()
        storage_root = REPO_ROOT / "storage"
        leftovers = [path for path in storage_root.rglob("*") if path.suffix in {".db", ".sqlite"}]
        self.assertEqual(leftovers, [])

    def test_running_migrations_adds_no_untracked_database_file(self) -> None:
        """The tree may legitimately be dirty mid-unit; migrations must add no NEW artifact.

        The suite has to be able to prove *itself* harmless without demanding an empty tree, so
        this compares git status before and after a migration and rejects anything database-shaped.
        """
        before = set(self._git_status())
        connection = connect(":memory:")
        try:
            self.assertEqual(migrate(connection), 1)
            self.assertEqual(migrate(connection), 1)
        finally:
            connection.close()
        after = set(self._git_status())
        added = sorted(after - before)
        self.assertEqual(added, [], msg=f"migrations added working-tree entries: {added}")
        database_entries = [
            entry for entry in after if re.search(r"\.(db|sqlite3?|db-wal|db-shm|db-journal)", entry)
        ]
        self.assertEqual(database_entries, [], msg="git reports a database artifact")


if __name__ == "__main__":
    unittest.main()
