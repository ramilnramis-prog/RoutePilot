"""End-to-end transport behaviour: routes, status codes, the error envelope and static assets.

The server runs **in process** on an ephemeral port bound to ``127.0.0.1`` and is driven with
:mod:`urllib.request` from the standard library - no browser, no external network. Each test gets a
**file-backed** SQLite database under the gitignored ``var/`` scratch tree (see
:mod:`tests.api.support`; :func:`storage.sqlite.database.connect` is not called with ``uri=True``, so
a shared-cache memory URI is not usable here). ``tearDownModule`` below runs
:func:`cleanup_scratch_root`, which removes that whole scratch tree, so this module leaves no
artifact behind (Stage 4 U13).
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from api.http_server import API_JSON_CONTENT_TYPE
from tests.api.support import ServerBackedTestCase, cleanup_scratch_root

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root


class ServerLifecycleTests(ServerBackedTestCase):
    def test_the_server_binds_loopback_on_an_ephemeral_port(self) -> None:
        host, port = self.server.server_address[0], self.server.server_address[1]
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)

    def test_json_errors_and_payloads_share_the_utf8_content_type(self) -> None:
        response = self.get("/api/health")
        self.assertEqual(response.content_type, API_JSON_CONTENT_TYPE)

    def test_the_schema_is_current_at_server_start(self) -> None:
        from storage.sqlite.database import SCHEMA_VERSION

        self.assertEqual(self.services.state.schema_version, SCHEMA_VERSION)

    def test_every_request_gets_its_own_connection_with_foreign_keys_on(self) -> None:
        connection = self.services.state.connect()
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertIsNot(connection, self.services.state.connect())


class HealthTests(ServerBackedTestCase):
    def test_health_reports_the_timezone_database_state_honestly(self) -> None:
        payload = self.get("/api/health").json()
        self.assertEqual(payload["status"], "ok")
        tz = payload["timezone_data"]
        self.assertIn(tz["source"], ("package", "system", "missing"))
        self.assertEqual(tz["available"], tz["source"] != "missing")
        self.assertIn("install_command", tz)
        self.assertIn("tzdata", tz["install_command"])
        if tz["source"] == "missing":
            self.assertIn("fix", tz)
            self.assertIsNone(tz["iana_version"])

    def test_health_labels_the_demo_data_as_demo_synthetic(self) -> None:
        payload = self.get("/api/health").json()
        self.assertEqual(payload["data_provenance"], "DEMO_SYNTHETIC")
        self.assertEqual(payload["demo_data"]["provenance"], "DEMO_SYNTHETIC")
        self.assertEqual(payload["demo_data"]["labelled"], ["DEMO", "SYNTHETIC"])
        self.assertIn("SYNTHETIC", payload["demo_data"]["warning"])

    def test_health_lists_implemented_capabilities(self) -> None:
        payload = self.get("/api/health").json()
        implemented = {entry["capability"] for entry in payload["implemented_capabilities"]}
        self.assertIn("engine", implemented)
        self.assertIn("route_mode:SMART_ROUTE", implemented)
        self.assertIn("cost_component:travel_time", implemented)
        self.assertIn("cost_component:waiting_time", implemented)

    def test_health_lists_the_not_implemented_capabilities_the_ui_needs(self) -> None:
        payload = self.get("/api/health").json()
        entries = payload["not_implemented_capabilities"]
        capabilities = {entry["capability"]: entry for entry in entries}
        for required in (
            "provider:traffic",
            "provider:side_of_road",
            "provider:turn_by_turn",
            "provider:geocoding",
            "provider:real_routing",
        ):
            self.assertIn(required, capabilities)
        for entry in entries:
            self.assertIn(entry["status"], ("planned", "requires_provider", "unsupported"))
            self.assertTrue(entry["detail"])
        self.assertEqual(capabilities["provider:traffic"]["status"], "requires_provider")
        self.assertEqual(capabilities["provider:side_of_road"]["status"], "requires_provider")

    def test_health_reports_every_route_mode_except_smart_route_as_unimplemented(self) -> None:
        payload = self.get("/api/health").json()
        modes = payload["route_modes"]
        self.assertEqual(modes["implemented"], ["SMART_ROUTE"])
        self.assertEqual(
            set(modes["not_implemented"]),
            {"FASTEST", "SHORTEST", "MINIMUM_TURNS", "ON_THE_WAY", "START_TO_FINISH"},
        )
        listed = {
            entry["capability"]
            for entry in payload["not_implemented_capabilities"]
            if entry["capability"].startswith("route_mode:")
        }
        self.assertEqual(
            listed,
            {
                "route_mode:FASTEST",
                "route_mode:SHORTEST",
                "route_mode:MINIMUM_TURNS",
                "route_mode:ON_THE_WAY",
                "route_mode:START_TO_FINISH",
            },
        )

    def test_health_names_the_database_and_the_schema_version(self) -> None:
        payload = self.get("/api/health").json()
        database = payload["database"]
        self.assertEqual(database["schema_version"], database["schema_target_version"])
        self.assertIn("SqliteRoutePlanRepository", database["implementation"])
        # The identifier is reported as configured (a path in this case), never rewritten.
        self.assertEqual(database["identifier"], self.services.state.display_identifier)
        self.assertTrue(database["identifier"])

    def test_health_states_which_units_are_implemented(self) -> None:
        payload = self.get("/api/health").json()
        self.assertIn("U13", payload["implemented_units"])
        self.assertIn("U14", payload["implemented_units"])
        self.assertIn("U15", payload["implemented_units"])

    def test_health_is_json_safe(self) -> None:
        from api.serialization import is_json_safe

        self.assertTrue(is_json_safe(self.get("/api/health").json()))


class PlanWorkflowTests(ServerBackedTestCase):
    def test_an_empty_database_lists_no_plans(self) -> None:
        response = self.get("/api/plans")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["data"], [])

    def test_create_then_list_and_get_the_demo_plan(self) -> None:
        created = self.create_demo_plan()
        self.assertEqual(created["id"], "demo-route-01")
        self.assertEqual(created["data_provenance"], "DEMO_SYNTHETIC")
        self.assertEqual(created["counts"]["enabled_stops"], 31)

        listed = self.get("/api/plans").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["data"][0]["id"], "demo-route-01")

        fetched = self.get("/api/plans/demo-route-01").json()
        self.assertEqual(fetched["data"]["id"], "demo-route-01")
        self.assertEqual(fetched["data"]["stops"], created["stops"])
        self.assertEqual(
            fetched["data"]["first_stop"]["state"], "awaiting_first_stop_choice"
        )
        self.assertEqual(
            fetched["data"]["data_provenance"], "DEMO_SYNTHETIC"
        )

    def test_the_plan_payload_carries_no_recommendation_field(self) -> None:
        """No recommendation is plan state (D4/D11/D32): the payload must not carry one.

        The only permitted occurrence of the word is the first-stop **mode** value ``"recommend"``,
        which describes *who decides*, not what the engine suggested.
        """
        self.create_demo_plan()
        payload = self.get("/api/plans/demo-route-01").json()["data"]
        self.assertEqual(payload["first_stop"]["mode"], "recommend")
        self.assertNotIn("recommended_stop_id", json.dumps(payload))
        # Dropping the mode value and its description leaves no other mention of a recommendation.
        text = json.dumps(payload).replace('"recommend"', '""')
        text = text.replace('"recommend: awaiting first stop choice"', '""').lower()
        self.assertNotIn("recommend", text)

    def test_creating_the_demo_plan_twice_is_idempotent(self) -> None:
        first = self.post("/api/plans", body={})
        second = self.post("/api/plans", body={})
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 201)
        self.assertEqual(first.json()["data"]["id"], second.json()["data"]["id"])
        self.assertEqual(self.get("/api/plans").json()["count"], 1)

    def test_editing_enabled_and_priority_persists(self) -> None:
        self.create_demo_plan()
        response = self.put(
            "/api/plans/demo-route-01",
            body={
                "stops": [
                    {"stop_id": "S01-NEAR", "enabled": False},
                    {"stop_id": "S02-NEAR2", "priority": 7},
                ]
            },
        )
        self.assertEqual(response.status, 200, msg=response.text)
        payload = response.json()["data"]
        by_id = {stop["id"]: stop for stop in payload["stops"]}
        self.assertFalse(by_id["S01-NEAR"]["enabled"])
        self.assertEqual(by_id["S02-NEAR2"]["priority"], 7)
        self.assertEqual(payload["counts"]["enabled_stops"], 30)
        self.assertEqual(payload["counts"]["disabled_stops"], 2)

        reloaded = self.get("/api/plans/demo-route-01").json()["data"]
        reloaded_by_id = {stop["id"]: stop for stop in reloaded["stops"]}
        self.assertFalse(reloaded_by_id["S01-NEAR"]["enabled"])
        self.assertEqual(reloaded_by_id["S02-NEAR2"]["priority"], 7)
        positions = [stop["input_position"] for stop in reloaded["stops"]]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(reloaded["first_stop"]["state"], "awaiting_first_stop_choice")

    def test_restoring_a_stop_and_clearing_a_priority(self) -> None:
        self.create_demo_plan()
        self.put(
            "/api/plans/demo-route-01",
            body={"stops": [{"stop_id": "S10-DISABLED", "enabled": True, "priority": 3}]},
        )
        payload = self.get("/api/plans/demo-route-01").json()["data"]
        by_id = {stop["id"]: stop for stop in payload["stops"]}
        self.assertTrue(by_id["S10-DISABLED"]["enabled"])
        self.assertEqual(by_id["S10-DISABLED"]["priority"], 3)
        self.assertEqual(payload["counts"]["enabled_stops"], 32)

        self.put(
            "/api/plans/demo-route-01",
            body={"stops": [{"stop_id": "S10-DISABLED", "enabled": False, "priority": None}]},
        )
        payload = self.get("/api/plans/demo-route-01").json()["data"]
        by_id = {stop["id"]: stop for stop in payload["stops"]}
        self.assertFalse(by_id["S10-DISABLED"]["enabled"])
        self.assertIsNone(by_id["S10-DISABLED"]["priority"])

    def test_editing_an_unknown_stop_is_a_404_json_error(self) -> None:
        self.create_demo_plan()
        response = self.put(
            "/api/plans/demo-route-01", body={"stops": [{"stop_id": "NOPE", "enabled": False}]}
        )
        self.assertEqual(response.status, 404)
        error = response.json()["error"]
        self.assertEqual(error["code"], "unknown_stop")
        self.assertIn("NOPE", error["message"])


class SettingsRoundTripTests(ServerBackedTestCase):
    def test_an_unset_key_is_a_404_json_error(self) -> None:
        response = self.get("/api/settings/tile_url")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_setting")

    def test_the_tile_keys_round_trip(self) -> None:
        values = {
            "tile_url": "https://tile.example/{z}/{x}/{y}.png",
            "tile_attribution": "(c) demo tile provider",
            "tile_max_zoom": 19,
        }
        for key, value in values.items():
            with self.subTest(key=key):
                stored = self.put(f"/api/settings/{key}", body={"value": value})
                self.assertEqual(stored.status, 200, msg=stored.text)
                self.assertEqual(stored.json()["data"]["value"], value)
                self.assertTrue(stored.json()["data"]["configured"])
                fetched = self.get(f"/api/settings/{key}")
                self.assertEqual(fetched.status, 200)
                self.assertEqual(fetched.json()["data"]["value"], value)

    def test_a_value_is_replaced_not_merged(self) -> None:
        self.put("/api/settings/tile_max_zoom", body={"value": 12})
        self.put("/api/settings/tile_max_zoom", body={"value": 18})
        self.assertEqual(self.get("/api/settings/tile_max_zoom").json()["data"]["value"], 18)

    def test_structured_values_are_stored_as_given(self) -> None:
        value = {"mode": "dev", "zones": []}
        self.put("/api/settings/doctor_mode", body={"value": value})
        self.assertEqual(self.get("/api/settings/doctor_mode").json()["data"]["value"], value)

    def test_a_stored_json_null_reads_back_as_unset(self) -> None:
        """A documented boundary of the settings port, reported honestly rather than papered over.

        ``AppSettingsRepository.get`` returns ``None`` for an absent key *and* for a stored JSON
        ``null``, so the API cannot tell them apart and answers 404 for both. The ``value`` field
        itself must still be present explicitly, because "store null" and "forgot the field" are
        different requests.
        """
        missing = self.put("/api/settings/doctor_mode", body={})
        self.assertEqual(missing.status, 422)
        stored = self.put("/api/settings/doctor_mode", body={"value": None})
        self.assertEqual(stored.status, 200, msg=stored.text)
        self.assertIsNone(stored.json()["data"]["value"])
        self.assertEqual(self.get("/api/settings/doctor_mode").status, 404)

    def test_a_body_without_a_value_is_a_422(self) -> None:
        response = self.put("/api/settings/tile_url", body={})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")


class ErrorMappingTests(ServerBackedTestCase):
    """The exact status code and error envelope for every refused request."""

    def assert_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status, status, msg=response.text)
        payload = response.json()
        self.assertEqual(set(payload), {"error"})
        error = payload["error"]
        self.assertEqual(set(error), {"code", "type", "message"})
        self.assertEqual(error["code"], code)
        self.assertTrue(error["type"])
        self.assertTrue(error["message"])
        return error

    # -- 400: the body is not a JSON object ------------------------------ #
    def test_a_malformed_json_body_is_a_400(self) -> None:
        response = self.post("/api/plans", raw_body=b"{not json", content_type="application/json")
        error = self.assert_error(response, 400, "invalid_body")
        self.assertIn("not valid JSON", error["message"])

    def test_a_json_body_that_is_not_an_object_is_a_400(self) -> None:
        for raw in (b'["a"]', b'"text"', b"42", b"null", b"true"):
            with self.subTest(raw=raw):
                response = self.post(
                    "/api/plans", raw_body=raw, content_type="application/json"
                )
                self.assert_error(response, 400, "invalid_body")

    def test_a_non_utf8_body_is_a_400(self) -> None:
        response = self.post(
            "/api/plans", raw_body=b"\xff\xfe{}", content_type="application/json"
        )
        self.assert_error(response, 400, "invalid_body")

    # -- 404 --------------------------------------------------------------- #
    def test_an_unknown_path_is_a_404_json_error(self) -> None:
        for path in ("/api/nope", "/api/plans/demo-route-01/bogus", "/nothing-here"):
            with self.subTest(path=path):
                self.assert_error(self.get(path), 404, "unknown_path")

    def test_an_unknown_plan_id_is_a_404(self) -> None:
        error = self.assert_error(self.get("/api/plans/nope"), 404, "unknown_plan")
        self.assertIn("nope", error["message"])

    def test_editing_an_unknown_plan_is_a_404(self) -> None:
        response = self.put(
            "/api/plans/nope", body={"stops": [{"stop_id": "S01-NEAR", "enabled": False}]}
        )
        self.assert_error(response, 404, "unknown_plan")

    def test_an_unknown_settings_key_is_a_404(self) -> None:
        self.assert_error(self.get("/api/settings/nope"), 404, "unknown_setting")

    # -- 405: wrong method for a known path ------------------------------- #
    def test_a_wrong_method_is_a_405_json_error(self) -> None:
        cases = (
            ("POST", "/api/health"),
            ("DELETE", "/api/health"),
            ("PUT", "/api/plans"),
            ("DELETE", "/api/plans/demo-route-01"),
            ("PATCH", "/api/plans/demo-route-01"),
            ("POST", "/api/settings/tile_url"),
            ("DELETE", "/api/settings/tile_url"),
        )
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response = self.request(method, path)
                error = self.assert_error(response, 405, "method_not_allowed")
                self.assertIn("allowed method(s)", error["message"])

    # -- an early error response survives the close ------------------------ #
    def test_an_early_error_response_survives_an_unread_request_body(self) -> None:
        """The transport never loses an error to the connection close (D26).

        Every early refusal - the ``501`` of a declared-but-unimplemented path, the ``405`` of a
        wrong method, the ``404`` of an unknown path and the static-asset ``405`` - is answered
        before the request body would be read. With ``HTTP/1.0`` the response closes the connection,
        and a socket closed while the client's declared body is still unread is *reset*
        (``WSAECONNABORTED`` / ``10053`` on Windows) instead of shut down cleanly, so the client can
        lose a response the server already sent. The transport now drains the declared body before
        it answers.

        This drives the transport with a raw socket rather than ``urllib`` so both interleavings are
        deterministic: the body sent together with the headers (what ``urllib`` and ``curl`` do),
        and the body sent while the refusal is being prepared. The client then reads the refusal
        whole and keeps reading until the server closes it: a clean shutdown ends with zero bytes,
        while a reset raises - which is the regression this test pins.
        """
        import socket
        import time
        from urllib.parse import urlsplit

        self.create_demo_plan()
        cases = (
            ("POST", "/api/plans/demo-route-01/reoptimize", 501, "unsupported_capability"),
            ("GET", "/api/plans/demo-route-01/optimize", 405, "method_not_allowed"),
            ("POST", "/api/plans/demo-route-01/route", 405, "method_not_allowed"),
            ("POST", "/api/plans/nope/bogus", 404, "unknown_path"),
            ("POST", "/not-a-static-path.js", 405, "method_not_allowed"),
        )
        split = urlsplit(self.base_url)
        body = json.dumps({"ignored": True}).encode("utf-8")
        for body_first in (True, False):
            for index, (method, path, status, code) in enumerate(cases):
                with self.subTest(body_first=body_first, case=f"{index}:{method} {path}"):
                    request = (
                        f"{method} {path} HTTP/1.0\r\n"
                        "Host: 127.0.0.1\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "\r\n"
                    ).encode("ascii")
                    with socket.create_connection(
                        (split.hostname, split.port), timeout=30
                    ) as sock:
                        sock.settimeout(30)
                        sock.sendall(request + body if body_first else request)
                        if not body_first:
                            time.sleep(0.3)  # the refusal is being prepared; the body is unread
                            sock.sendall(body)
                        received = bytearray()
                        while b"\r\n\r\n" not in received:
                            chunk = sock.recv(4096)
                            self.assertTrue(
                                chunk, msg=f"the server closed without answering {path}"
                            )
                            received.extend(chunk)
                        head, _, rest = bytes(received).partition(b"\r\n\r\n")
                        declared = int(
                            dict(
                                line.split(": ", 1)
                                for line in head.decode("iso-8859-1").split("\r\n")[1:]
                            )["Content-Length"]
                        )
                        self.assertTrue(
                            head.startswith(f"HTTP/1.0 {status} ".encode("ascii")),
                            msg=head.decode("iso-8859-1"),
                        )
                        while len(rest) < declared:
                            chunk = sock.recv(4096)
                            self.assertTrue(
                                chunk, msg=f"the response to {path} was truncated"
                            )
                            rest += chunk
                        # A clean shutdown ends with zero bytes; a reset raises, losing the error.
                        self.assertEqual(sock.recv(4096), b"", msg=f"the close after {path} reset")
                    document = json.loads(rest.decode("utf-8"))
                    self.assertEqual(document["error"]["code"], code)

    # -- 409 --------------------------------------------------------------- #
    def test_a_stop_change_that_carries_nothing_is_a_409(self) -> None:
        self.create_demo_plan()
        response = self.put(
            "/api/plans/demo-route-01", body={"stops": [{"stop_id": "S01-NEAR"}]}
        )
        self.assert_error(response, 409, "illegal_state")

    # -- 422: input validation and domain shape errors --------------------- #
    def test_malformed_edit_requests_are_422(self) -> None:
        self.create_demo_plan()
        cases = (
            {"stops": []},
            {"stops": [{"enabled": True}]},
            {"stops": [{"stop_id": "S01-NEAR", "enabled": "yes"}]},
            {"stops": [{"stop_id": "S01-NEAR", "enabled": 0}]},
            {"stops": [{"stop_id": "S01-NEAR", "priority": -1}]},
            {"stops": [{"stop_id": "S01-NEAR", "priority": 1.5}]},
            {"stops": [{"stop_id": "S01-NEAR", "enabled": True, "position": 3}]},
            {"stops": [{"stop_id": "S01-NEAR", "enabled": True, "order": 3}]},
            {"stops": "not-a-list"},
            {"first_stop": "S01-NEAR"},
            {"route_mode": "FASTEST"},
        )
        for body in cases:
            with self.subTest(body=body):
                response = self.put("/api/plans/demo-route-01", body=body)
                self.assert_error(response, 422, "invalid_input")

    def test_an_edit_body_that_is_not_an_object_is_a_400(self) -> None:
        self.create_demo_plan()
        response = self.put(
            "/api/plans/demo-route-01", raw_body=b"[]", content_type="application/json"
        )
        self.assert_error(response, 400, "invalid_body")

    # -- 501: declared but unimplemented capabilities ---------------------- #
    def test_a_route_mode_other_than_smart_route_is_a_501(self) -> None:
        response = self.post("/api/plans", body={"route_mode": "FASTEST"})
        error = self.assert_error(response, 501, "unsupported_capability")
        self.assertIn("FASTEST", error["message"])
        self.assertIn("SMART_ROUTE", error["message"])

    def test_asking_for_geocoding_or_real_routing_is_a_501(self) -> None:
        cases = (
            {"geocode": True},
            {"addresses": ["Pushkina 1"]},
            {"traffic": True},
            {"side_of_road": True},
            {"turn_by_turn": True},
            {"routing": {"provider": "google"}},
            {"provider": "google"},
            {"optimize": True},
            {"matrix": {"source": "here"}},
        )
        for body in cases:
            with self.subTest(body=body):
                response = self.post("/api/plans", body=body)
                error = self.assert_error(response, 501, "unsupported_capability")
                message = error["message"].lower()
                self.assertTrue(
                    "not implemented" in message
                    or "no external provider is implemented" in message,
                    msg=error["message"],
                )
                self.assertIn("implemented", message)

    def test_an_unknown_request_field_is_a_422(self) -> None:
        response = self.post("/api/plans", body={"nonsense": 1})
        self.assert_error(response, 422, "invalid_input")

    def test_the_u14_endpoints_are_implemented_not_501(self) -> None:
        """U13 declared these paths as later-unit 501s; U14 implements them (docs/DECISIONS.md D39).

        The recommendation is a live ``200``, a selection body is validated (``422`` here), a
        cancellation with nothing selected is the documented ``409``, the route without a selection
        is ``409 no_first_stop_selected`` and the history is an empty read-only ``200``.
        """
        self.create_demo_plan()
        cases = (
            ("GET", "/api/plans/demo-route-01/recommendation", 200, None),
            ("POST", "/api/plans/demo-route-01/selection", 422, "invalid_input"),
            ("DELETE", "/api/plans/demo-route-01/selection", 409, "illegal_state"),
            (
                "GET",
                "/api/plans/demo-route-01/route",
                409,
                "no_first_stop_selected",
            ),
            ("GET", "/api/plans/demo-route-01/runs", 200, None),
        )
        for method, path, status, code in cases:
            with self.subTest(method=method, path=path):
                response = self.request(method, path, body={} if method == "POST" else None)
                self.assertEqual(response.status, status, msg=response.text)
                if code is not None:
                    self.assertEqual(response.json()["error"]["code"], code)

    def test_a_later_stage_endpoint_is_still_a_501(self) -> None:
        """Reoptimization after a served stop is a later stage, and it is refused honestly."""
        self.create_demo_plan()
        response = self.post("/api/plans/demo-route-01/reoptimize", body={})
        error = self.assert_error(response, 501, "unsupported_capability")
        self.assertIn("not implemented", error["message"])

    def test_a_wrong_method_on_a_u14_path_is_a_405(self) -> None:
        response = self.put("/api/plans/demo-route-01/recommendation", body={})
        self.assert_error(response, 405, "method_not_allowed")

    # -- no domain error becomes a silent 2xx ------------------------------ #
    def test_no_refused_request_returns_a_success_status(self) -> None:
        bodies = (
            {"stops": []},
            {"stops": [{"stop_id": "S01-NEAR", "priority": -5}]},
            {"stops": [{"stop_id": "NOPE", "enabled": True}]},
        )
        self.create_demo_plan()
        for body in bodies:
            with self.subTest(body=body):
                self.assertGreaterEqual(self.put("/api/plans/demo-route-01", body=body).status, 400)


class StaticAssetTests(ServerBackedTestCase):
    def test_static_requests_are_404_json_errors_when_web_does_not_exist(self) -> None:
        for path in ("/", "/index.html", "/app.js"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.content_type, API_JSON_CONTENT_TYPE)
                self.assertEqual(response.json()["error"]["code"], "unknown_path")
                # The message names the static root the server was started with, so a missing
                # workspace is diagnosable instead of just "not found" (U13 behaviour; the U15
                # workspace now lives in web/ and is found when the default root is used).
                self.assertIn("not available under", response.json()["error"]["message"])
    def test_a_traversal_attempt_is_refused_with_a_json_404(self) -> None:
        for path in (
            "/../secret.html",
            "/%2e%2e/secret.html",
            "/a/../../b.js",
            "/C:/Windows/app.js",
            "/app.db",
        ):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.json()["error"]["code"], "unknown_path")

    def test_a_non_get_method_on_a_static_path_is_a_405(self) -> None:
        response = self.post("/app.js", body={})
        self.assertEqual(response.status, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")


class StaticServingTests(ServerBackedTestCase):
    """With a real (temporary) static root, ``web/`` serving works with no code change."""

    @classmethod
    def setUpClass(cls) -> None:
        # Built inside the repository on purpose: the sandboxed environment does not reliably
        # create directories under %TEMP%, and this scratch tree is deleted again in tearDownClass.
        cls.directory = Path(__file__).resolve().parent / "_static_scratch"
        cls.static_root = cls.directory / "web"
        cls.static_root.mkdir(parents=True, exist_ok=True)
        (cls.static_root / "index.html").write_text(
            "<!doctype html><title>RoutePilot demo UI</title>", encoding="utf-8"
        )
        (cls.static_root / "app.css").write_text("body { margin: 0; }", encoding="utf-8")
        (cls.static_root / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
        (cls.static_root / "notes.txt").write_text("not served", encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)
    def missing_static_root(self) -> Path:
        """This case serves from a real (temporary) static root instead of a missing one."""
        return self.static_root

    def test_html_css_and_javascript_are_served_with_their_content_type(self) -> None:
        cases = {
            "/index.html": ("text/html", "<!doctype html>"),
            "/app.css": ("text/css", "body"),
            "/app.js": ("application/javascript", "export const x"),
        }
        for path, (content_type, needle) in cases.items():
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 200, msg=response.text)
                self.assertEqual(response.content_type, content_type)
                self.assertIn(needle, response.text)
                self.assertEqual(response.headers.get("cache-control"), "no-store")

    def test_an_existing_but_unlisted_asset_is_not_served(self) -> None:
        response = self.get("/notes.txt")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_path")

    def test_a_missing_asset_under_a_real_root_is_a_404(self) -> None:
        response = self.get("/missing.js")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_path")

    def test_the_api_and_the_static_root_do_not_shadow_each_other(self) -> None:
        self.assertEqual(self.get("/api/health").json()["status"], "ok")


class ConcurrencyTests(ServerBackedTestCase):
    def test_two_requests_in_parallel_each_get_their_own_connection(self) -> None:
        """A sqlite3 connection is not thread-safe, so the transport opens one per request."""
        import http.client

        self.create_demo_plan()
        results: list[int] = []
        errors: list[BaseException] = []

        def fetch() -> None:
            try:
                host, port = self.server.server_address[0], self.server.server_address[1]
                connection = http.client.HTTPConnection(host, port, timeout=10)
                connection.request("GET", "/api/plans")
                response = connection.getresponse()
                response.read()
                results.append(response.status)
                connection.close()
            except BaseException as error:  # noqa: BLE001 - recorded, then asserted
                errors.append(error)

        import threading

        threads = [threading.Thread(target=fetch) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 6)

    def test_health_payload_matches_the_service_layer_without_transport_arithmetic(self) -> None:
        """The transport returns what the service produced; it adds no numbers of its own."""
        from api.services import health_payload

        served = self.get("/api/health").json()
        expected = health_payload(self.services.state)
        served.pop("database")
        expected.pop("database")
        self.assertEqual(served, expected)

    def test_json_encoding_is_stable_across_requests(self) -> None:
        first = self.get("/api/health").body
        second = self.get("/api/health").body
        self.assertEqual(json.loads(first.decode("utf-8")), json.loads(second.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()

