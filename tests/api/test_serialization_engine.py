"""The Stage 4 U14 serialisation contracts of ``api/serialization.py`` (deliverable 4).

These tests pin the three U14 payload families - the advisory **recommendation**, the **committed
route** and the immutable **optimization run** - exactly the way ``tests/api/test_serialization.py``
pins the plan payloads: UTC instants end in ``Z``, durations are integer **seconds**, booleans are
JSON booleans (never ``0``/``1``), fingerprints are lowercase hex text, and a recommendation is
**never** plan state.

Cost is bounded deliberately: one demo plan is created, recommended, selected, routed and optimized
**once** in ``setUpClass`` (each exhaustive call evaluates 31 complete routes), and every test reads
those stored payloads. The ``no_fully_feasible_route`` rule needs a different fixture - the engine's
own report for ``tests.api.support.build_infeasible_demo_plan()`` - so it is a second case, and that
one needs no database at all.
"""

from __future__ import annotations

import json
import re
import shutil
import unittest
from dataclasses import replace

from api import serialization
from api.services import MAX_RANKED_RECOMMENDATION_CANDIDATES, ApiServices
from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.engine.optimizer.route_fingerprint import route_fingerprint
from core.engine.optimizer.solve import solve_route
from core.model.first_stop import FirstStopIntent, FirstStopMode
from demo.synthetic_matrix import demo_matrix
from tests.api.support import (
    build_infeasible_demo_plan,
    cleanup_scratch_root,
    new_database_file,
    new_scratch_directory,
)

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

#: ``2026-09-11T01:00:00Z`` - UTC, second precision, trailing Z.
UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")

DEMO_PLAN_ID = "demo-route-01"

#: The exact keys every U14 payload documents (``api/serialization.py``), so a payload that silently
#: loses one, or the reader that reads it, is a test failure rather than a surprise in the UI.
DOCUMENTED_RECOMMENDATION_DOCUMENT_KEYS = frozenset(
    {"type", "api_version", "live_recompute", "computed_at", "data"}
)
DOCUMENTED_RECOMMENDATION_KEYS = frozenset(
    {
        "plan_id",
        "status",
        "recommended_stop_id",
        "advisory",
        "applied_decision",
        "as_plan_state",
        "note",
        "policy",
        "fingerprints",
        "counts",
        "computation_seconds",
        "disabled_stop_ids",
        "ranked",
        "rejected",
        "diagnostics",
    }
)
DOCUMENTED_CANDIDATE_KEYS = frozenset(
    {"stop_id", "rank", "feasible", "first_leg", "complete_route", "objective"}
)
DOCUMENTED_FIRST_LEG_KEYS = frozenset(
    {
        "travel_sec",
        "estimated_arrival",
        "service_window_start",
        "waiting_sec",
        "estimated_service_start",
        "lateness_sec",
    }
)
DOCUMENTED_COMPLETE_ROUTE_KEYS = frozenset(
    {
        "duration_sec",
        "travel_sec",
        "waiting_sec",
        "service_sec",
        "finish_arrival",
        "max_lateness_sec",
        "violating_stop_ids",
    }
)
DOCUMENTED_OBJECTIVE_KEYS = frozenset({"score", "breakdown", "metrics"})
DOCUMENTED_CANDIDATE_METRICS_KEYS = frozenset({"travel_sec", "waiting_sec", "distance_m"})
DOCUMENTED_DIAGNOSTIC_KEYS = frozenset(
    {"stop_id", "candidate_stop_id", "code", "violation_kind", "message", "reason"}
)
DOCUMENTED_TIMELINE_ROW_KEYS = frozenset(
    {
        "stop_id",
        "departure_from_previous",
        "travel_sec",
        "estimated_arrival",
        "window_kind",
        "service_window_start",
        "service_window_end",
        "window_end_policy",
        "waiting_sec",
        "service_start",
        "service_duration_sec",
        "estimated_departure",
        "lateness_sec",
        "finish_overtime_sec",
        "feasibility",
        "flags",
    }
)
DOCUMENTED_ROUTE_METRICS_KEYS = frozenset(
    {
        "distance_m",
        "duration_sec",
        "travel_sec",
        "waiting_sec",
        "service_sec",
        "finish_arrival",
        "feasible",
        "baseline_kind",
    }
)
DOCUMENTED_VIOLATION_KEYS = frozenset(
    {"stop_id", "kind", "message", "service_start", "service_window_end"}
)
DOCUMENTED_ROUTE_KEYS = frozenset(
    {
        "order",
        "selection",
        "status",
        "timeline",
        "metrics",
        "violations",
        "fingerprints",
        "tzdata_version",
        "provenance",
    }
)
DOCUMENTED_ROUTE_DOCUMENT_KEYS = frozenset(
    {"type", "api_version", "live_recompute", "data"}
)
DOCUMENTED_RUN_KEYS = frozenset(
    {
        "id",
        "plan_id",
        "run_kind",
        "status",
        "algorithm",
        "algorithm_version",
        "fingerprints",
        "tzdata_version",
        "cost_policy",
        "data_provenance",
        "created_at",
        "order",
        "has_committed_route",
        "metrics",
        "violations",
        "recommendation",
        "top_k",
    }
)
DOCUMENTED_RUN_METRICS_KEYS = frozenset(
    {"after", "user_baseline", "algorithm_baseline", "saved_distance_m", "saved_duration_sec"}
)
DOCUMENTED_RUN_RECOMMENDATION_KEYS = frozenset(
    {
        "status",
        "recommended_stop_id",
        "ranked_stop_ids",
        "resolved_at",
        "inputs_fingerprint",
        "diagnostics",
        "as_plan_state",
    }
)
DOCUMENTED_RUN_DOCUMENT_KEYS = frozenset({"type", "api_version", "computation", "data"})
DOCUMENTED_RUN_LIST_KEYS = frozenset(
    {"type", "api_version", "plan_id", "count", "read_only", "note", "data"}
)
DOCUMENTED_SELECTION_DOCUMENT_KEYS = frozenset({"type", "api_version", "data"})
DOCUMENTED_SELECTION_KEYS = frozenset(
    {
        "plan_id",
        "first_stop",
        "state",
        "mode",
        "selected_stop_id",
        "selection_source",
        "pinned",
        "note",
    }
)


class PayloadContractAssertions:
    """The three checks the JSON contract repeats, shared by both cases below.

    Deliberately not a ``TestCase``: it holds no tests, only the assertions an instant, a duration
    and a number need everywhere in these payloads, so the rule is stated once.
    """

    def assert_utc_instant(self, value: object, where: str) -> None:
        """An instant in this API is UTC ISO-8601 text with a trailing ``Z`` (D2)."""
        self.assertIsInstance(value, str, msg=f"{where} must be instant text, got {value!r}")
        self.assertRegex(value, UTC_Z, msg=f"{where} is not UTC ISO-8601 with a trailing Z")

    def assert_whole_seconds(self, value: object, where: str) -> None:
        """A duration is integer SECONDS: never a ``bool``, never a float, never ``"1h30m"``."""
        self.assertIsInstance(value, int, msg=f"{where} must be integer SECONDS, got {value!r}")
        self.assertNotIsInstance(value, bool, msg=f"{where} must not be a boolean, got {value!r}")

    def assert_number(self, value: object, where: str) -> None:
        """A distance stays a number (never a formatted string, never a ``bool``)."""
        self.assertIsInstance(value, (int, float), msg=f"{where} must be a number, got {value!r}")
        self.assertNotIsInstance(value, bool, msg=f"{where} must not be a boolean, got {value!r}")


class EnginePayloadContractTests(PayloadContractAssertions, unittest.TestCase):
    """One demo plan, recommended/selected/routed/optimized once; every test reads that payload.

    The expensive work happens exactly once per class: the recommendation loop, the selection
    verification, the committed route and the recorded run. Nothing here recomputes a route per test
    and nothing writes to the tree - the engine is deterministic, so one serialisation is enough.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = new_scratch_directory("api-serialization-u14")
        cls.services = ApiServices(new_database_file(cls.scratch))
        cls.services.plans.create_demo_plan({})

        recommendation = cls.services.recommendations.recommend(DEMO_PLAN_ID)
        cls.recommendation_report = recommendation.report
        cls.recommendation_payload = serialization.recommendation_payload(recommendation.report)
        cls.recommendation_document = serialization.recommendation_document(
            recommendation.report,
            computed_at=serialization.instant_text(recommendation.report.resolved_at),
            computation_seconds=recommendation.computation_seconds,
            ranked_limit=MAX_RANKED_RECOMMENDATION_CANDIDATES,
        )

        selection = cls.services.selections.select_first_stop(
            DEMO_PLAN_ID, "recommend", recommendation.report.recommended_stop_id
        )
        cls.selection_plan = selection.plan
        cls.selection_payload = serialization.selection_payload(selection.plan)
        cls.selection_document = serialization.selection_document(selection.plan)

        cls.route_result = cls.services.routes.committed_route(DEMO_PLAN_ID)
        cls.route_payload = serialization.route_payload(
            cls.route_result.solution,
            route_fingerprint=cls.route_result.route_fingerprint,
        )
        cls.route_document = serialization.route_document(
            cls.route_result.solution,
            plan_id=cls.route_result.plan.id,
            route_fingerprint=cls.route_result.route_fingerprint,
            computation_seconds=cls.route_result.computation_seconds,
        )

        run_result = cls.services.routes.optimize_and_record(DEMO_PLAN_ID)
        # Named ``optimization_run`` on purpose: ``TestCase.run`` is the runner's own method.
        cls.optimization_run = run_result.run
        cls.run_payload = serialization.run_payload(run_result.run)
        cls.computation = {
            "computation_seconds": run_result.computation_seconds,
            "recommendation_seconds": run_result.recommendation_seconds,
            "route_seconds": run_result.route_seconds,
            "note": "measured latency of this recalculation (test fixture)",
        }
        cls.run_document = serialization.run_document(
            run_result.run, computation=cls.computation
        )
        cls.runs = cls.services.routes.list_runs(DEMO_PLAN_ID)
        cls.run_list_document = serialization.run_list_document(DEMO_PLAN_ID, cls.runs)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.services.close()
        shutil.rmtree(cls.scratch, ignore_errors=True)

    # -- 1. every U14 payload is JSON-safe -------------------------------- #
    def test_every_u14_payload_is_json_safe(self) -> None:
        """No enum, no ``datetime``, no tuple and no set may reach the transport (U13/U14 rule)."""
        payloads = {
            "recommendation document": self.recommendation_document,
            "recommendation payload": self.recommendation_payload,
            "route document": self.route_document,
            "route payload": self.route_payload,
            "run document": self.run_document,
            "run payload": self.run_payload,
            "run list document": self.run_list_document,
            "selection document": self.selection_document,
            "selection payload": self.selection_payload,
        }
        for name, payload in payloads.items():
            with self.subTest(payload=name):
                self.assertTrue(serialization.is_json_safe(payload))
                json.dumps(payload)  # raising TypeError here would be the failure

    def test_the_documents_name_their_payload_type_and_version(self) -> None:
        for name, document, payload_type in (
            (
                "recommendation",
                self.recommendation_document,
                serialization.RECOMMENDATION_PAYLOAD_TYPE,
            ),
            ("route", self.route_document, serialization.ROUTE_PAYLOAD_TYPE),
            ("run", self.run_document, serialization.RUN_PAYLOAD_TYPE),
            ("selection", self.selection_document, serialization.SELECTION_PAYLOAD_TYPE),
        ):
            with self.subTest(document=name):
                self.assertEqual(document["type"], payload_type)
                self.assertEqual(document["api_version"], serialization.API_VERSION)

    def test_the_recommendation_document_is_a_live_recompute_envelope(self) -> None:
        document = self.recommendation_document
        self.assertEqual(set(document), DOCUMENTED_RECOMMENDATION_DOCUMENT_KEYS)
        self.assertIs(document["live_recompute"], True)
        self.assertEqual(
            document["data"]["status"], self.recommendation_report.status.value
        )
        # A top-K view may return fewer candidates than were ranked, never more.
        self.assertLessEqual(
            document["data"]["counts"]["ranked_returned"],
            document["data"]["counts"]["ranked"],
        )
        self.assertEqual(
            document["data"]["counts"]["ranked_returned"], len(document["data"]["ranked"])
        )

    def test_the_selection_document_is_the_selection_envelope(self) -> None:
        document = self.selection_document
        self.assertEqual(set(document), DOCUMENTED_SELECTION_DOCUMENT_KEYS)
        self.assertEqual(document["data"], self.selection_payload)

    # -- 2. instants: UTC ISO-8601 with a trailing Z ---------------------- #
    def test_candidate_instants_are_utc_iso8601_with_a_trailing_z(self) -> None:
        """Every candidate instant that exists must be UTC with a trailing ``Z``."""
        candidates = self.recommendation_payload["ranked"] + self.recommendation_payload["rejected"]
        self.assertTrue(candidates)
        observed = {"estimated_arrival": 0, "estimated_service_start": 0, "finish_arrival": 0}
        for candidate in candidates:
            with self.subTest(stop=candidate["stop_id"]):
                first_leg = candidate["first_leg"]
                complete = candidate["complete_route"]
                values = {
                    "estimated_arrival": first_leg["estimated_arrival"],
                    "estimated_service_start": first_leg["estimated_service_start"],
                    "finish_arrival": complete["finish_arrival"],
                }
                for field, value in values.items():
                    if value is None:
                        continue
                    self.assert_utc_instant(value, f"{field} of {candidate['stop_id']}")
                    observed[field] += 1
                if first_leg["service_window_start"] is not None:
                    self.assert_utc_instant(
                        first_leg["service_window_start"], "service_window_start"
                    )
        for field, count in observed.items():
            self.assertGreater(count, 0, msg=f"no candidate carried a {field} instant")

    def test_route_timeline_and_metrics_instants_are_utc_iso8601(self) -> None:
        timeline = self.route_payload["timeline"]
        self.assertTrue(timeline)
        for row in timeline:
            with self.subTest(stop=row["stop_id"]):
                for field in (
                    "departure_from_previous",
                    "estimated_arrival",
                    "service_start",
                    "estimated_departure",
                ):
                    self.assert_utc_instant(row[field], f"timeline.{field}")
                for field in ("service_window_start", "service_window_end"):
                    if row[field] is not None:
                        self.assert_utc_instant(row[field], f"timeline.{field}")
        for name in ("after", "user_baseline", "algorithm_baseline"):
            with self.subTest(metrics=name):
                block = self.route_payload["metrics"][name]
                self.assertIsNotNone(block, msg=f"route metrics has no {name} block")
                self.assert_utc_instant(block["finish_arrival"], f"metrics.{name}.finish_arrival")

    def test_envelope_instants_computed_at_and_created_at_are_utc(self) -> None:
        resolved_at = self.recommendation_report.resolved_at
        self.assertIsNotNone(resolved_at)
        self.assert_utc_instant(
            self.recommendation_document["computed_at"], "computed_at"
        )
        self.assertEqual(
            self.recommendation_document["computed_at"],
            serialization.instant_text(resolved_at),
        )
        self.assert_utc_instant(self.run_payload["created_at"], "run.created_at")
        self.assertEqual(
            self.run_payload["created_at"],
            serialization.instant_text(self.optimization_run.created_at_utc),
        )
        recommendation = self.run_payload["recommendation"]
        if recommendation["resolved_at"] is not None:
            self.assert_utc_instant(recommendation["resolved_at"], "run.recommendation.resolved_at")
        for candidate in self.run_payload["top_k"] or ():
            with self.subTest(stop=candidate["stop_id"]):
                self.assert_utc_instant(
                    candidate["first_leg"]["estimated_arrival"], "top_k.estimated_arrival"
                )
                self.assert_utc_instant(
                    candidate["complete_route"]["finish_arrival"], "top_k.finish_arrival"
                )

    # -- 3. durations are integer seconds --------------------------------- #
    def test_candidate_durations_are_integer_seconds_and_distance_is_a_number(self) -> None:
        candidates = self.recommendation_payload["ranked"] + self.recommendation_payload["rejected"]
        for candidate in candidates:
            with self.subTest(stop=candidate["stop_id"]):
                first_leg = candidate["first_leg"]
                for field in ("travel_sec", "waiting_sec", "lateness_sec"):
                    self.assert_whole_seconds(first_leg[field], f"first_leg.{field}")
                complete = candidate["complete_route"]
                for field in (
                    "duration_sec",
                    "travel_sec",
                    "waiting_sec",
                    "service_sec",
                    "max_lateness_sec",
                ):
                    self.assert_whole_seconds(complete[field], f"complete_route.{field}")
                metrics = candidate["objective"]["metrics"]
                self.assertIsNotNone(metrics)
                self.assert_whole_seconds(metrics["travel_sec"], "objective.metrics.travel_sec")
                self.assert_whole_seconds(metrics["waiting_sec"], "objective.metrics.waiting_sec")
                self.assert_number(metrics["distance_m"], "objective.metrics.distance_m")

    def test_route_metrics_and_timeline_durations_are_integer_seconds(self) -> None:
        for name in ("after", "user_baseline", "algorithm_baseline"):
            with self.subTest(metrics=name):
                block = self.route_payload["metrics"][name]
                for field in ("duration_sec", "travel_sec", "waiting_sec", "service_sec"):
                    self.assert_whole_seconds(block[field], f"metrics.{name}.{field}")
                self.assert_number(block["distance_m"], f"metrics.{name}.distance_m")
        for row in self.route_payload["timeline"]:
            with self.subTest(stop=row["stop_id"]):
                for field in (
                    "travel_sec",
                    "waiting_sec",
                    "service_duration_sec",
                    "lateness_sec",
                    "finish_overtime_sec",
                ):
                    self.assert_whole_seconds(row[field], f"timeline.{field}")
        self.assert_whole_seconds(
            self.route_payload["metrics"]["saved_duration_sec"], "metrics.saved_duration_sec"
        )
        self.assert_number(
            self.route_payload["metrics"]["saved_distance_m"], "metrics.saved_distance_m"
        )

    def test_run_metrics_durations_are_integer_seconds(self) -> None:
        metrics = self.run_payload["metrics"]
        self.assertEqual(set(metrics), DOCUMENTED_RUN_METRICS_KEYS)
        for name in ("after", "user_baseline", "algorithm_baseline"):
            with self.subTest(metrics=name):
                block = metrics[name]
                for field in ("duration_sec", "travel_sec", "waiting_sec", "service_sec"):
                    self.assert_whole_seconds(block[field], f"run.metrics.{name}.{field}")
                self.assert_number(block["distance_m"], f"run.metrics.{name}.distance_m")
        self.assert_whole_seconds(metrics["saved_duration_sec"], "run.metrics.saved_duration_sec")
        self.assert_number(metrics["saved_distance_m"], "run.metrics.saved_distance_m")

    # -- 4. booleans are JSON booleans, never 0/1 -------------------------- #
    def test_booleans_are_json_booleans_never_zero_or_one(self) -> None:
        """``assertIs`` is the point: ``0 == False`` and ``1 == True``, ``0 is False`` is not."""
        payload = self.recommendation_payload
        self.assertIs(payload["advisory"], True)
        self.assertIs(payload["applied_decision"], False)
        self.assertIs(payload["as_plan_state"], False)
        for candidate in payload["ranked"]:
            with self.subTest(stop=candidate["stop_id"]):
                self.assertIs(candidate["feasible"], True)
        for candidate in payload["rejected"]:
            with self.subTest(stop=candidate["stop_id"]):
                self.assertIs(candidate["feasible"], False)
        for name in ("after", "user_baseline", "algorithm_baseline"):
            with self.subTest(metrics=name):
                self.assertIsInstance(self.route_payload["metrics"][name]["feasible"], bool)
                self.assertIsInstance(self.run_payload["metrics"][name]["feasible"], bool)
        self.assertIs(self.route_payload["metrics"]["after"]["feasible"], True)
        self.assertIs(self.run_payload["metrics"]["after"]["feasible"], True)
        self.assertIs(self.run_payload["has_committed_route"], True)
        self.assertIs(self.selection_payload["pinned"], True)
        self.assertIs(self.selection_payload["first_stop"]["pinned"], True)
        self.assertIs(self.recommendation_document["live_recompute"], True)
        self.assertIs(self.route_document["live_recompute"], True)
        self.assertIs(self.run_list_document["read_only"], True)
        self.assertIsInstance(self.run_payload["cost_policy"]["provisional"], bool)

    # -- 5. fingerprints are lowercase 64-char hex ------------------------ #
    def test_fingerprints_are_lowercase_64_char_hex(self) -> None:
        self.assertRegex(
            self.recommendation_payload["fingerprints"]["inputs_fingerprint"], HEX_64
        )
        route_fingerprints = self.route_payload["fingerprints"]
        self.assertEqual(
            set(route_fingerprints), {"inputs_fingerprint", "route_fingerprint"}
        )
        self.assertRegex(route_fingerprints["inputs_fingerprint"], HEX_64)
        self.assertRegex(route_fingerprints["route_fingerprint"], HEX_64)
        run_fingerprints = self.run_payload["fingerprints"]
        self.assertEqual(set(run_fingerprints), {"inputs_fingerprint", "route_fingerprint"})
        for field, value in run_fingerprints.items():
            with self.subTest(fingerprint=field):
                self.assertRegex(value, HEX_64)
        self.assertRegex(
            self.run_payload["recommendation"]["inputs_fingerprint"], HEX_64
        )

    def test_no_payload_claims_a_matrix_fingerprint_the_api_cannot_produce(self) -> None:
        """No field is emitted that the API never fills.

        ``core`` computes no fingerprint for the configured travel matrix - ``route_fingerprint``
        and ``RoutePlan.inputs_fingerprint`` *accept* a caller-supplied one - so the contract exposes
        the two fingerprints the engine does produce and no permanently-``null`` third one. The
        absence is asserted rather than the shape of a ``None``.
        """
        for name, document in (
            ("route payload", self.route_payload),
            ("route document", self.route_document),
            ("run payload", self.run_payload),
            ("run document", self.run_document),
            ("run list document", self.run_list_document),
        ):
            with self.subTest(document=name):
                self.assertNotIn("matrix_fingerprint", json.dumps(document))

    # -- 6. the recommendation says plainly that it is advisory ----------- #
    def test_the_recommendation_payload_says_plainly_that_it_is_advisory(self) -> None:
        """The product principle of D4/D32 stated as data, so no client can misread it."""
        payload = self.recommendation_payload
        self.assertIs(payload["advisory"], True)
        self.assertIs(payload["applied_decision"], False)
        self.assertIs(payload["as_plan_state"], False)
        self.assertEqual(payload["note"], serialization.RECOMMENDATION_ADVISORY_NOTE)
        self.assertIn("recommendation", payload["note"])
        self.assertIn("not an applied decision", payload["note"])

    # -- 7. counts, the ranked limit and the ranks ------------------------ #
    def test_the_counts_report_the_exhaustive_evaluation(self) -> None:
        """``candidates_evaluated == ranked + rejected``: no unreported prefilter (v2 section 20)."""
        counts = self.recommendation_payload["counts"]
        self.assertEqual(counts["candidates_evaluated"], counts["ranked"] + counts["rejected"])
        self.assertEqual(
            counts["candidates_evaluated"], self.recommendation_report.candidates_evaluated
        )
        self.assertEqual(counts["ranked"], len(self.recommendation_report.ranked))
        self.assertEqual(counts["rejected"], len(self.recommendation_report.rejected))
        self.assertEqual(counts["ranked_returned"], len(self.recommendation_payload["ranked"]))
        # Nothing was truncated, because no ranked_limit was passed.
        self.assertEqual(counts["ranked_returned"], counts["ranked"])
        self.assertTrue(self.recommendation_payload["rejected"])

    def test_a_ranked_limit_truncates_only_the_ranked_list(self) -> None:
        """``ranked_limit`` is a top-K view: the counts still state the whole ranking (section 13)."""
        limit = 3
        full = self.recommendation_payload
        self.assertGreater(full["counts"]["ranked"], limit)
        limited = serialization.recommendation_payload(
            self.recommendation_report, ranked_limit=limit
        )
        counts = limited["counts"]
        self.assertEqual(len(limited["ranked"]), limit)
        self.assertEqual(counts["ranked_returned"], len(limited["ranked"]))
        self.assertLessEqual(counts["ranked_returned"], counts["ranked"])
        self.assertEqual(counts["ranked"], full["counts"]["ranked"])
        # The rejected candidates are never truncated: their diagnostics are the point (section 14).
        self.assertEqual(counts["rejected"], full["counts"]["rejected"])
        self.assertEqual(len(limited["rejected"]), len(full["rejected"]))
        self.assertEqual(len(limited["diagnostics"]), len(full["diagnostics"]))
        self.assertEqual(
            [candidate["stop_id"] for candidate in limited["ranked"]],
            [candidate["stop_id"] for candidate in full["ranked"][:limit]],
        )

    def test_ranked_entries_are_one_based_and_rejected_entries_are_unranked(self) -> None:
        payload = self.recommendation_payload
        self.assertTrue(payload["ranked"])
        self.assertEqual(
            [candidate["rank"] for candidate in payload["ranked"]],
            list(range(1, len(payload["ranked"]) + 1)),
        )
        self.assertTrue(payload["rejected"])
        for candidate in payload["rejected"]:
            with self.subTest(stop=candidate["stop_id"]):
                self.assertIsNone(candidate["rank"])

    # -- 9. the committed route ------------------------------------------- #
    def test_the_route_payload_is_the_order_with_one_timeline_row_per_stop(self) -> None:
        payload = self.route_payload
        order = payload["order"]
        self.assertTrue(order)
        self.assertEqual(len(payload["timeline"]), len(order))
        self.assertEqual([row["stop_id"] for row in payload["timeline"]], order)
        self.assertEqual([str(stop_id) for stop_id in self.route_result.solution.order], order)
        self.assertIsInstance(payload["violations"], list)
        # The route is the driver's own decision, not one the engine made (I3/I4).
        self.assertEqual(
            payload["selection"]["selected_stop_id"],
            self.recommendation_report.recommended_stop_id,
        )
        self.assertEqual(payload["selection"]["selection_source"], "accepted_recommendation")

    def test_the_route_metrics_carry_after_and_both_baselines(self) -> None:
        """The committed route is unlabelled; the two baselines are explicitly labelled (D22)."""
        metrics = self.route_payload["metrics"]
        self.assertEqual(
            set(metrics),
            {"after", "user_baseline", "algorithm_baseline", "saved_distance_m", "saved_duration_sec"},
        )
        self.assertIsNotNone(metrics["after"])
        self.assertIsNotNone(metrics["user_baseline"])
        self.assertIsNotNone(metrics["algorithm_baseline"])
        self.assertIsNone(metrics["after"]["baseline_kind"])
        self.assertEqual(metrics["user_baseline"]["baseline_kind"], "user_supplied")
        self.assertEqual(metrics["algorithm_baseline"]["baseline_kind"], "algorithm_greedy")

    def test_the_route_document_envelope(self) -> None:
        document = self.route_document
        self.assertEqual(set(document), DOCUMENTED_ROUTE_DOCUMENT_KEYS)
        self.assertEqual(document["type"], serialization.ROUTE_PAYLOAD_TYPE)
        self.assertEqual(document["api_version"], serialization.API_VERSION)
        self.assertIs(document["live_recompute"], True)
        self.assertEqual(document["data"]["plan_id"], DEMO_PLAN_ID)
        seconds = document["data"]["computation_seconds"]
        self.assertIsInstance(seconds, int)
        self.assertNotIsInstance(seconds, bool)
        self.assertGreaterEqual(seconds, 0)

    # -- 10. the optimization run ----------------------------------------- #
    def test_the_run_payload_carries_the_immutable_row_fields(self) -> None:
        payload = self.run_payload
        run = self.optimization_run
        self.assertEqual(payload["id"], str(run.id))
        self.assertTrue(payload["id"].startswith("run-"))
        self.assertEqual(payload["plan_id"], DEMO_PLAN_ID)
        self.assertEqual(payload["run_kind"], run.run_kind.value)
        self.assertEqual(payload["run_kind"], "optimize")  # the plan's first recorded run
        self.assertEqual(payload["status"], run.status.value)
        self.assertEqual(payload["algorithm"], "greedy_seed+2opt")
        self.assertEqual(payload["algorithm_version"], run.algorithm_version)
        self.assertTrue(
            payload["tzdata_version"] is None or isinstance(payload["tzdata_version"], str)
        )
        self.assertEqual(payload["cost_policy"]["name"], "smart_route_elapsed_v1")
        self.assertRegex(payload["created_at"], UTC_Z)
        self.assertEqual(payload["order"], [str(stop_id) for stop_id in run.order])
        self.assertIsInstance(payload["violations"], list)
        self.assertIs(payload["has_committed_route"], True)
        top_k = payload["top_k"]
        self.assertTrue(top_k is None or (isinstance(top_k, list) and top_k))
        if top_k is not None:
            self.assertEqual(
                [candidate["rank"] for candidate in top_k],
                list(range(1, len(top_k) + 1)),
            )
        self.assertEqual(set(payload["fingerprints"]), {"inputs_fingerprint", "route_fingerprint"})

    def test_the_run_metrics_carry_all_three_routes_and_the_savings(self) -> None:
        metrics = self.run_payload["metrics"]
        self.assertIsNone(metrics["after"]["baseline_kind"])
        self.assertEqual(metrics["user_baseline"]["baseline_kind"], "user_supplied")
        self.assertEqual(metrics["algorithm_baseline"]["baseline_kind"], "algorithm_greedy")
        self.assertEqual(
            metrics["saved_duration_sec"],
            metrics["user_baseline"]["duration_sec"] - metrics["after"]["duration_sec"],
        )
        self.assertAlmostEqual(
            metrics["saved_distance_m"],
            metrics["user_baseline"]["distance_m"] - metrics["after"]["distance_m"],
            places=6,
        )

    def test_the_run_recommendation_is_history_and_never_plan_state(self) -> None:
        recommendation = self.run_payload["recommendation"]
        self.assertEqual(set(recommendation), DOCUMENTED_RUN_RECOMMENDATION_KEYS)
        self.assertIs(recommendation["as_plan_state"], False)
        self.assertEqual(recommendation["status"], self.recommendation_report.status.value)
        self.assertEqual(
            recommendation["recommended_stop_id"],
            self.recommendation_report.recommended_stop_id,
        )
        self.assertEqual(
            recommendation["ranked_stop_ids"],
            [str(stop_id) for stop_id in self.recommendation_report.ranked_ids()],
        )

    def test_the_run_list_document_is_read_only_and_keeps_the_repository_order(self) -> None:
        document = self.run_list_document
        self.assertEqual(set(document), DOCUMENTED_RUN_LIST_KEYS)
        self.assertEqual(document["type"], "OptimizationRunList")
        self.assertEqual(document["api_version"], serialization.API_VERSION)
        self.assertEqual(document["plan_id"], DEMO_PLAN_ID)
        self.assertIs(document["read_only"], True)
        self.assertEqual(document["count"], len(self.runs))
        self.assertEqual(
            [entry["id"] for entry in document["data"]],
            [str(run.id) for run in self.runs],
        )

    def test_the_run_document_keeps_the_measured_computation_outside_data(self) -> None:
        """Measured latency is about the request, so it is never part of the stored row."""
        document = self.run_document
        self.assertEqual(set(document), DOCUMENTED_RUN_DOCUMENT_KEYS)
        self.assertEqual(document["type"], serialization.RUN_PAYLOAD_TYPE)
        self.assertEqual(document["api_version"], serialization.API_VERSION)
        self.assertEqual(document["computation"], self.computation)
        self.assertEqual(document["data"], self.run_payload)
        self.assertNotIn("computation", document["data"])
        self.assertNotIn("computation_seconds", document["data"])

    # -- 11. the selection is the driver's decision, never a recommendation #
    def test_the_selection_payload_contains_no_recommendation_field(self) -> None:
        """A recommendation is derived and never plan state (D4/D11/D32/I5)."""
        text = json.dumps(serialization.selection_payload(self.selection_plan))
        self.assertNotIn("recommended_stop_id", text)
        self.assertNotIn("as_plan_state", text)
        self.assertNotIn("advisory", text)
        for payload in (self.selection_payload, self.selection_payload["first_stop"]):
            for key in payload:
                with self.subTest(key=key):
                    self.assertNotIn("recommend", key)

    def test_the_selection_payload_reports_the_drivers_decision(self) -> None:
        payload = self.selection_payload
        for field in (
            "state",
            "mode",
            "selected_stop_id",
            "selection_source",
            "pinned",
            "note",
        ):
            self.assertIn(field, payload)
        self.assertEqual(payload["state"], "first_stop_selected")
        self.assertEqual(payload["mode"], "recommend")
        self.assertEqual(
            payload["selected_stop_id"], self.recommendation_report.recommended_stop_id
        )
        self.assertEqual(payload["selection_source"], "accepted_recommendation")
        self.assertIs(payload["pinned"], True)
        self.assertTrue(payload["note"])

    # -- 12. the documented keys are present, and the stable sets exact --- #
    def test_every_documented_key_is_present(self) -> None:
        cases = (
            (
                "recommendation document",
                self.recommendation_document,
                DOCUMENTED_RECOMMENDATION_DOCUMENT_KEYS,
            ),
            (
                "recommendation payload",
                self.recommendation_payload,
                DOCUMENTED_RECOMMENDATION_KEYS,
            ),
            ("route payload", self.route_payload, DOCUMENTED_ROUTE_KEYS),
            (
                "route document data",
                self.route_document["data"],
                DOCUMENTED_ROUTE_KEYS | {"plan_id", "computation_seconds"},
            ),
            ("run payload", self.run_payload, DOCUMENTED_RUN_KEYS),
            ("run document", self.run_document, DOCUMENTED_RUN_DOCUMENT_KEYS),
            ("run list document", self.run_list_document, DOCUMENTED_RUN_LIST_KEYS),
            ("selection payload", self.selection_payload, DOCUMENTED_SELECTION_KEYS),
            ("selection document", self.selection_document, DOCUMENTED_SELECTION_DOCUMENT_KEYS),
        )
        for name, payload, required in cases:
            with self.subTest(payload=name):
                missing = sorted(required - set(payload))
                self.assertEqual(missing, [], msg=f"{name} is missing keys {missing}")

    def test_candidate_payloads_pin_the_exact_sub_block_keys(self) -> None:
        self.assertTrue(self.recommendation_payload["ranked"])
        for candidate in self.recommendation_payload["ranked"]:
            with self.subTest(stop=candidate["stop_id"]):
                self.assertEqual(set(candidate), DOCUMENTED_CANDIDATE_KEYS)
                self.assertEqual(set(candidate["first_leg"]), DOCUMENTED_FIRST_LEG_KEYS)
                self.assertEqual(set(candidate["complete_route"]), DOCUMENTED_COMPLETE_ROUTE_KEYS)
                self.assertEqual(set(candidate["objective"]), DOCUMENTED_OBJECTIVE_KEYS)
                self.assertEqual(
                    set(candidate["objective"]["metrics"]), DOCUMENTED_CANDIDATE_METRICS_KEYS
                )

    def test_timeline_rows_pin_the_exact_keys(self) -> None:
        self.assertTrue(self.route_payload["timeline"])
        for row in self.route_payload["timeline"]:
            with self.subTest(stop=row["stop_id"]):
                self.assertEqual(set(row), DOCUMENTED_TIMELINE_ROW_KEYS)

    def test_route_metrics_blocks_pin_the_exact_keys(self) -> None:
        for name in ("after", "user_baseline", "algorithm_baseline"):
            with self.subTest(metrics=name):
                self.assertEqual(
                    set(self.route_payload["metrics"][name]), DOCUMENTED_ROUTE_METRICS_KEYS
                )

    def test_selection_payload_pins_the_exact_top_level_keys(self) -> None:
        self.assertEqual(set(self.selection_payload), DOCUMENTED_SELECTION_KEYS)
        self.assertEqual(
            set(self.selection_payload["first_stop"]),
            {"mode", "selected_stop_id", "selection_source", "pinned", "state", "description"},
        )

    def test_diagnostics_pin_the_exact_keys(self) -> None:
        self.assertTrue(self.recommendation_payload["diagnostics"])
        for diagnostic in self.recommendation_payload["diagnostics"]:
            with self.subTest(stop=diagnostic["stop_id"]):
                self.assertEqual(set(diagnostic), DOCUMENTED_DIAGNOSTIC_KEYS)


class NoFullyFeasibleRouteTests(PayloadContractAssertions, unittest.TestCase):
    """The engine's ``no_fully_feasible_route`` report is a valid answer, not an error (section 14).

    The fixture is the demo plan whose stops all close before the driver can arrive, and the report
    is the real engine's
    (:func:`core.engine.first_stop.evaluation.evaluate_first_stop_candidates`) - nothing here is a
    hand-built report object. There is no database at all: this rule is about the payload, not about
    storage. The exhaustive loop runs once for the class.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.plan, cls.window_stop_id = build_infeasible_demo_plan()
        cls.report = evaluate_first_stop_candidates(
            plan=cls.plan, travel_matrix=demo_matrix()
        )
        cls.payload = serialization.recommendation_payload(cls.report)
        cls.document = serialization.recommendation_document(
            cls.report,
            computed_at=(
                None
                if cls.report.resolved_at is None
                else serialization.instant_text(cls.report.resolved_at)
            ),
        )

    def test_a_no_fully_feasible_route_report_serialises_as_a_valid_payload(self) -> None:
        """No fabricated winner: the valid answer is the diagnostics, with no recommended stop."""
        payload = self.payload
        self.assertEqual(payload["status"], "no_fully_feasible_route")
        self.assertEqual(payload["status"], self.report.status.value)
        self.assertIsNone(payload["recommended_stop_id"])
        self.assertEqual(payload["ranked"], [])
        self.assertEqual(payload["counts"]["ranked"], 0)
        self.assertTrue(payload["diagnostics"])
        self.assertTrue(payload["rejected"])
        self.assertEqual(
            payload["counts"]["candidates_evaluated"],
            payload["counts"]["ranked"] + payload["counts"]["rejected"],
        )
        self.assertEqual(payload["counts"]["rejected"], len(self.report.rejected))
        # The violations are the engine's own: the window stop it could not serve.
        first_rejected = payload["rejected"][0]
        self.assertIsNone(first_rejected["rank"])
        self.assertIs(first_rejected["feasible"], False)
        self.assertIn(
            self.window_stop_id, first_rejected["complete_route"]["violating_stop_ids"]
        )
        for diagnostic in payload["diagnostics"][:5]:
            with self.subTest(stop=diagnostic["stop_id"]):
                self.assertEqual(set(diagnostic), DOCUMENTED_DIAGNOSTIC_KEYS)
                self.assertTrue(diagnostic["code"])
                self.assertTrue(diagnostic["message"])
                self.assertTrue(diagnostic["reason"])
                self.assertTrue(diagnostic["candidate_stop_id"])
        self.assertIs(payload["advisory"], True)
        self.assertIs(payload["applied_decision"], False)
        self.assertEqual(payload["note"], serialization.RECOMMENDATION_ADVISORY_NOTE)
        for name, document in (("payload", payload), ("document", self.document)):
            with self.subTest(document=name):
                self.assertTrue(serialization.is_json_safe(document))
                json.dumps(document)

    def test_violation_payloads_are_explicit_and_utc(self) -> None:
        """A missed hard window is explicit data with UTC instants, never a hidden penalty (D13)."""
        selected = replace(
            self.plan,
            first_service_stop=FirstStopIntent.manual_choice(
                self.window_stop_id, mode=FirstStopMode.MANUAL
            ),
        )
        solution = solve_route(plan=selected, travel_matrix=demo_matrix())
        payload = serialization.route_payload(
            solution,
            route_fingerprint=route_fingerprint(selected, solution.order),
        )
        self.assertEqual(payload["status"], "has_infeasible_windows")
        self.assertIsInstance(payload["violations"], list)
        self.assertTrue(payload["violations"])
        self.assertIs(payload["metrics"]["after"]["feasible"], False)
        for violation in payload["violations"][:5]:
            with self.subTest(stop=violation["stop_id"]):
                self.assertEqual(set(violation), DOCUMENTED_VIOLATION_KEYS)
                self.assertTrue(violation["message"])
                for field in ("service_start", "service_window_end"):
                    if violation[field] is not None:
                        self.assert_utc_instant(violation[field], f"violation.{field}")


if __name__ == "__main__":
    unittest.main()
