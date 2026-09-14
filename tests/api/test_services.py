"""The framework-agnostic application layer, and the architecture boundary it must respect.

No HTTP is involved here: these tests call :mod:`api.services` directly, which is exactly what a
future FastAPI transport would do. The last test class is the architecture guard - ``core/`` must
not import ``api/`` (D1, ``tests/test_core_isolation.py``, ``python tools/doctor.py``).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from api.services import (
    DEFAULT_DB_PATH,
    IMPLEMENTED_ROUTE_MODE,
    KNOWN_SETTING_KEYS,
    ApiServices,
    CapabilityNotImplemented,
    Conflict,
    DatabaseState,
    InvalidInput,
    NotFound,
    TimezoneDataUnavailable,
    capability_report,
    health_payload,
)
from core.model.value_objects import DataProvenance
from core.repositories import AppSettingsRepository, RoutePlanRepository
from demo.dataset import DEMO_PLAN_ID, build_demo_plan
from storage import StorageError
from storage.sqlite.database import SCHEMA_VERSION, connect
from tests.api.support import cleanup_scratch_root, new_database_file, new_scratch_directory

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root


class ServiceTestCase(unittest.TestCase):
    """A clean file-backed database and a fresh :class:`ApiServices` per test.

    The database is a real file under the gitignored ``var/`` tree, created and removed per test:
    see ``tests/api/support.py`` for why a per-test file is preferred to a process-wide
    ``mode=memory`` URI even though the storage connection helper now opens such a URI correctly.
    """

    def setUp(self) -> None:
        self.scratch = new_scratch_directory("api-services")
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.database_path = new_database_file(self.scratch)
        self.services = ApiServices(self.database_path)
        self.addCleanup(self.services.close)


class DemoPlanCreationTests(ServiceTestCase):
    def test_the_created_plan_is_the_deterministic_fixture(self) -> None:
        record = self.services.plans.create_demo_plan({})
        expected = build_demo_plan()
        self.assertEqual(record.plan.id, DEMO_PLAN_ID)
        self.assertEqual(record.data_provenance, DataProvenance.DEMO_SYNTHETIC)
        self.assertEqual(record.plan.inputs_fingerprint(), expected.inputs_fingerprint())
        self.assertEqual(record.plan.stops, expected.stops)
        self.assertEqual(record.plan.departure_time, expected.departure_time)
        self.assertEqual(len(record.plan.active_stops()), 31)
        self.assertEqual(len(record.plan.disabled_stops()), 1)

    def test_the_repository_is_a_real_protocol_implementation(self) -> None:
        with self.services.state.connection() as connection:
            repository = self.services.state.plan_repository(connection)
            settings = self.services.state.settings_repository(connection)
        self.assertIsInstance(repository, RoutePlanRepository)
        self.assertIsInstance(settings, AppSettingsRepository)

    def test_creating_twice_keeps_exactly_one_plan(self) -> None:
        first = self.services.plans.create_demo_plan({})
        second = self.services.plans.create_demo_plan({})
        self.assertEqual(first.plan.id, second.plan.id)
        self.assertEqual([record.plan.id for record in self.services.plans.list_plans()], [DEMO_PLAN_ID])

    def test_a_stored_plan_round_trips_through_the_repository_exactly(self) -> None:
        created = self.services.plans.create_demo_plan({})
        loaded = self.services.plans.get_plan(DEMO_PLAN_ID)
        self.assertEqual(loaded.plan, created.plan)
        self.assertIsNone(loaded.plan.first_service_stop.selected_stop_id)
        positions = [stop.input_position for stop in loaded.plan.stops]
        self.assertEqual(positions, sorted(positions))

    def test_an_explicit_demo_plan_id_is_honoured(self) -> None:
        record = self.services.plans.create_demo_plan({"id": "demo-alt"})
        self.assertEqual(record.plan.id, "demo-alt")
        self.assertIsNotNone(self.services.plans.get_plan("demo-alt"))

    def test_a_non_demo_plan_kind_is_refused(self) -> None:
        for body in ({"kind": "addresses"}, {"kind": "import"}):
            with self.subTest(body=body):
                with self.assertRaises(CapabilityNotImplemented):
                    self.services.plans.create_demo_plan(body)

    def test_a_non_demo_provenance_is_refused(self) -> None:
        for body in (
            {"data_provenance": "REAL_ROUTING"},
            {"provenance": "REAL_ROUTING"},
        ):
            with self.subTest(body=body):
                with self.assertRaises(CapabilityNotImplemented):
                    self.services.plans.create_demo_plan(body)
        accepted = self.services.plans.create_demo_plan({"data_provenance": "DEMO_SYNTHETIC"})
        self.assertEqual(accepted.data_provenance, DataProvenance.DEMO_SYNTHETIC)


class CapabilityRefusalTests(ServiceTestCase):
    def test_an_unimplemented_capability_field_is_refused_with_its_name(self) -> None:
        cases = {
            "geocode": "demand_geocoding",
            "addresses": "demand_geocoding",
            "traffic": "demand_traffic",
            "side_of_road": "demand_side_of_road",
            "turn_by_turn": "demand_turn_by_turn",
            "optimize": "demand_real_routing",
            "routing": "demand_real_routing",
            "matrix": "demand_real_routing",
        }
        for field, capability in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(CapabilityNotImplemented) as caught:
                    self.services.plans.create_demo_plan({field: True})
                self.assertIn(capability, str(caught.exception))
                self.assertEqual(caught.exception.code, "unsupported_capability")

    def test_a_nested_provider_capability_request_is_refused(self) -> None:
        with self.assertRaises(CapabilityNotImplemented):
            self.services.plans.create_demo_plan({"provider": {"traffic": True}})

    def test_only_smart_route_is_implemented(self) -> None:
        self.assertEqual(IMPLEMENTED_ROUTE_MODE.value, "SMART_ROUTE")
        accepted = self.services.plans.create_demo_plan({"route_mode": "SMART_ROUTE"})
        self.assertEqual(accepted.plan.route_mode.value, "SMART_ROUTE")
        for mode in ("FASTEST", "SHORTEST", "MINIMUM_TURNS", "ON_THE_WAY", "START_TO_FINISH"):
            with self.subTest(mode=mode):
                with self.assertRaises(CapabilityNotImplemented) as caught:
                    self.services.plans.create_demo_plan({"route_mode": mode})
                self.assertIn(mode, str(caught.exception))

    def test_an_unknown_route_mode_is_a_validation_error_not_a_501(self) -> None:
        with self.assertRaises(InvalidInput):
            self.services.plans.create_demo_plan({"route_mode": "TELEPORT"})

    def test_an_unknown_request_field_is_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(InvalidInput):
            self.services.plans.create_demo_plan({"drag_reorder": [1, 2]})


class PlanControlTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.services.plans.create_demo_plan({})

    def stop(self, plan_id: str, stop_id: str):
        record = self.services.plans.get_plan(plan_id)
        return record.plan.stop_by_id(stop_id)

    def test_enabled_and_priority_are_applied_and_persisted(self) -> None:
        record = self.services.plans.update_plan_controls(
            DEMO_PLAN_ID,
            {
                "stops": [
                    {"stop_id": "S01-NEAR", "enabled": False},
                    {"stop_id": "S02-NEAR2", "priority": 9},
                ]
            },
        )
        self.assertFalse(record.plan.stop_by_id("S01-NEAR").enabled)
        self.assertEqual(record.plan.stop_by_id("S02-NEAR2").priority, 9)
        reloaded = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.assertFalse(reloaded.stop_by_id("S01-NEAR").enabled)
        self.assertEqual(reloaded.stop_by_id("S02-NEAR2").priority, 9)
        self.assertEqual(len(reloaded.active_stops()), 30)

    def test_input_position_and_the_first_stop_decision_are_untouched(self) -> None:
        before = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        after = self.services.plans.update_plan_controls(
            DEMO_PLAN_ID, {"stops": [{"stop_id": "S01-NEAR", "enabled": False}]}
        ).plan
        self.assertEqual(
            [stop.input_position for stop in after.stops],
            [stop.input_position for stop in before.stops],
        )
        self.assertEqual(after.first_service_stop, before.first_service_stop)
        self.assertEqual(after.first_stop_state, before.first_stop_state)

    def test_a_single_entry_may_change_both_approved_controls(self) -> None:
        record = self.services.plans.update_plan_controls(
            DEMO_PLAN_ID,
            {"stops": [{"stop_id": "S10-DISABLED", "enabled": True, "priority": 4}]},
        )
        stop = record.plan.stop_by_id("S10-DISABLED")
        self.assertTrue(stop.enabled)
        self.assertEqual(stop.priority, 4)

    def test_an_unknown_plan_is_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.services.plans.update_plan_controls(
                "nope", {"stops": [{"stop_id": "S01-NEAR", "enabled": False}]}
            )
        with self.assertRaises(NotFound):
            self.services.plans.get_plan("nope")

    def test_an_unknown_stop_is_not_found(self) -> None:
        with self.assertRaises(NotFound) as caught:
            self.services.plans.update_plan_controls(
                DEMO_PLAN_ID, {"stops": [{"stop_id": "S99-NOPE", "enabled": False}]}
            )
        self.assertIn("S99-NOPE", str(caught.exception))

    def test_unapproved_controls_are_refused(self) -> None:
        cases = (
            {"first_stop": "S01-NEAR"},
            {"stops": [{"stop_id": "S01-NEAR", "position": 2}]},
            {"stops": [{"stop_id": "S01-NEAR", "order": 2}]},
            {"stops": [{"stop_id": "S01-NEAR", "service_status": "served"}]},
            {"route_mode": "SMART_ROUTE"},
        )
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    self.services.plans.update_plan_controls(DEMO_PLAN_ID, body)

    def test_a_malformed_control_value_is_a_validation_error(self) -> None:
        cases = (
            {"stops": []},
            {"stops": "nope"},
            {"stops": [{"enabled": True}]},
            {"stops": [{"stop_id": "S01-NEAR", "enabled": 1}]},
            {"stops": [{"stop_id": "S01-NEAR", "priority": -1}]},
            {"stops": [{"stop_id": "S01-NEAR", "priority": True}]},
            {"stops": [{"stop_id": "  ", "enabled": True}]},
        )
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    self.services.plans.update_plan_controls(DEMO_PLAN_ID, body)

    def test_a_stop_change_with_no_change_is_a_conflict(self) -> None:
        with self.assertRaises(Conflict) as caught:
            self.services.plans.update_plan_controls(
                DEMO_PLAN_ID, {"stops": [{"stop_id": "S01-NEAR"}]}
            )
        self.assertEqual(caught.exception.code, "illegal_state")

    def test_a_body_that_is_not_an_object_is_refused(self) -> None:
        for body in ([], "text", 3, None):
            with self.subTest(body=body):
                with self.assertRaises(InvalidInput):
                    self.services.plans.update_plan_controls(DEMO_PLAN_ID, body)

    def test_a_plan_id_that_is_not_one_path_segment_is_refused(self) -> None:
        for plan_id in ("a/b", "a\\b", "", "  ", "x" * 201):
            with self.subTest(plan_id=plan_id):
                with self.assertRaises(InvalidInput):
                    self.services.plans.get_plan(plan_id)


class SettingsServiceTests(ServiceTestCase):
    def test_the_tile_keys_round_trip(self) -> None:
        self.assertIn("tile_url", KNOWN_SETTING_KEYS)
        self.assertIn("tile_attribution", KNOWN_SETTING_KEYS)
        self.assertIn("tile_max_zoom", KNOWN_SETTING_KEYS)
        for key, value in (
            ("tile_url", "https://tile.example/{z}/{x}/{y}.png"),
            ("tile_attribution", "(c) demo tiles"),
            ("tile_max_zoom", 19),
        ):
            with self.subTest(key=key):
                stored = self.services.settings.set_setting(key, {"value": value})
                self.assertEqual(stored.value, value)
                self.assertTrue(stored.configured)
                fetched = self.services.settings.get_setting(key)
                self.assertEqual(fetched.value, value)

    def test_an_unset_key_is_not_found(self) -> None:
        with self.assertRaises(NotFound) as caught:
            self.services.settings.get_setting("tile_url")
        self.assertIn("tile_url", str(caught.exception))
        # The transport refines this application-level NotFound into the documented
        # "unknown_setting" code (see tests/api/test_http_server.py); the service layer itself
        # reports the framework-neutral outcome and no HTTP notion.

    def test_no_default_is_invented_for_an_unset_key(self) -> None:
        """A missing setting is missing: the service substitutes nothing."""
        for key in KNOWN_SETTING_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(NotFound):
                    self.services.settings.get_setting(key)

    def test_a_value_is_replaced_not_merged(self) -> None:
        self.services.settings.set_setting("tile_max_zoom", {"value": 12})
        self.services.settings.set_setting("tile_max_zoom", {"value": {"zoom": 18}})
        self.assertEqual(self.services.settings.get_setting("tile_max_zoom").value, {"zoom": 18})

    def test_a_value_that_is_not_json_is_refused_by_the_store(self) -> None:
        """The store's own refusal is reported; the service does not soften it (D26)."""
        with self.assertRaises(StorageError):
            self.services.settings.set_setting("bad", {"value": {1, 2}})

    def test_a_missing_value_field_is_refused(self) -> None:
        with self.assertRaises(InvalidInput):
            self.services.settings.set_setting("tile_url", {})
        with self.assertRaises(InvalidInput):
            self.services.settings.set_setting("tile_url", {"value": 1, "extra": 2})

    def test_a_key_that_is_not_one_path_segment_is_refused(self) -> None:
        for key in ("a/b", "", "  ", "a b", "a?b", "x" * 201):
            with self.subTest(key=key):
                with self.assertRaises(InvalidInput):
                    self.services.settings.get_setting(key)


class DatabaseStateTests(unittest.TestCase):
    """The database wiring: one migration at startup, one connection per request."""

    def setUp(self) -> None:
        self.scratch = new_scratch_directory("api-state")
        self.addCleanup(shutil.rmtree, self.scratch, True)

    def test_the_default_database_path_is_gitignored(self) -> None:
        self.assertEqual(DEFAULT_DB_PATH, "var/routepilot.db")
        self.assertTrue(DEFAULT_DB_PATH.startswith("var/"))
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split()
        self.assertIn("var/", ignored)

    def test_the_schema_is_migrated_once_at_startup(self) -> None:
        state = DatabaseState(new_database_file(self.scratch))
        self.addCleanup(state.close)
        self.assertEqual(state.schema_version, SCHEMA_VERSION)
        # The migration runner lives in storage; running it again here must apply nothing.
        from storage.sqlite.database import MigrationRunner

        with state.connection() as connection:
            self.assertEqual(MigrationRunner().apply(connection), ())

    def test_a_database_from_a_newer_build_is_refused_loudly(self) -> None:
        """A stored schema this build does not know is a loud failure, never a silent downgrade."""
        path = new_database_file(self.scratch)
        connection = connect(path)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_utc TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at_utc) VALUES (?, ?)",
                (SCHEMA_VERSION + 1, "2026-09-11T01:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(StorageError) as caught:
            DatabaseState(path)
        self.assertIn("migration", str(caught.exception).lower())

    def test_every_request_opens_its_own_connection_with_foreign_keys_on(self) -> None:
        state = DatabaseState(new_database_file(self.scratch))
        self.addCleanup(state.close)
        with state.connection() as first:
            with state.connection() as second:
                self.assertIsNot(first, second)
                self.assertEqual(first.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(second.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_connection_is_closed_when_the_request_ends(self) -> None:
        state = DatabaseState(new_database_file(self.scratch))
        self.addCleanup(state.close)
        with state.connection() as connection:
            pass
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_a_repository_that_does_not_report_provenance_is_believed_as_demo(self) -> None:
        """The demo fixture is the only plan this build creates, and it is DEMO/SYNTHETIC."""

        class FakeRepository:
            data_provenance = DataProvenance.DEMO_SYNTHETIC

            def save(self, plan):  # pragma: no cover - not exercised here
                raise AssertionError

            def get(self, plan_id):
                return build_demo_plan()

            def list(self):
                return (build_demo_plan(),)

            def delete(self, plan_id):  # pragma: no cover - not exercised here
                return False

        services = ApiServices(
            new_database_file(self.scratch),
            plan_repository_factory=lambda connection, *, data_provenance: FakeRepository(),
        )
        self.addCleanup(services.close)
        record = services.plans.list_plans()[0]
        self.assertEqual(record.data_provenance, DataProvenance.DEMO_SYNTHETIC)


class HealthAndCapabilityTests(ServiceTestCase):
    def test_there_is_no_timezone_availability_exception_raised_by_health(self) -> None:
        """Health reports the tzdata state; it does not fail because tzdata is missing."""
        payload = health_payload(self.services.state)
        self.assertIn(payload["timezone_data"]["source"], ("package", "system", "missing"))

    def test_the_capability_report_splits_implemented_from_the_rest(self) -> None:
        implemented, not_implemented = capability_report()
        self.assertTrue(implemented)
        self.assertTrue(not_implemented)
        names = {entry["capability"] for entry in implemented}
        self.assertIn("engine", names)
        self.assertIn("route_mode:SMART_ROUTE", names)
        for entry in not_implemented:
            self.assertNotEqual(entry["status"], "implemented")
        statuses = {entry["status"] for entry in not_implemented}
        self.assertTrue(statuses <= {"planned", "requires_provider", "unsupported"})

    def test_requires_provider_entries_name_what_they_need(self) -> None:
        _implemented, not_implemented = capability_report()
        demanded = [
            entry
            for entry in not_implemented
            if entry["status"] == "requires_provider"
        ]
        self.assertTrue(demanded)
        for entry in demanded:
            self.assertTrue(entry["requires"], msg=entry)

    def test_the_health_payload_is_json_serialisable(self) -> None:
        payload = health_payload(self.services.state)
        json.dumps(payload)

    def test_timezone_unavailability_is_a_distinct_error_type(self) -> None:
        error = TimezoneDataUnavailable("no database")
        self.assertEqual(error.code, "timezone_data_unavailable")
        self.assertIn("pip install tzdata", error.fix)


class CoreIsolationTests(unittest.TestCase):
    """``core/`` must never depend on ``api/`` (D1; the check ``doctor`` runs must stay green)."""

    def test_no_core_module_imports_the_api_package(self) -> None:
        pattern = re.compile(r"^(?:import|from)\s+api(?:\.|\s|$)")
        offenders = []
        for path in sorted((REPO_ROOT / "core").rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.match(line.strip()):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_importing_core_never_imports_api(self) -> None:
        """Proven in a fresh interpreter: importing every core module loads no ``api`` module."""
        script = (
            "import sys, pkgutil, importlib, core\n"
            "for info in pkgutil.walk_packages(core.__path__, 'core.'):\n"
            "    importlib.import_module(info.name)\n"
            "loaded = sorted(name for name in sys.modules if name == 'api' "
            "or name.startswith('api.'))\n"
            "print(','.join(loaded))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr.decode("utf-8", "replace"))
        loaded = completed.stdout.decode("utf-8", "replace").strip()
        self.assertEqual(loaded, "", msg=f"importing core pulled in: {loaded}")

    def test_the_isolation_scanner_reports_no_violation(self) -> None:
        from tools.isolation_check import scan_core

        self.assertEqual(scan_core(REPO_ROOT), ())


if __name__ == "__main__":
    unittest.main()

