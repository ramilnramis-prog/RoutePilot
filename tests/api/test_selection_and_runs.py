"""U14 HTTP: the driver-decision state machine, the committed route and the run history.

The server runs **in process** on an ephemeral loopback port bound to ``127.0.0.1`` and is driven
with :mod:`urllib.request` from the standard library - no browser and no external network. Each test
gets a **file-backed** SQLite database under the gitignored ``var/`` scratch tree
(``tests/api/support.py``) and ``tearDownModule`` removes that tree again, so the module leaves no
artifact behind.

Two contracts are pinned here beyond the state machine:

* **engine fidelity of the route**: the route payload served over HTTP must match a **direct
  optimizer run** on the same plan and the same selected first stop exactly - the same order, the
  same per-stop timeline rows, the same metrics with both baselines and the same violations. That is
  the anti-fabrication gate for the route;
* **REST discipline (owner decision 5)**: a ``GET`` never appends a run; only ``POST
  /api/plans/{id}/optimize`` does, exactly one row per request.

Cost: the exhaustive recommendation and the optimizer each take a few seconds on the 31-enabled-stop
demo plan, so the shared plan, its selection and its first optimize run are prepared **once per
class** and the tests reuse them.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace

from api import serialization
from api.http_server import create_server
from api.services import MAX_RANKED_RECOMMENDATION_CANDIDATES
from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.engine.optimizer.solve import solve_route
from core.model.first_stop import FirstStopIntent, FirstStopMode, SelectionSource
from core.model.route_plan import RoutePlan
from demo.dataset import DEMO_PLAN_ID, build_demo_plan
from demo.synthetic_matrix import demo_matrix
from tests.api.support import (
    ApiServerTestCase,
    ServerBackedTestCase,
    cleanup_scratch_root,
    new_database_file,
    new_scratch_directory,
)

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")

MANUAL_STOP = "S01-NEAR"
DISABLED_STOP = "S10-DISABLED"
SELECTION_PATH = f"/api/plans/{DEMO_PLAN_ID}/selection"
ROUTE_PATH = f"/api/plans/{DEMO_PLAN_ID}/route"
OPTIMIZE_PATH = f"/api/plans/{DEMO_PLAN_ID}/optimize"
RUNS_PATH = f"/api/plans/{DEMO_PLAN_ID}/runs"


class SharedDemoPlanTestCase(ServerBackedTestCase):
    """One stored demo plan and one computed recommendation for the whole class.

    Preparing them once keeps the exhaustive loop out of every test; each test still gets its own
    server and its own ``ApiServices`` over the **same** database file, so a test that changes the
    plan cannot leak into another test.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from api.services import ApiServices

        cls.class_scratch = new_scratch_directory("api-u14-selection")
        cls.class_database = new_database_file(cls.class_scratch)
        seeding = ApiServices(cls.class_database)
        try:
            seeding.plans.create_demo_plan({})
            cls.engine_report = seeding.recommendations.recommend(DEMO_PLAN_ID).report
        finally:
            seeding.close()
        cls.recommended = cls.engine_report.recommended_stop_id
        cls.rejected = cls.engine_report.rejected[0].stop_id
    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.class_scratch, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        self.create_demo_plan()


class SelectionStateMachineTests(SharedDemoPlanTestCase):
    """``POST``/``DELETE /api/plans/{id}/selection`` - the driver's decision, loudly validated."""

    def test_accepting_the_recommendation_sets_mode_source_and_pinned(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "recommend", self.recommended)
        self.assertEqual(response.status, 200, msg=response.text)
        data = response.json()["data"]
        self.assertEqual(data["mode"], "recommend")
        self.assertEqual(data["selected_stop_id"], self.recommended)
        self.assertEqual(data["selection_source"], "accepted_recommendation")
        self.assertIs(data["pinned"], True)
        self.assertEqual(data["state"], "first_stop_selected")
        self.assertEqual(data["first_stop"]["mode"], FirstStopMode.RECOMMEND.value)
        self.assertEqual(
            data["first_stop"]["selection_source"],
            SelectionSource.ACCEPTED_RECOMMENDATION.value,
        )

    def test_the_accept_alias_is_the_same_decision(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "accept", self.recommended)
        self.assertEqual(response.status, 200, msg=response.text)
        data = response.json()["data"]
        self.assertEqual(data["mode"], "recommend")
        self.assertEqual(data["selection_source"], "accepted_recommendation")
        self.assertIs(data["pinned"], True)

    def test_a_manual_choice_is_manual_mode_with_manual_provenance(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        self.assertEqual(response.status, 200, msg=response.text)
        data = response.json()["data"]
        self.assertEqual(data["mode"], "manual")
        self.assertEqual(data["selected_stop_id"], MANUAL_STOP)
        self.assertEqual(data["selection_source"], "manual_choice")
        self.assertIs(data["pinned"], True)
        self.assertEqual(data["first_stop"]["state"], "first_stop_selected")

    def test_the_selection_survives_a_reload_through_the_plan_endpoint(self) -> None:
        self.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        data = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]
        self.assertEqual(data["first_stop"]["selected_stop_id"], MANUAL_STOP)
        self.assertEqual(data["first_stop"]["selection_source"], "manual_choice")
        self.assertIs(data["first_stop"]["pinned"], True)
        self.assertEqual(data["first_stop"]["state"], "first_stop_selected")

    def test_cancelling_returns_to_awaiting_with_a_null_stop_and_source(self) -> None:
        self.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        response = self.delete(SELECTION_PATH)
        self.assertEqual(response.status, 200, msg=response.text)
        data = response.json()["data"]
        self.assertIsNone(data["selected_stop_id"])
        self.assertIsNone(data["selection_source"])
        self.assertIs(data["pinned"], False)
        self.assertEqual(data["state"], "awaiting_first_stop_choice")
        reloaded = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]["first_stop"]
        self.assertIsNone(reloaded["selected_stop_id"])
        self.assertIsNone(reloaded["selection_source"])
        self.assertEqual(reloaded["state"], "awaiting_first_stop_choice")

    def test_cancelling_with_nothing_selected_is_a_409(self) -> None:
        response = self.delete(SELECTION_PATH)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.json()["error"]["code"], "illegal_state")

    def test_an_unknown_stop_is_a_404_and_changes_nothing(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "manual", "S99-NOPE")
        self.assertEqual(response.status, 404)
        error = response.json()["error"]
        self.assertEqual(error["code"], "unknown_stop")
        self.assertIn("S99-NOPE", error["message"])
        self.assertEqual(
            self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]["first_stop"]["state"],
            "awaiting_first_stop_choice",
        )

    def test_a_disabled_stop_is_refused_with_422(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "manual", DISABLED_STOP)
        self.assertEqual(response.status, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_input")
        self.assertIn("disabled", error["message"])

    def test_an_invalid_mode_or_body_is_refused_with_422(self) -> None:
        cases = (
            {"mode": "auto", "stop_id": MANUAL_STOP},
            {"mode": "RECOMMEND", "stop_id": MANUAL_STOP},
            {"mode": None, "stop_id": MANUAL_STOP},
            {"mode": "manual"},
            {"stop_id": MANUAL_STOP},
            {"mode": "manual", "stop_id": MANUAL_STOP, "pinned": True},
            {"mode": "manual", "stop_id": ""},
        )
        for body in cases:
            with self.subTest(body=body):
                response = self.post(SELECTION_PATH, body=body)
                self.assertEqual(response.status, 422, msg=response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_a_mode_source_mismatch_is_refused_not_repaired(self) -> None:
        """``manual`` may only ever mean ``manual_choice`` (D6/D7): the domain rejects the rest.

        Accepting the recommendation under ``mode=manual`` is therefore refused rather than stored
        with a provenance the request did not ask for.
        """
        response = self.select_first_stop(DEMO_PLAN_ID, "manual", self.recommended)
        self.assertEqual(response.status, 200, msg=response.text)
        data = response.json()["data"]
        self.assertEqual(data["mode"], "manual")
        self.assertEqual(data["selection_source"], "manual_choice")
        self.assertNotEqual(data["selection_source"], "accepted_recommendation")

    def test_accepting_a_rejected_candidate_is_a_409(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "recommend", self.rejected)
        self.assertEqual(response.status, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "illegal_state")
        self.assertIn(self.rejected, error["message"])
        self.assertIn("REJECTED", error["message"])

    def test_accepting_another_stop_is_a_409(self) -> None:
        other = MANUAL_STOP if self.recommended != MANUAL_STOP else "S02-NEAR2"
        response = self.select_first_stop(DEMO_PLAN_ID, "accept", other)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.json()["error"]["code"], "illegal_state")

    def test_an_unknown_plan_is_a_404(self) -> None:
        response = self.post(
            "/api/plans/nope/selection", body={"mode": "manual", "stop_id": MANUAL_STOP}
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_plan")

    def test_a_selection_appends_no_run(self) -> None:
        self.select_first_stop(DEMO_PLAN_ID, "accept", self.recommended)
        self.assertEqual(self.get(RUNS_PATH).json()["count"], 0)

    def test_the_selection_payload_carries_no_recommendation_field(self) -> None:
        self.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        text = self.delete(SELECTION_PATH).text
        self.assertNotIn("recommended_stop_id", text)

    def test_a_wrong_method_is_a_405(self) -> None:
        response = self.get(SELECTION_PATH)
        self.assertEqual(response.status, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")


class PreparedDemoPlanTestCase(SharedDemoPlanTestCase):
    """The demo plan with the recommendation accepted, one served route and one recorded run.

    The route and the run are the expensive part (the optimizer plus the exhaustive recalculation),
    so they are prepared **once for the class** and the read-only tests below assert against those
    frozen documents while their own server reads that same prepared database. Tests that need a
    different plan state or a different lock bound remain in their own state-changing classes.
    """

    #: Every test in these cases reads the plan state ``setUpClass`` prepared.
    use_prepared_database = True

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from api.services import ApiServices

        cls.class_database = new_database_file(cls.class_scratch)
        seeding = ApiServices(cls.class_database)
        try:
            seeding.plans.create_demo_plan({})
            seeding.selections.select_first_stop(
                DEMO_PLAN_ID, "recommend", cls.recommended
            )
        finally:
            seeding.close()

        cls.direct_plan = _with_selection(build_demo_plan(), cls.recommended)
        cls.direct_solution = solve_route(
            plan=cls.direct_plan, travel_matrix=demo_matrix()
        )

        services = ApiServices(cls.class_database)
        server = _start_server(services)
        try:
            status, body = _fetch(server, "GET", ROUTE_PATH)
            assert status == 200, body
            cls.route_document = json.loads(body)
            status, body = _fetch(server, "POST", OPTIMIZE_PATH, body=b"{}")
            assert status == 201, body
            cls.run_document = json.loads(body)
            status, body = _fetch(server, "GET", RUNS_PATH)
            assert status == 200, body
            cls.runs_document = json.loads(body)
        finally:
            _stop_server(server, services)

    def make_services(self, identifier: str):
        from api.services import ApiServices

        return ApiServices(identifier)

    def setUp(self) -> None:
        # No re-seeding: the prepared database already holds the selection and the recorded run.
        ApiServerTestCase.setUp(self)
        self.base_url = self.start_server()

class RouteEndpointTests(PreparedDemoPlanTestCase):
    """``GET /api/plans/{id}/route`` - the committed route must BE the optimizer's route."""

    def test_a_route_before_any_selection_is_the_documented_409(self) -> None:
        scratch = new_scratch_directory("api-u14-route-unselected")
        self.addCleanup(shutil.rmtree, scratch, True)
        from api.services import ApiServices

        services = ApiServices(new_database_file(scratch))
        self.addCleanup(services.close)
        server = _start_server(services)
        try:
            services.plans.create_demo_plan({})
            status, body = _fetch(server, "GET", ROUTE_PATH)
        finally:
            _stop_server(server, services)
        self.assertEqual(status, 409, msg=body)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "no_first_stop_selected")
        self.assertIn("awaiting_first_stop_choice", error["message"])
        self.assertNotIn("order", json.loads(body))

    def test_the_served_route_matches_a_direct_optimizer_run_exactly(self) -> None:
        document = self.route_document
        data = document["data"]
        direct = self.direct_solution
        plan = self.direct_plan
        order = data["order"]
        self.assertEqual(order[0], self.recommended)

        self.assertEqual(order, [str(stop_id) for stop_id in direct.order])
        self.assertEqual(len(data["timeline"]), len(direct.timelines))
        for served, timeline in zip(data["timeline"], direct.timelines, strict=True):
            with self.subTest(stop=str(timeline.stop_id)):
                self.assertEqual(served["stop_id"], str(timeline.stop_id))
                self.assertEqual(served["travel_sec"], timeline.travel_time)
                self.assertEqual(served["waiting_sec"], timeline.waiting_time)
                self.assertEqual(served["service_duration_sec"], timeline.service_duration)
                self.assertEqual(served["lateness_sec"], timeline.lateness)
                self.assertEqual(served["finish_overtime_sec"], timeline.finish_overtime)
                self.assertEqual(served["feasibility"], timeline.feasibility.value)
                self.assertEqual(served["window_kind"], timeline.window_kind.value)
                for key, value in served.items():
                    if key.endswith("_sec"):
                        self.assertIsInstance(value, int)
                        self.assertNotIsInstance(value, bool)
                self.assertEqual(
                    served["estimated_arrival"],
                    serialization.instant_text(timeline.estimated_arrival),
                )
                self.assertEqual(
                    served["service_start"],
                    serialization.instant_text(timeline.service_start),
                )
                self.assertEqual(
                    served["estimated_departure"],
                    serialization.instant_text(timeline.estimated_departure),
                )
                self.assertEqual(
                    served["departure_from_previous"],
                    serialization.instant_text(timeline.departure_from_previous),
                )
                self.assertEqual(
                    served["window_end_policy"],
                    timeline.window_end_policy.value if timeline.window_end_policy else None,
                )
                self.assertEqual(
                    served["service_window_start"],
                    None
                    if timeline.service_window_start is None
                    else serialization.instant_text(timeline.service_window_start),
                )
                self.assertEqual(
                    served["service_window_end"],
                    None
                    if timeline.service_window_end is None
                    else serialization.instant_text(timeline.service_window_end),
                )
                for key in (
                    "departure_from_previous",
                    "estimated_arrival",
                    "service_start",
                    "estimated_departure",
                ):
                    self.assertRegex(served[key], UTC_Z)

        # -- the metrics, including both baselines ---------------------------------- #
        metrics = data["metrics"]
        self.assertEqual(metrics["after"]["duration_sec"], direct.metrics.duration_sec)
        self.assertEqual(metrics["after"]["travel_sec"], direct.metrics.travel_sec)
        self.assertEqual(metrics["after"]["waiting_sec"], direct.metrics.waiting_sec)
        self.assertEqual(metrics["after"]["service_sec"], direct.metrics.service_sec)
        self.assertEqual(metrics["after"]["distance_m"], direct.metrics.distance_m)
        self.assertEqual(
            metrics["after"]["finish_arrival"],
            serialization.instant_text(direct.metrics.finish_arrival),
        )
        self.assertIs(metrics["after"]["feasible"], direct.metrics.feasible)
        self.assertIsNone(metrics["after"]["baseline_kind"])
        self.assertEqual(
            metrics["user_baseline"]["duration_sec"], direct.user_baseline.duration_sec
        )
        self.assertEqual(metrics["user_baseline"]["baseline_kind"], "user_supplied")
        self.assertEqual(
            metrics["user_baseline"]["distance_m"], direct.user_baseline.distance_m
        )
        self.assertEqual(
            metrics["algorithm_baseline"]["baseline_kind"], "algorithm_greedy"
        )
        self.assertEqual(
            metrics["algorithm_baseline"]["duration_sec"],
            direct.algorithm_baseline.duration_sec,
        )
        self.assertEqual(
            metrics["saved_duration_sec"],
            metrics["user_baseline"]["duration_sec"] - metrics["after"]["duration_sec"],
        )
        self.assertEqual(
            metrics["saved_distance_m"],
            metrics["user_baseline"]["distance_m"] - metrics["after"]["distance_m"],
        )

        # -- violations, fingerprints, the selection that produced the route --------- #
        self.assertEqual(
            [item["stop_id"] for item in data["violations"]],
            [str(violation.stop_id) for violation in direct.violations],
        )
        self.assertEqual(
            data["fingerprints"]["inputs_fingerprint"], plan.inputs_fingerprint()
        )
        self.assertRegex(data["fingerprints"]["route_fingerprint"], HEX_64)
        self.assertEqual(data["selection"]["selected_stop_id"], self.recommended)
        self.assertEqual(data["selection"]["selection_source"], "accepted_recommendation")
        self.assertIs(data["selection"]["pinned"], True)
        self.assertEqual(data["plan_id"], DEMO_PLAN_ID)
        self.assertEqual(data["status"], direct.status.value)
        self.assertEqual(data["provenance"], "DEMO_SYNTHETIC")
        self.assertEqual(document["type"], serialization.ROUTE_PAYLOAD_TYPE)
        self.assertIs(document["live_recompute"], True)
        self.assertIsInstance(data["computation_seconds"], int)
        self.assertTrue(serialization.is_json_safe(document))

    def test_a_route_request_appends_no_run(self) -> None:
        self.assertEqual(self.runs_document["count"], 1)
        # The prepared run document came from POST /optimize; a GET appends nothing, so the history
        # still holds exactly the one row that request created.
        self.assertEqual(self.runs_document["data"][0]["id"], self.run_document["data"]["id"])
        self.assertIs(self.runs_document["read_only"], True)
        # Reading the route again through the live endpoint changes nothing either.
        response = self.get(ROUTE_PATH)
        self.assertEqual(response.status, 200, msg=response.text)
        self.assertEqual(self.get(RUNS_PATH).json()["count"], 1)

    def test_a_wrong_method_is_a_405(self) -> None:
        response = self.post(ROUTE_PATH, body={})
        self.assertEqual(response.status, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")


class RecordedRecommendationTests(SharedDemoPlanTestCase):
    """A run records the recommendation the ENGINE showed NEXT TO the stop the driver committed.

    The driver manually selects the stop the engine ranks **last**, so ``order[0]`` and the recorded
    ``recommended_stop_id`` are deliberately different stops (D32: the driver may commit a first stop
    the recommendation does not put first, and both facts belong in the row). The recorded
    recommendation must be exactly a direct engine call's ranking on the same plan, and the row must
    never claim a ``recommended_stop_id`` that is not its own ``top_k[0]``.
    """

    def test_the_recorded_recommendation_matches_a_direct_engine_call(self) -> None:
        last_ranked = self.engine_report.ranked[-1].stop_id
        self.assertNotEqual(last_ranked, self.recommended)
        selection = self.select_first_stop(DEMO_PLAN_ID, "manual", last_ranked)
        self.assertEqual(selection.status, 200, msg=selection.text)

        response = self.post(OPTIMIZE_PATH, body={})
        self.assertEqual(response.status, 201, msg=response.text)
        run = response.json()["data"]

        direct = evaluate_first_stop_candidates(
            plan=_with_selection(build_demo_plan(), last_ranked), travel_matrix=demo_matrix()
        )
        ranked_ids = [str(stop_id) for stop_id in direct.ranked_ids()]
        top_k_ids = [candidate["stop_id"] for candidate in run["top_k"]]

        # Both facts of the row coexist: the engine's recommendation and the driver's first stop.
        self.assertEqual(run["order"][0], last_ranked)
        self.assertEqual(
            run["recommendation"]["recommended_stop_id"], str(direct.recommended_stop_id)
        )
        self.assertNotEqual(run["recommendation"]["recommended_stop_id"], run["order"][0])

        # The recorded recommendation is the engine's own ranking, and top_k is its head.
        self.assertEqual(run["recommendation"]["status"], direct.status.value)
        self.assertEqual(run["recommendation"]["ranked_stop_ids"], ranked_ids)
        self.assertEqual(run["recommendation"]["inputs_fingerprint"], direct.inputs_fingerprint)
        self.assertEqual(top_k_ids, ranked_ids[: len(top_k_ids)])
        self.assertEqual(run["recommendation"]["recommended_stop_id"], top_k_ids[0])

        # The recommendation is history, never plan state: the plan keeps the driver's decision.
        self.assertIs(run["recommendation"]["as_plan_state"], False)
        plan = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]
        self.assertEqual(plan["first_stop"]["selected_stop_id"], last_ranked)
        self.assertEqual(plan["first_stop"]["selection_source"], "manual_choice")
        self.assertNotIn("recommended_stop_id", json.dumps(plan["first_stop"]))


class RunHistoryTests(PreparedDemoPlanTestCase):
    """``POST /api/plans/{id}/optimize`` and the read-only history around it."""

    def test_optimize_appends_exactly_one_row_and_returns_its_id(self) -> None:
        document = self.run_document
        run = document["data"]
        self.assertTrue(run["id"].startswith("run-"))
        self.assertEqual(run["plan_id"], DEMO_PLAN_ID)
        self.assertEqual(run["run_kind"], "optimize")
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["algorithm"], "greedy_seed+2opt")
        self.assertEqual(run["algorithm_version"], "1")
        self.assertRegex(run["fingerprints"]["inputs_fingerprint"], HEX_64)
        self.assertRegex(run["fingerprints"]["route_fingerprint"], HEX_64)
        self.assertEqual(run["cost_policy"]["name"], "smart_route_elapsed_v1")
        self.assertEqual(run["data_provenance"], "DEMO_SYNTHETIC")
        self.assertRegex(run["created_at"], UTC_Z)
        self.assertIs(run["has_committed_route"], True)
        self.assertTrue(run["order"])
        self.assertEqual(run["order"], self.route_document["data"]["order"])
        self.assertEqual(run["metrics"]["user_baseline"]["baseline_kind"], "user_supplied")
        self.assertEqual(
            run["metrics"]["algorithm_baseline"]["baseline_kind"], "algorithm_greedy"
        )
        self.assertEqual(
            run["metrics"]["saved_duration_sec"],
            run["metrics"]["user_baseline"]["duration_sec"]
            - run["metrics"]["after"]["duration_sec"],
        )
        self.assertIs(run["recommendation"]["as_plan_state"], False)
        self.assertEqual(document["type"], serialization.RUN_PAYLOAD_TYPE)
        self.assertIsInstance(document["computation"]["computation_seconds"], int)
        self.assertNotIn("computation_seconds", run)

        listed = self.runs_document
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["data"][0], run)

    def test_a_run_is_readable_by_its_own_id(self) -> None:
        run = self.run_document["data"]
        response = self.get(f"/api/runs/{run['id']}")
        self.assertEqual(response.status, 200, msg=response.text)
        self.assertEqual(response.json()["data"], run)
        self.assertEqual(response.json()["type"], serialization.RUN_PAYLOAD_TYPE)

    def test_the_run_history_is_read_only_and_ordered(self) -> None:
        before = self.get(RUNS_PATH).json()
        self.assertIs(before["read_only"], True)
        self.assertIn("only POST /api/plans/{id}/optimize appends a run", before["note"])
        # Reading does not append.
        self.assertEqual(self.get(RUNS_PATH).json()["count"], before["count"])
        created = [run["created_at"] for run in before["data"]]
        self.assertEqual(created, sorted(created))
        self.assertEqual(before["data"], self.runs_document["data"])

    def test_an_unknown_run_id_is_a_404(self) -> None:
        response = self.get("/api/runs/run-does-not-exist")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_run")

    def test_an_unknown_plan_has_no_run_history(self) -> None:
        response = self.get("/api/plans/nope/runs")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_plan")

    def test_the_recorded_recommendation_is_history_not_plan_state(self) -> None:
        run = self.run_document["data"]
        self.assertIs(run["recommendation"]["as_plan_state"], False)
        self.assertIsNotNone(run["recommendation"]["recommended_stop_id"])
        self.assertTrue(run["top_k"])
        self.assertLessEqual(len(run["top_k"]), MAX_RANKED_RECOMMENDATION_CANDIDATES)
        self.assertEqual(run["top_k"][0]["rank"], 1)
        self.assertEqual(run["recommendation"]["ranked_stop_ids"][0], self.recommended)
        self.assertEqual(run["recommendation"]["status"], "recommended")

    def test_a_wrong_method_on_the_history_is_a_405(self) -> None:
        cases = (("POST", RUNS_PATH), ("PATCH", RUNS_PATH), ("POST", "/api/runs/run-1"))
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response = self.request(method, path, body={})
                self.assertEqual(response.status, 405, msg=response.text)
                self.assertEqual(response.json()["error"]["code"], "method_not_allowed")


class OptimizeStateChangeTests(SharedDemoPlanTestCase):
    """The state-changing cases of the recalculation endpoint, on a plan with no selection yet."""

    def accept_recommendation(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "recommend", self.recommended)
        self.assertEqual(response.status, 200, msg=response.text)

    def test_optimize_needs_a_selection_and_refuses_a_body(self) -> None:
        no_selection = self.post(OPTIMIZE_PATH, body={})
        self.assertEqual(no_selection.status, 409)
        self.assertEqual(no_selection.json()["error"]["code"], "no_first_stop_selected")
        self.assertEqual(self.get(RUNS_PATH).json()["count"], 0)
        self.assertEqual(self.get(ROUTE_PATH).status, 409)

        with_body = self.post(OPTIMIZE_PATH, body={"force": True})
        self.assertEqual(with_body.status, 422)
        self.assertEqual(with_body.json()["error"]["code"], "invalid_input")
        self.assertEqual(self.get(RUNS_PATH).json()["count"], 0)

    def test_a_second_recalculation_appends_a_second_row_and_leaves_the_first(self) -> None:
        self.accept_recommendation()
        first = self.post(OPTIMIZE_PATH, body={}).json()["data"]
        second = self.post(OPTIMIZE_PATH, body={}).json()["data"]
        self.assertEqual(first["run_kind"], "optimize")
        self.assertEqual(second["run_kind"], "reoptimize")
        self.assertNotEqual(first["id"], second["id"])

        listed = self.get(RUNS_PATH).json()
        self.assertEqual(listed["count"], 2)
        self.assertEqual([run["id"] for run in listed["data"]], [first["id"], second["id"]])
        # The first row is history: reading it again returns exactly what it was.
        self.assertEqual(listed["data"][0], first)
        self.assertEqual(self.get(f"/api/runs/{first['id']}").json()["data"], first)

    def test_a_manual_selection_routes_from_the_chosen_stop(self) -> None:
        self.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        data = self.get(ROUTE_PATH).json()["data"]
        self.assertEqual(data["order"][0], MANUAL_STOP)
        self.assertEqual(data["selection"]["mode"], "manual")
        self.assertEqual(data["selection"]["selection_source"], "manual_choice")

    def test_a_route_after_cancelling_is_the_409_again(self) -> None:
        self.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        self.assertEqual(self.get(ROUTE_PATH).status, 200)
        self.assertEqual(self.delete(SELECTION_PATH).status, 200)
        response = self.get(ROUTE_PATH)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.json()["error"]["code"], "no_first_stop_selected")


class OptimizeSingleFlightTests(SharedDemoPlanTestCase):
    """Two concurrent recalibrations of one plan: one row, and the documented ``plan_busy``."""

    def make_services(self, identifier: str):
        from api.services import ApiServices

        return ApiServices(identifier, plan_lock_timeout_seconds=0.05)

    def _accept(self) -> None:
        response = self.select_first_stop(DEMO_PLAN_ID, "recommend", self.recommended)
        self.assertEqual(response.status, 200, msg=response.text)

    def test_two_concurrent_optimize_requests_append_exactly_one_row(self) -> None:
        self._accept()
        barrier = threading.Barrier(2, timeout=30)
        results: list[tuple[int, str]] = []
        lock = threading.Lock()

        def call() -> None:
            barrier.wait()
            response = self.request(
                "POST", OPTIMIZE_PATH, body={}, base_url=self.base_url
            )
            with lock:
                results.append((response.status, response.text))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual(len(results), 2, msg=f"a request never answered: {results}")
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses.count(200) + statuses.count(201), 1, msg=results)
        self.assertEqual(statuses.count(409), 1, msg=results)

        refused = [
            json.loads(text) for status, text in results if status == 409
        ][0]
        self.assertEqual(refused["error"]["code"], "plan_busy")
        self.assertIn("no background job queue", refused["error"]["message"])

        listed = self.get(RUNS_PATH).json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["data"][0]["run_kind"], "optimize")

    def test_a_serialized_second_request_appends_the_second_row(self) -> None:
        """The bound is a refusal, not a queue: once the first request is done, the next succeeds."""
        self._accept()
        first = self.post(OPTIMIZE_PATH, body={})
        self.assertEqual(first.status, 201, msg=first.text)
        second = self.post(OPTIMIZE_PATH, body={})
        self.assertEqual(second.status, 201, msg=second.text)
        self.assertEqual(second.json()["data"]["run_kind"], "reoptimize")
        self.assertEqual(self.get(RUNS_PATH).json()["count"], 2)


def _with_selection(plan: RoutePlan, stop_id: str) -> RoutePlan:
    """The same plan with the driver's accepted first stop (a test-side selection, not a write)."""
    return replace(
        plan,
        first_service_stop=FirstStopIntent.accepted_recommendation(stop_id),
    )


def _start_server(services):
    """Start a quiet loopback server for a class-level (``setUpClass``) request."""
    static_root = new_scratch_directory("api-u14-nostatic") / "no-web"
    server = create_server(services, host="127.0.0.1", port=0, quiet=True, static_root=static_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._routepilot_thread = thread  # noqa: SLF001 - the test owns this server instance
    return server


def _stop_server(server, services) -> None:
    server.shutdown()
    server.server_close()
    thread = getattr(server, "_routepilot_thread", None)
    if thread is not None:
        thread.join(5.0)
    services.close()


def _fetch(server, method: str, path: str, body: bytes | None = None):
    """One request against a server this module started itself (no ``self.base_url`` needed)."""
    host, port = server.server_address[0], server.server_address[1]
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


if __name__ == "__main__":
    unittest.main()
