"""Stage 3 U12 (D38): the end-to-end storage round-trip demo.

Drives :mod:`demo.storage_roundtrip` in-process against the **real** engine and the **real** SQLite
repositories, on ``:memory:`` databases only:

* the demo is deterministic: two runs render byte-identical text and produce equal structured
  results (a pinned run instant, an injected repository clock, no wall clock and no
  machine-specific path);
* the shipped demo plan survives the plan repository exactly (``RoutePlan == loaded RoutePlan``),
  with 31 enabled + 1 disabled stop, and START/FINISH come back as plan locations, never stop rows;
* the reloaded plan re-runs the exhaustive first-stop evaluation and the optimizer to the same
  inputs fingerprint, route fingerprint, order and complete-route result (strict DST after load,
  D3);
* the real run derived from that solution round-trips through ``list_for_plan`` and ``latest``, and
  two ``app_settings`` entries round-trip through ``get``/``set``;
* no database file is created: the connection the demo uses is in-memory, and the demo accepts no
  path option that could point at one.

Deterministic and offline: no files, no scratch directories, no network, no wall clock.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import unittest

from core.engine.optimizer.route_fingerprint import route_fingerprint
from core.model.first_stop import FirstStopIntent
from core.model.optimization_run import RunKind, RunStatus
from core.model.value_objects import DataProvenance
from demo.dataset import build_demo_plan
from demo.storage_roundtrip import (
    DEMO_CREATED_AT_UTC,
    DEMO_RUN_ID,
    DEMO_TOP_K,
    SETTINGS_ENTRIES,
    main,
    open_demo_database,
    render_report,
    run_round_trip,
)
from storage.sqlite.app_settings_repository import SqliteAppSettingsRepository
from storage.sqlite.database import connect, migrate
from storage.sqlite.optimization_run_repository import SqliteRouteOptimizationRunRepository
from storage.sqlite.route_plan_repository import SqliteRoutePlanRepository
from tests import REPO_ROOT


class StorageRoundTripDemoTests(unittest.TestCase):
    """One shared demo run on a shared ``:memory:`` database the tests also query directly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = connect(":memory:")
        migrate(cls.connection)
        cls.result = run_round_trip(cls.connection)
        cls.report = render_report(cls.result)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()

    # -- determinism ------------------------------------------------------ #
    def test_two_runs_produce_identical_output(self) -> None:
        second = run_round_trip()
        self.assertEqual(render_report(second), self.report)
        self.assertEqual(second.plan_id, self.result.plan_id)
        self.assertEqual(second.original, self.result.original)
        self.assertEqual(second.reloaded, self.result.reloaded)
        self.assertEqual(second.run, self.result.run)
        self.assertEqual(second.settings_after, self.result.settings_after)

    def test_the_report_contains_no_machine_specific_path(self) -> None:
        self.assertNotIn(str(REPO_ROOT), self.report)
        self.assertNotIn("storage_roundtrip.py", self.report)

    def test_main_returns_zero_and_prints_that_report(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main([]), 0)
        self.assertEqual(buffer.getvalue(), self.report + "\n")

    # -- the plan round trip ---------------------------------------------- #
    def test_the_reloaded_plan_equals_the_original(self) -> None:
        self.assertTrue(self.result.plan_equal)
        plan = build_demo_plan()
        self.assertEqual(plan.id, self.result.plan_id)
        self.assertEqual(len(plan.stops), self.result.stop_rows)
        self.assertEqual(len(plan.active_stops()), self.result.enabled_count)
        self.assertEqual(len(plan.disabled_stops()), self.result.disabled_count)
        self.assertEqual((self.result.enabled_count, self.result.disabled_count), (31, 1))
        self.assertEqual(self.result.plan_first_stop_state, "awaiting_first_stop_choice")

        # Independently of the demo's own flag: the repository returns the plan unchanged.
        plans = SqliteRoutePlanRepository(
            self.connection, data_provenance=DataProvenance.DEMO_SYNTHETIC
        )
        self.assertEqual(plans.get(plan.id), plan)

    def test_start_and_finish_stay_plan_locations(self) -> None:
        plan = build_demo_plan()
        self.assertFalse(self.result.start_and_finish_are_stop_rows)
        self.assertEqual(self.result.start_label, plan.departure.label)
        self.assertEqual(self.result.finish_label, plan.finish.label)
        row = self.connection.execute(
            "SELECT departure_label, finish_label FROM route_plans WHERE id = ?",
            (self.result.plan_id,),
        ).fetchone()
        self.assertEqual(row["departure_label"], plan.departure.label)
        self.assertEqual(row["finish_label"], plan.finish.label)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM route_stops WHERE plan_id = ?", (self.result.plan_id,)
            ).fetchone()[0],
            len(plan.stops),
        )

    # -- the engine after the load ---------------------------------------- #
    def test_the_reloaded_plan_reproduces_the_same_fingerprints_and_route(self) -> None:
        original, loaded = self.result.original, self.result.reloaded
        self.assertTrue(self.result.routes_identical)
        self.assertEqual(original, loaded)
        self.assertEqual(original.inputs_fingerprint, loaded.inputs_fingerprint)
        self.assertEqual(original.route_fingerprint, loaded.route_fingerprint)
        self.assertEqual(original.order, loaded.order)
        self.assertEqual(original.metrics, loaded.metrics)
        self.assertEqual(original.violations, loaded.violations)
        self.assertEqual(original.timelines, loaded.timelines)
        self.assertEqual(len(original.order), self.result.enabled_count)

        # Both fingerprints are recomputable from the shipped plan, so neither is a fixture echo.
        plan = build_demo_plan()
        self.assertEqual(original.inputs_fingerprint, plan.inputs_fingerprint())
        selected = dataclasses.replace(
            plan,
            first_service_stop=FirstStopIntent.accepted_recommendation(
                self.result.recommended_stop_id
            ),
        )
        self.assertEqual(route_fingerprint(selected, original.order), original.route_fingerprint)
        self.assertEqual(original.user_baseline.baseline_kind.value, "user_supplied")
        self.assertEqual(original.algorithm_baseline.baseline_kind.value, "algorithm_greedy")
        self.assertIsNone(original.metrics.baseline_kind)

    # -- the run history -------------------------------------------------- #
    def test_the_run_history_round_trips(self) -> None:
        run = self.result.run
        self.assertEqual(run.id, DEMO_RUN_ID)
        self.assertIs(run.run_kind, RunKind.OPTIMIZE)
        self.assertEqual(run.created_at_utc, DEMO_CREATED_AT_UTC)
        self.assertEqual(run.plan_id, self.result.plan_id)
        self.assertEqual(run.order, self.result.original.order)
        self.assertEqual(run.metrics.after, self.result.original.metrics)
        self.assertEqual(run.metrics.user_baseline, self.result.original.user_baseline)
        self.assertEqual(run.metrics.algorithm_baseline, self.result.original.algorithm_baseline)
        self.assertEqual(run.violations, self.result.original.violations)
        self.assertIs(run.status, RunStatus.OK)
        self.assertEqual(run.recommendation.recommended_stop_id, self.result.recommended_stop_id)
        self.assertEqual(len(run.top_k or ()), DEMO_TOP_K)
        self.assertEqual(
            tuple(candidate.stop_id for candidate in run.top_k or ()),
            self.result.ranked_ids[:DEMO_TOP_K],
        )

        repository = SqliteRouteOptimizationRunRepository(self.connection)
        stored = repository.list_for_plan(self.result.plan_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0], run)
        self.assertEqual(repository.latest(self.result.plan_id), run)
        self.assertTrue(self.result.run_round_trip_equal)
        self.assertEqual(self.result.history_count, 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM route_optimization_runs").fetchone()[0],
            1,
        )

    # -- the settings ----------------------------------------------------- #
    def test_the_settings_round_trip(self) -> None:
        repository = SqliteAppSettingsRepository(self.connection)
        for (key, value), before, after in zip(
            SETTINGS_ENTRIES, self.result.settings_before, self.result.settings_after
        ):
            self.assertIsNone(before, msg=f"{key} must start unset")
            self.assertEqual(after, value)
            self.assertEqual(repository.get(key), value)
        self.assertEqual(len(SETTINGS_ENTRIES), 2)
        self.assertTrue(self.result.settings_round_trip_ok)

    # -- database hygiene ------------------------------------------------- #
    def test_no_database_file_is_created(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA database_list").fetchone()[2], "")
        owned = open_demo_database()
        self.addCleanup(owned.close)
        self.assertEqual(owned.execute("PRAGMA database_list").fetchone()[2], "")
        # The demo accepts no path option, so it cannot be pointed at a file: v2 section 37 and D38
        # forbid a committed database file, and an argv the demo does not know is refused.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--database", "demo.db"])
        self.assertEqual(caught.exception.code, 2)

    # -- what the printed report claims ----------------------------------- #
    def test_the_report_states_the_three_acceptance_facts_and_the_recompute_decision(self) -> None:
        self.assertIn("plan round trip         : EXACT", self.report)
        self.assertIn("run round trip          : EXACT", self.report)
        self.assertIn("settings round trip     : EXACT", self.report)
        self.assertIn("engine after reload     : IDENTICAL", self.report)
        self.assertIn("IDENTICAL: True", self.report)
        self.assertIn(":memory:", self.report)
        self.assertIn("no database file is created", self.report)
        # D38 open question 2 = RECOMPUTE is stated, and the pinned run instant is the printed one.
        self.assertIn("D38 OPEN QUESTION 2 = RECOMPUTE", self.report)
        self.assertIn("2026-09-11T06:00:00Z", self.report)
        self.assertIn("DEMO / SYNTHETIC DATA", self.report)
