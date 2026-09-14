"""U14 HTTP: the recommendation endpoint and the anti-fabrication fidelity gate.

The server runs **in process** on an ephemeral loopback port bound to ``127.0.0.1`` and is driven
with :mod:`urllib.request` from the standard library - no browser and no external network. Each test
gets a **file-backed** SQLite database under the gitignored ``var/`` scratch tree
(``tests/api/support.py``), and ``tearDownModule`` removes that tree again, so the module leaves no
artifact behind.

The central assertion of this module is *fidelity*: the payload served over HTTP must match a
**direct engine call on the same plan** exactly - the same ranked order, the same recommended stop,
the same per-candidate complete-route durations, travel, waiting and service seconds, the same
rejected candidates with the same violating stops, and the same fingerprints. That is the
anti-fabrication gate: a payload the engine did not produce cannot pass it.

Cost: one exhaustive recommendation evaluates 31 complete routes (a few seconds), so the expected
engine report is computed **once per class** and the tests reuse it.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import unittest
import urllib.error
import urllib.request

from api import serialization
from api.http_server import API_JSON_CONTENT_TYPE, create_server
from api.services import MAX_RANKED_RECOMMENDATION_CANDIDATES, PLAN_LOCK_TIMEOUT_SECONDS
from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.model.first_stop import RecommendationStatus
from demo.dataset import DEMO_PLAN_ID, build_demo_plan
from demo.synthetic_matrix import demo_matrix
from tests.api.support import (
    ServerBackedTestCase,
    cleanup_scratch_root,
    new_database_file,
    new_scratch_directory,
)

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")

RECOMMENDATION_PATH = f"/api/plans/{DEMO_PLAN_ID}/recommendation"


def fetch_once(server, method: str, path: str):
    """One request against a server this module started itself (no ``self.base_url`` needed).

    Returns ``(status, content_type, body)``; an HTTP error is returned rather than raised, because
    the status code is part of what the class-level preparation asserts.
    """
    host, port = server.server_address[0], server.server_address[1]
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return (
                response.status,
                response.headers.get("Content-Type", ""),
                response.read().decode("utf-8"),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            error.headers.get("Content-Type", ""),
            error.read().decode("utf-8"),
        )


class DemoRecommendationTestCase(ServerBackedTestCase):
    """One direct engine report **and** one served payload for the whole class.

    The exhaustive loop costs seconds, so the class-level setup seeds the database once, runs the
    engine directly once (the expected answer) and serves the endpoint once (the answer under test);
    every test below asserts against those two objects. Each test still gets its own server and its
    own ``ApiServices`` over the **same** database file, so a test that changes state cannot leak
    into another test.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from api.services import ApiServices
        cls.class_scratch = new_scratch_directory("api-u14-recommendation")
        cls.class_database = new_database_file(cls.class_scratch)
        seeding = ApiServices(cls.class_database)
        try:
            seeding.plans.create_demo_plan({})
        finally:
            seeding.close()

        cls.engine_plan = build_demo_plan()
        cls.engine_report = evaluate_first_stop_candidates(
            plan=cls.engine_plan, travel_matrix=demo_matrix()
        )

        services = ApiServices(cls.class_database)
        server = create_server(
            services,
            host="127.0.0.1",
            port=0,
            quiet=True,
            static_root=cls.class_scratch / "no-web",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, content_type, body = fetch_once(server, "GET", RECOMMENDATION_PATH)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5.0)
            services.close()
        assert status == 200, body
        cls.served_content_type = content_type
        cls.served_document = json.loads(body)
        cls.served_payload = cls.served_document["data"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.class_scratch, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        self.create_demo_plan()


class RecommendationEndpointTests(DemoRecommendationTestCase):
    """``GET /api/plans/{id}/recommendation`` - live recompute, advisory, and writes nothing."""

    def test_the_served_recommendation_matches_a_direct_engine_call_exactly(self) -> None:
        data = self.served_payload
        report = self.engine_report

        # -- status, the recommended stop and the two fingerprints ------------------ #
        self.assertEqual(data["status"], report.status.value)
        self.assertEqual(data["status"], "recommended")
        self.assertEqual(data["recommended_stop_id"], report.recommended_stop_id)
        self.assertEqual(
            data["fingerprints"]["inputs_fingerprint"], report.inputs_fingerprint
        )

        # -- the counts: exhaustive, and every candidate is ranked or rejected ------ #
        counts = data["counts"]
        self.assertEqual(counts["candidates_evaluated"], report.candidates_evaluated)
        self.assertEqual(counts["ranked"], len(report.ranked))
        self.assertEqual(counts["rejected"], len(report.rejected))
        self.assertEqual(
            counts["candidates_evaluated"], counts["ranked"] + counts["rejected"]
        )
        self.assertEqual(counts["optimizer_runs"], len(self.engine_plan.active_stops()))

        # -- the ranked order, position by position --------------------------------- #
        self.assertEqual(
            [candidate["stop_id"] for candidate in data["ranked"]],
            [candidate.stop_id for candidate in report.ranked][
                :MAX_RANKED_RECOMMENDATION_CANDIDATES
            ],
        )
        self.assertEqual(counts["ranked_returned"], len(data["ranked"]))
        for position, (served, direct) in enumerate(
            zip(data["ranked"], report.ranked, strict=False), start=1
        ):
            with self.subTest(stop=direct.stop_id):
                self.assertEqual(served["rank"], position)
                self.assertEqual(served["stop_id"], direct.stop_id)
                complete = served["complete_route"]
                self.assertEqual(
                    complete["duration_sec"], direct.estimated_complete_route_duration
                )
                self.assertEqual(complete["travel_sec"], direct.complete_travel_time)
                self.assertEqual(complete["waiting_sec"], direct.complete_waiting_time)
                self.assertEqual(complete["service_sec"], direct.total_service_time)
                self.assertEqual(complete["max_lateness_sec"], direct.max_lateness)
                self.assertEqual(
                    complete["violating_stop_ids"],
                    [str(stop_id) for stop_id in direct.violating_stop_ids],
                )
                self.assertTrue(served["feasible"])
                first_leg = served["first_leg"]
                self.assertEqual(first_leg["travel_sec"], direct.travel_time)
                self.assertEqual(first_leg["waiting_sec"], direct.waiting_time)
                self.assertEqual(first_leg["lateness_sec"], direct.lateness)
                self.assertEqual(
                    first_leg["estimated_arrival"],
                    serialization.instant_text(direct.estimated_arrival),
                )
                self.assertEqual(
                    first_leg["estimated_service_start"],
                    serialization.instant_text(direct.estimated_service_start),
                )
                self.assertEqual(
                    complete["finish_arrival"],
                    serialization.instant_text(direct.estimated_finish),
                )
                self.assertEqual(served["objective"]["score"], direct.score)

        # -- the rejected candidates, with their violating stops and reasons -------- #
        self.assertEqual(
            [candidate["stop_id"] for candidate in data["rejected"]],
            [candidate.stop_id for candidate in report.rejected],
        )
        self.assertTrue(data["rejected"])
        for served, direct in zip(data["rejected"], report.rejected, strict=True):
            with self.subTest(stop=direct.stop_id):
                self.assertIsNone(served["rank"])
                self.assertFalse(served["feasible"])
                self.assertEqual(
                    served["complete_route"]["violating_stop_ids"],
                    [str(stop_id) for stop_id in direct.violating_stop_ids],
                )
        served_diagnostics = [
            (item["stop_id"], item["candidate_stop_id"], item["code"], item["reason"])
            for item in data["diagnostics"]
        ]
        self.assertEqual(
            served_diagnostics,
            [
                (
                    str(item.stop_id),
                    str(item.candidate_stop_id),
                    item.code,
                    item.reason,
                )
                for item in report.diagnostics
            ],
        )

        # -- the policy, the window-end policy and the envelope --------------------- #
        self.assertEqual(data["policy"]["name"], report.policy_name)
        self.assertEqual(data["policy"]["provisional"], report.policy_is_provisional)
        self.assertEqual(
            data["policy"]["window_end_policy"], report.window_end_policy.value
        )
        self.assertEqual(report.policy_name, "smart_route_elapsed_v1")
        self.assertFalse(data["policy"]["provisional"])
        self.assertEqual(
            self.served_document["type"], serialization.RECOMMENDATION_PAYLOAD_TYPE
        )
        self.assertIs(self.served_document["live_recompute"], True)
        self.assertEqual(self.served_document["computed_at"], "2026-09-11T01:00:00Z")

    def test_the_response_says_plainly_that_it_is_not_an_applied_decision(self) -> None:
        data = self.served_payload
        self.assertIs(data["advisory"], True)
        self.assertIs(data["applied_decision"], False)
        self.assertIs(data["as_plan_state"], False)
        self.assertEqual(data["note"], serialization.RECOMMENDATION_ADVISORY_NOTE)
        lowered = data["note"].lower()
        self.assertIn("recommendation", lowered)
        self.assertIn("not an applied decision", lowered)

    def test_the_response_reports_its_measured_computation_seconds(self) -> None:
        self.assertIsInstance(self.served_payload["computation_seconds"], int)
        self.assertGreaterEqual(self.served_payload["computation_seconds"], 0)

    def test_the_recommendation_payload_is_json_safe_and_durations_are_seconds(self) -> None:
        payload = self.served_document
        self.assertTrue(serialization.is_json_safe(payload))
        self.assertEqual(payload["api_version"], serialization.API_VERSION)
        self.assertEqual(self.served_content_type, API_JSON_CONTENT_TYPE)
        for candidate in payload["data"]["ranked"] + payload["data"]["rejected"]:
            for block in (candidate["complete_route"], candidate["first_leg"]):
                for key, value in block.items():
                    if key.endswith("_sec"):
                        self.assertIsInstance(value, int)
                        self.assertNotIsInstance(value, bool)
            for instant_key in ("estimated_arrival", "estimated_service_start"):
                self.assertRegex(candidate["first_leg"][instant_key], UTC_Z)
            self.assertRegex(candidate["complete_route"]["finish_arrival"], UTC_Z)
        self.assertRegex(payload["data"]["fingerprints"]["inputs_fingerprint"], HEX_64)

    def test_a_get_recommendation_appends_no_run_and_does_not_touch_the_plan(self) -> None:
        before = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]
        self.assertEqual(self.get(RECOMMENDATION_PATH).status, 200)
        after = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]
        self.assertEqual(after, before)
        self.assertEqual(after["first_stop"]["state"], "awaiting_first_stop_choice")
        self.assertIsNone(after["first_stop"]["selected_stop_id"])
        runs = self.get(f"/api/plans/{DEMO_PLAN_ID}/runs").json()
        self.assertEqual(runs["count"], 0)

    def test_the_plan_payload_still_carries_no_recommendation_field(self) -> None:
        """Reading a recommendation never turns it into plan state (D4/D11/D32, I5)."""
        self.get(RECOMMENDATION_PATH)
        text = self.get(f"/api/plans/{DEMO_PLAN_ID}").text
        self.assertNotIn("recommended_stop_id", text)
        self.assertNotIn("recommendation", text)

    def test_an_unknown_plan_is_a_404(self) -> None:
        response = self.get("/api/plans/nope/recommendation")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_plan")

    def test_a_wrong_method_is_a_405(self) -> None:
        response = self.post(RECOMMENDATION_PATH, body={})
        self.assertEqual(response.status, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")


class RecommendationBusyTests(ServerBackedTestCase):
    """The per-plan single-flight bound answers ``409 plan_busy`` - never a partial result."""

    def make_services(self, identifier: str):
        from api.services import ApiServices

        return ApiServices(identifier, plan_lock_timeout_seconds=0.2)

    def setUp(self) -> None:
        super().setUp()
        self.create_demo_plan()

    def test_a_busy_plan_is_refused_with_the_documented_409(self) -> None:
        lock = self.services.recommendations.plan_lock(DEMO_PLAN_ID)
        self.assertTrue(lock.acquire(0))
        try:
            response = self.get(RECOMMENDATION_PATH)
        finally:
            lock.release()
        self.assertEqual(response.status, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "plan_busy")
        self.assertIn("no background job queue", error["message"])
        self.assertIn("0.2", error["message"])

    def test_a_busy_plan_is_refused_on_the_route_endpoint_too(self) -> None:
        self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", "S01-NEAR")
        lock = self.services.routes.plan_lock(DEMO_PLAN_ID)
        self.assertTrue(lock.acquire(0))
        try:
            response = self.get(f"/api/plans/{DEMO_PLAN_ID}/route")
        finally:
            lock.release()
        self.assertEqual(response.status, 409)
        self.assertEqual(response.json()["error"]["code"], "plan_busy")

    def test_a_refused_computation_writes_nothing(self) -> None:
        lock = self.services.routes.plan_lock(DEMO_PLAN_ID)
        self.assertTrue(lock.acquire(0))
        try:
            self.assertEqual(
                self.post(f"/api/plans/{DEMO_PLAN_ID}/optimize", body={}).status, 409
            )
        finally:
            lock.release()
        self.assertEqual(self.get(f"/api/plans/{DEMO_PLAN_ID}/runs").json()["count"], 0)

    def test_the_health_payload_reports_the_documented_bound(self) -> None:
        payload = self.get("/api/health").json()
        computation = payload["computation"]
        self.assertIs(computation["synchronous"], True)
        self.assertIs(computation["background_job_queue"], False)
        self.assertIs(computation["per_plan_single_flight"], True)
        self.assertEqual(computation["lock_wait_bound_seconds"], 0.2)
        self.assertEqual(PLAN_LOCK_TIMEOUT_SECONDS, 30.0)
        self.assertIn("accepted_mvp_latency", computation)
        self.assertIn("8 seconds", computation["accepted_mvp_latency"])
        self.assertIn("D36", computation["accepted_mvp_latency"])

    def test_the_lock_is_released_so_a_later_request_succeeds(self) -> None:
        self.assertEqual(self.get(RECOMMENDATION_PATH).status, 200)
        self.assertEqual(self.get(RECOMMENDATION_PATH).status, 200)


class NoFullyFeasibleRouteOverHttpTests(ServerBackedTestCase):
    """``no_fully_feasible_route`` is a VALID ``200`` with diagnostics (v2 section 14).

    The fixture is a **constructed** plan: the demo plan with every enabled stop closing before the
    driver departs. It is served through the same repository the API always uses (the SQLite plan
    repository, seeded directly by the test), so the whole HTTP path is exercised and the engine -
    not the test - produces the outcome.
    """

    def setUp(self) -> None:
        super().setUp()
        from tests.api.support import build_infeasible_demo_plan

        plan, self.window_stop_id = build_infeasible_demo_plan(plan_id="demo-unreachable")
        with self.services.state.connection() as connection:
            self.services.state.plan_repository(connection).save(plan)
        self.plan_id = plan.id
        self.active_count = len(plan.active_stops())

    def test_every_candidate_infeasible_is_a_200_with_diagnostics_and_no_winner(self) -> None:
        response = self.get(f"/api/plans/{self.plan_id}/recommendation")
        self.assertEqual(response.status, 200, msg=response.text)
        data = response.json()["data"]
        self.assertEqual(data["status"], "no_fully_feasible_route")
        self.assertEqual(data["status"], RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE.value)
        self.assertIsNone(data["recommended_stop_id"])
        self.assertEqual(data["ranked"], [])
        self.assertEqual(data["counts"]["ranked"], 0)
        self.assertEqual(data["counts"]["rejected"], self.active_count)
        self.assertEqual(data["counts"]["candidates_evaluated"], self.active_count)
        self.assertTrue(data["rejected"])
        self.assertTrue(data["diagnostics"])
        self.assertIn(
            self.window_stop_id, data["rejected"][0]["complete_route"]["violating_stop_ids"]
        )
        for diagnostic in data["diagnostics"]:
            self.assertEqual(diagnostic["code"], "time_window_infeasible")
            self.assertTrue(diagnostic["reason"])
        self.assertIs(data["as_plan_state"], False)

    def test_the_route_endpoint_reports_the_missing_selection_not_a_route(self) -> None:
        response = self.get(f"/api/plans/{self.plan_id}/route")
        self.assertEqual(response.status, 409)
        self.assertEqual(response.json()["error"]["code"], "no_first_stop_selected")


if __name__ == "__main__":
    unittest.main()