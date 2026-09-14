"""The serialisation contracts of ``api/serialization.py`` (Stage 4 U13, deliverable 4).

These tests pin the contract, not the transport: UTC instants end in ``Z``, durations are integer
**seconds**, booleans are JSON booleans (never 0/1), enums are their string values, fingerprints are
hex text, service windows are LOCAL ``HH:MM:SS`` text, and **no payload ever carries a
recommendation as plan state**.
"""

from __future__ import annotations

import json
import re
import shutil
import unittest

from api import serialization
from api.services import ApiServices
from tests.api.support import cleanup_scratch_root, new_database_file, new_scratch_directory

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

#: ``2026-09-11T01:00:00Z`` - UTC, second precision, trailing Z.
UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
LOCAL_HHMMSS = re.compile(r"^\d{2}:\d{2}:\d{2}$")

DEMO_PLAN_ID = "demo-route-01"


class PayloadContractTests(unittest.TestCase):
    """One shared demo plan, serialised once; every assertion reads that payload."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = new_scratch_directory("api-serialization")
        cls.services = ApiServices(new_database_file(cls.scratch))
        record = cls.services.plans.create_demo_plan({})
        cls.record = record
        cls.payload = serialization.plan_payload(record.plan, record.data_provenance)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.services.close()
        shutil.rmtree(cls.scratch, ignore_errors=True)

    # -- overall shape ---------------------------------------------------- #
    def test_the_payload_is_json_safe(self) -> None:
        self.assertTrue(serialization.is_json_safe(self.payload))
        json.dumps(self.payload)  # raising here would be the failure

    def test_is_json_safe_rejects_domain_representations(self) -> None:
        from datetime import datetime, timezone
        from enum import Enum

        class Colour(Enum):
            RED = "red"

        for value in (Colour.RED, datetime.now(timezone.utc), ("a",), {1, 2}, b"bytes"):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(serialization.is_json_safe(value))
        self.assertTrue(serialization.is_json_safe({"a": [1, None, True, 1.5, "x"]}))

    def test_the_document_envelope_names_the_payload_and_version(self) -> None:
        document = serialization.plan_document(self.record.plan, self.record.data_provenance)
        self.assertEqual(document["type"], serialization.PLAN_PAYLOAD_TYPE)
        self.assertEqual(document["api_version"], serialization.API_VERSION)
        self.assertEqual(document["data"]["id"], DEMO_PLAN_ID)

    def test_provenance_and_identity_are_present(self) -> None:
        self.assertEqual(self.payload["id"], DEMO_PLAN_ID)
        self.assertEqual(self.payload["data_provenance"], "DEMO_SYNTHETIC")
        self.assertEqual(self.payload["timezone"], "Europe/Moscow")
        self.assertEqual(self.payload["route_mode"], "SMART_ROUTE")
        self.assertEqual(self.payload["window_end_policy"], "service_finish_before_end")
        self.assertEqual(self.payload["name"], None)

    def test_the_plan_has_its_stops_and_counts(self) -> None:
        counts = self.payload["counts"]
        self.assertEqual(counts["stops"], 32)
        self.assertEqual(counts["enabled_stops"], 31)
        self.assertEqual(counts["disabled_stops"], 1)
        self.assertEqual(len(self.payload["stops"]), 32)

    def test_no_recommendation_ever_appears_in_a_plan_payload(self) -> None:
        """A recommendation is not plan state (D4/D11/D32); no field may invent one."""
        text = json.dumps(self.payload)
        self.assertNotIn("recommended_stop_id", text)
        self.assertNotIn("recommendation", text)
        self.assertNotIn("recommended", text)

    # -- instants: UTC ISO-8601 with a trailing Z ------------------------- #
    def test_instants_are_utc_iso8601_with_a_trailing_z(self) -> None:
        self.assertRegex(self.payload["departure_time"], UTC_Z)
        self.assertEqual(self.payload["departure_time"], "2026-09-11T01:00:00Z")

    # -- durations: integer seconds --------------------------------------- #
    def test_durations_are_integer_seconds(self) -> None:
        self.assertEqual(self.payload["default_service_duration_sec"], 600)
        self.assertIsInstance(self.payload["default_service_duration_sec"], int)
        durations = [stop["service_duration_sec"] for stop in self.payload["stops"]]
        self.assertIn(240, durations)
        # Exactly one demo stop has no duration of its own; the plan default applies to it.
        self.assertIn(None, durations)
        for value in durations:
            if value is not None:
                self.assertIsInstance(value, int)
                self.assertNotIsInstance(value, bool)

    def test_no_duration_field_is_a_float_or_a_formatted_string(self) -> None:
        text = json.dumps(self.payload)
        self.assertNotIn("PT", text)
        for stop in self.payload["stops"]:
            value = stop["service_duration_sec"]
            self.assertNotIsInstance(value, float)

    # -- booleans are JSON booleans --------------------------------------- #
    def test_booleans_are_json_booleans_not_zero_or_one(self) -> None:
        enabled = [stop["enabled"] for stop in self.payload["stops"]]
        self.assertIn(True, enabled)
        self.assertIn(False, enabled)
        for value in enabled:
            self.assertIsInstance(value, bool)
        self.assertIsInstance(self.payload["cost_policy"]["provisional"], bool)
        self.assertIsInstance(self.payload["first_stop"]["pinned"], bool)

    # -- enums are strings ------------------------------------------------ #
    def test_enums_serialise_as_their_string_values(self) -> None:
        self.assertIsInstance(self.payload["route_mode"], str)
        self.assertIsInstance(self.payload["first_stop"]["mode"], str)
        self.assertIsInstance(self.payload["first_stop"]["state"], str)
        for stop in self.payload["stops"]:
            self.assertIsInstance(stop["geocode_status"], str)
            self.assertIsInstance(stop["service_status"], str)
            self.assertIn(stop["service_window"]["window_kind"], ("fixed", "unrestricted", "unknown"))

    # -- fingerprints are hex text ---------------------------------------- #
    def test_the_inputs_fingerprint_is_lowercase_hex(self) -> None:
        self.assertRegex(self.payload["inputs_fingerprint"], HEX_64)

    # -- stops carry the section 25 fields -------------------------------- #
    def test_every_stop_carries_input_position_status_and_controls(self) -> None:
        positions = [stop["input_position"] for stop in self.payload["stops"]]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(set(positions)), len(positions))
        for stop in self.payload["stops"]:
            for field in (
                "id",
                "input_position",
                "raw_address",
                "normalized_address",
                "latitude",
                "longitude",
                "geocode_status",
                "service_status",
                "enabled",
                "priority",
                "service_duration_sec",
                "service_window",
            ):
                self.assertIn(field, stop)

    def test_windows_are_local_hhmmss_text_with_the_stop_policy_override(self) -> None:
        windows = [stop["service_window"] for stop in self.payload["stops"]]
        kinds = {window["window_kind"] for window in windows}
        self.assertEqual(kinds, {"fixed", "unrestricted", "unknown"})
        fixed = [window for window in windows if window["window_kind"] == "fixed"]
        self.assertTrue(fixed)
        for window in fixed:
            self.assertRegex(window["start_local"], LOCAL_HHMMSS)
            self.assertRegex(window["end_local"], LOCAL_HHMMSS)
            # The demo fixture sets no per-stop override, so each fixed window inherits the plan.
            self.assertIsNone(window["window_end_policy"])
            self.assertEqual(
                window["effective_window_end_policy"], "service_finish_before_end"
            )
        for window in windows:
            if window["window_kind"] != "fixed":
                self.assertIsNone(window["start_local"])
                self.assertIsNone(window["end_local"])
                self.assertIsNone(window["window_end_policy"])

    def test_the_first_stop_block_is_the_driver_state_not_a_recommendation(self) -> None:
        block = self.payload["first_stop"]
        self.assertEqual(
            set(block),
            {"mode", "selected_stop_id", "selection_source", "pinned", "state", "description"},
        )
        self.assertEqual(block["mode"], "recommend")
        self.assertIsNone(block["selected_stop_id"])
        self.assertIsNone(block["selection_source"])
        self.assertFalse(block["pinned"])
        self.assertEqual(block["state"], "awaiting_first_stop_choice")

    def test_the_cost_policy_reports_the_capability_table(self) -> None:
        policy = self.payload["cost_policy"]
        self.assertEqual(policy["name"], "smart_route_elapsed_v1")
        self.assertEqual(policy["weights"], {"travel_time": 1.0, "waiting_time": 1.0})
        statuses = {entry["component"]: entry["status"] for entry in policy["components"]}
        self.assertEqual(statuses["travel_time"], "implemented")
        self.assertEqual(statuses["wrong_side_penalty"], "requires_provider")
        self.assertEqual(statuses["priority_penalty"], "planned")
        self.assertIn("wrong_side_penalty", policy["unimplemented_components"])

    def test_the_summary_payload_is_json_safe_and_small(self) -> None:
        summary = serialization.plan_summary_payload(self.record.plan, self.record.data_provenance)
        self.assertTrue(serialization.is_json_safe(summary))
        self.assertNotIn("stops", summary)
        self.assertEqual(summary["counts"]["enabled_stops"], 31)
        self.assertEqual(summary["first_stop"]["state"], "awaiting_first_stop_choice")
        document = serialization.plan_summary_document(
            self.record.plan, self.record.data_provenance
        )
        self.assertEqual(document["type"], serialization.PLAN_SUMMARY_TYPE)

    def test_settings_payload_is_json_safe_for_any_json_value(self) -> None:
        for value in (None, 19, 2.5, "text", {"a": [1, 2]}, [True, False]):
            with self.subTest(value=value):
                payload = serialization.settings_payload("tile_max_zoom", value, configured=True)
                self.assertTrue(serialization.is_json_safe(payload))
                self.assertTrue(payload["configured"])


class ErrorDocumentTests(unittest.TestCase):
    def test_the_error_envelope_has_exactly_the_documented_shape(self) -> None:
        document = serialization.error_document("unknown_plan", "NotFound", "no such plan")
        self.assertEqual(set(document), {"error"})
        self.assertEqual(set(document["error"]), {"code", "type", "message"})
        self.assertEqual(document["error"]["code"], "unknown_plan")
        self.assertEqual(document["error"]["type"], "NotFound")

    def test_every_documented_code_is_usable_and_has_a_status(self) -> None:
        self.assertIn("invalid_body", serialization.ERROR_CODES)
        for code, (status, meaning) in serialization.ERROR_CODES.items():
            with self.subTest(code=code):
                document = serialization.error_document(code, "SomeError", "message")
                self.assertEqual(document["error"]["code"], code)
                self.assertIsInstance(status, int)
                self.assertTrue(meaning)

    def test_an_undocumented_code_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            serialization.error_code("made_up_code")

    def test_the_documented_codes_cover_the_required_statuses(self) -> None:
        statuses = {status for status, _ in serialization.ERROR_CODES.values()}
        for status in (400, 404, 405, 409, 422, 500, 501, 503):
            self.assertIn(status, statuses)

    def test_the_engine_facing_codes_are_in_the_error_tables_not_only_docstrings(self) -> None:
        """The U14 codes live where the error table lives: the documented codes and the mapping."""
        from api.http_server import ERROR_STATUS_MAP
        from api.services import NoFirstStopSelected, PlanBusy, UnknownRun

        documented = {
            "unknown_run": (404, UnknownRun),
            "no_first_stop_selected": (409, NoFirstStopSelected),
            "plan_busy": (409, PlanBusy),
        }
        for code, (status, exception) in documented.items():
            with self.subTest(code=code):
                self.assertIn(code, serialization.ERROR_CODES)
                self.assertEqual(serialization.ERROR_CODES[code][0], status)
                self.assertTrue(serialization.ERROR_CODES[code][1])
                self.assertEqual(ERROR_STATUS_MAP[exception], (status, code))


if __name__ == "__main__":
    unittest.main()

