"""The engine-facing application layer of Stage 4 U14, called directly (no HTTP).

These tests exercise :class:`api.services.RecommendationService`,
:class:`api.services.SelectionService` and :class:`api.services.RouteService` the way a future
FastAPI transport would: plain Python calls over a real file-backed SQLite database and the real
deterministic engine. The transport-level equivalents live in
``tests/api/test_recommendation_endpoints.py`` and ``tests/api/test_selection_and_runs.py``.

Cost: the exhaustive recommendation evaluates one complete route per enabled stop (31 on the demo
plan), so a class computes the recommendation **once** in ``setUpClass`` and its tests reuse that
result instead of re-running the loop per test.

Determinism: the demo fixture is deterministic, the travel matrix is DEMO/SYNTHETIC and no test
reads a wall clock for an assertion. The only measured numbers here are latencies, which are
reported, never asserted exactly.
"""

from __future__ import annotations

import shutil
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from api.services import (
    ApiServices,
    CapabilityNotImplemented,
    Conflict,
    InvalidInput,
    NoFirstStopSelected,
    NotFound,
    PlanBusy,
    RecommendationService,
    RouteService,
    SelectionService,
    UnknownPlan,
    UnknownStop,
    run_id_for,
)
from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.engine.optimizer.route_evaluation import evaluate_order
from core.engine.optimizer.solve import solve_route
from core.engine.providers import ProviderCapabilities
from core.model.cost_policy import smart_route_elapsed_policy
from core.model.first_stop import (
    FirstStopMode,
    FirstStopState,
    RecommendationStatus,
    SelectionSource,
)
from core.model.optimization_run import RunKind, RunStatus
from core.model.route_mode import RouteMode
from core.model.value_objects import DataProvenance
from demo.dataset import DEMO_PLAN_ID
from demo.synthetic_matrix import demo_matrix
from storage import StorageError
from tests.api.support import (
    build_infeasible_demo_plan,
    cleanup_scratch_root,
    new_database_file,
    new_scratch_directory,
)

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

#: The demo stop the manual-choice tests use (a real, enabled demo stop).
MANUAL_STOP = "S01-NEAR"
#: A real demo stop that is stored disabled.
DISABLED_STOP = "S10-DISABLED"

#: A fixed, timezone-aware UTC instant a test injects through the container's clock seam.
FIXED_NOW = datetime(2026, 9, 11, 12, 34, 56, tzinfo=timezone.utc)


class ScaledDemoMatrix:
    """A deterministic **non-demo** ``TravelMatrix``: the demo legs, priced differently.

    It is a real provider object rather than the :func:`demo.synthetic_matrix.demo_matrix` singleton,
    so a service configured with it must price every leg through it. The numbers are the demo
    fixture's times and distances scaled by whole factors, which keeps the matrix deterministic while
    making its results visibly different from the demo matrix's.
    """

    #: The computation is still DEMO/SYNTHETIC data - only the priced numbers differ.
    provenance = DataProvenance.DEMO_SYNTHETIC
    capabilities = ProviderCapabilities()

    def __init__(self, *, time_scale: int = 2, distance_scale: int = 2) -> None:
        self._inner = demo_matrix()
        self._time_scale = int(time_scale)
        self._distance_scale = int(distance_scale)

    def travel_time_seconds(self, origin, destination) -> int:
        return int(self._inner.travel_time_seconds(origin, destination)) * self._time_scale

    def distance_meters(self, origin, destination) -> float:
        return float(self._inner.distance_meters(origin, destination)) * self._distance_scale


class InMemoryPlanRepository:
    """A minimal plan repository used only to seed a **constructed** fixture.

    It stores plans in a dict and speaks the same domain vocabulary as the SQLite adapter, so a test
    can exercise the service layer with a plan the demo fixture cannot express without writing a
    database artifact. It is deliberately tiny: the real persistence path is covered by
    ``tests/storage`` and by every other test in this suite.
    """

    def __init__(self, plans: dict, *, data_provenance: DataProvenance) -> None:
        self._plans = plans
        self.data_provenance = data_provenance

    def save(self, plan) -> None:
        self._plans[plan.id] = plan

    def get(self, plan_id):
        return self._plans.get(str(plan_id))

    def list(self):
        return tuple(self._plans[key] for key in sorted(self._plans))

    def delete(self, plan_id) -> bool:
        return self._plans.pop(str(plan_id), None) is not None


class ServiceEngineTestCase(unittest.TestCase):
    """A real file-backed database plus one stored demo plan per test."""

    #: Extra :class:`ApiServices` configuration for a case that needs a deterministic seam (a clock,
    #: a travel matrix). Empty for the ordinary cases, which run the production defaults.
    services_kwargs: dict = {}

    def setUp(self) -> None:
        self.scratch = new_scratch_directory("api-u14-services")
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.services = ApiServices(
            new_database_file(self.scratch), **self.services_kwargs
        )
        self.addCleanup(self.services.close)

    def seed_demo_plan(self) -> None:
        self.services.plans.create_demo_plan({})


class SharedDemoPlanTestCase(ServiceEngineTestCase):
    """One recomputed demo recommendation for the whole class (the loop costs seconds).

    The engine is deterministic, so the recommendation computed here is the same one every request
    in the class recomputes; the tests below use it as the **expected** value for the engine and as
    the stop the driver accepts.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.class_scratch = new_scratch_directory("api-u14-crosscheck")
        cls.shared = ApiServices(new_database_file(cls.class_scratch))
        cls.shared.plans.create_demo_plan({})
        cls.expected = cls.shared.recommendations.recommend(DEMO_PLAN_ID).report

    @classmethod
    def tearDownClass(cls) -> None:
        cls.shared.close()
        shutil.rmtree(cls.class_scratch, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        self.seed_demo_plan()
        self.recommended = self.expected.recommended_stop_id
        self.rejected = self.expected.rejected[0].stop_id


class RecommendationServiceTests(SharedDemoPlanTestCase):
    """``recommendation(plan_id)`` runs the real engine and is advisory only."""

    def test_the_report_is_the_engines_report_for_the_stored_plan(self) -> None:
        result = self.services.recommendations.recommend(DEMO_PLAN_ID)
        stored = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        direct = evaluate_first_stop_candidates(plan=stored, travel_matrix=demo_matrix())

        self.assertEqual(result.report.status, RecommendationStatus.RECOMMENDED)
        self.assertEqual(result.report.ranked, direct.ranked)
        self.assertEqual(result.report.rejected, direct.rejected)
        self.assertEqual(result.report.diagnostics, direct.diagnostics)
        self.assertEqual(result.report.ranked_ids(), direct.ranked_ids())
        self.assertEqual(result.report.rejected_ids(), direct.rejected_ids())
        self.assertEqual(result.report.recommended_stop_id, direct.recommended_stop_id)
        self.assertEqual(result.report.inputs_fingerprint, direct.inputs_fingerprint)
        self.assertEqual(result.report.candidates_evaluated, direct.candidates_evaluated)
        self.assertEqual(
            result.report.candidates_evaluated,
            len(result.report.ranked) + len(result.report.rejected),
        )
        # Exhaustive: one optimizer run per enabled stop, and no candidate is silently dropped.
        self.assertEqual(result.report.optimizer_runs, len(stored.active_stops()))
        self.assertEqual(result.report.candidates_evaluated, len(stored.active_stops()))
        self.assertGreater(len(result.report.rejected), 0)

    def test_the_recommendation_writes_nothing_at_all(self) -> None:
        before = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.services.recommendations.recommend(DEMO_PLAN_ID)
        after = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.assertEqual(after, before)
        self.assertEqual(after.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertIsNone(after.first_service_stop.selected_stop_id)
        self.assertEqual(self.services.routes.list_runs(DEMO_PLAN_ID), ())

    def test_a_recommendation_is_never_a_selection(self) -> None:
        result = self.services.recommendations.recommend(DEMO_PLAN_ID)
        stored = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.assertIsNotNone(result.report.recommended_stop_id)
        self.assertIsNone(stored.first_service_stop.selected_stop_id)
        self.assertFalse(stored.first_service_stop.pinned)

    def test_the_computation_reports_its_own_measured_duration(self) -> None:
        result = self.services.recommendations.recommend(DEMO_PLAN_ID)
        self.assertIsInstance(result.computation_seconds, int)
        self.assertGreaterEqual(result.computation_seconds, 0)

    def test_an_unknown_plan_is_refused(self) -> None:
        with self.assertRaises(UnknownPlan):
            self.services.recommendations.recommend("nope")


class NoFullyFeasibleRouteTests(unittest.TestCase):
    """Every candidate infeasible is a VALID answer with diagnostics - never an error (v2 section 14).

    The fixture is the demo plan with one change: every enabled stop closes before the driver
    departs, so every complete route misses a hard window. The engine is not mocked and no payload is
    built by hand - the service returns the engine's own report. The exhaustive loop runs once here
    and the tests below read that one report.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.class_scratch = new_scratch_directory("api-u14-infeasible")
        cls.plan, cls.window_stop_id = build_infeasible_demo_plan()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.class_scratch, ignore_errors=True)

    def setUp(self) -> None:
        # One in-memory plan store for this test's whole container: the factory is called per
        # request, so the store has to be created once here or a selection would be written to a
        # repository instance that the next request no longer sees.
        self.plans = {self.plan.id: self.plan}
        self.services = ApiServices(
            new_database_file(self.class_scratch),
            plan_repository_factory=lambda connection, *, data_provenance: InMemoryPlanRepository(
                self.plans, data_provenance=data_provenance
            ),
        )
        self.addCleanup(self.services.close)
        self.addCleanup(self._drop_plan, self.plan.id)
        self.report = self.services.recommendations.recommend(self.plan.id).report

    def _drop_plan(self, plan_id: str) -> None:
        self.plans.pop(plan_id, None)

    def test_every_candidate_is_rejected_with_its_violating_stop(self) -> None:
        report = self.report
        self.assertEqual(report.status, RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE)
        self.assertIsNone(report.recommended_stop_id)
        self.assertEqual(report.ranked, ())
        self.assertEqual(len(report.rejected), len(self.plan.active_stops()))
        self.assertEqual(report.candidates_evaluated, len(report.rejected))
        self.assertTrue(report.diagnostics)
        self.assertIn(self.window_stop_id, report.rejected[0].violating_stop_ids)
        for diagnostic in report.diagnostics:
            self.assertEqual(diagnostic.code, "time_window_infeasible")
            self.assertTrue(diagnostic.reason)
            self.assertTrue(diagnostic.candidate_stop_id)

    def test_the_report_converts_to_a_diagnostic_recommendation_with_no_stop(self) -> None:
        recommendation = self.report.to_recommendation()
        self.assertEqual(recommendation.status, RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE)
        self.assertIsNone(recommendation.recommended_stop_id)
        self.assertEqual(recommendation.ranked, ())

    def test_no_route_can_be_committed_from_an_infeasible_plan(self) -> None:
        with self.assertRaises(NoFirstStopSelected):
            self.services.routes.committed_route(self.plan.id)

    def test_accepting_a_stop_is_refused_when_nothing_is_recommended(self) -> None:
        for mode in ("recommend", "accept"):
            with self.subTest(mode=mode):
                with self.assertRaises(Conflict) as caught:
                    self.services.selections.select_first_stop(
                        self.plan.id, mode, self.window_stop_id
                    )
                self.assertEqual(caught.exception.code, "illegal_state")
                self.assertIn("no_fully_feasible_route", str(caught.exception))

    def test_a_manual_choice_is_still_the_drivers_decision(self) -> None:
        """The driver may still choose a stop; the route is then reported with its violations.

        The whole sequence runs through **one** service container, so the selection the service
        persisted is the one the route is computed from - the same object graph a request would use.
        """
        result = self.services.selections.select_first_stop(
            self.plan.id, "manual", self.window_stop_id
        )
        self.assertEqual(result.plan.first_service_stop.mode, FirstStopMode.MANUAL)
        self.assertEqual(
            result.plan.first_service_stop.selection_source, SelectionSource.MANUAL_CHOICE
        )
        route = self.services.routes.committed_route(self.plan.id)
        self.assertEqual(route.solution.order[0], self.window_stop_id)
        self.assertEqual(route.solution.status.value, "has_infeasible_windows")
        self.assertTrue(route.solution.violations)
        self.assertFalse(route.solution.metrics.feasible)
        self.assertFalse(route.solution.user_baseline.feasible)
        self.assertEqual(self.services.routes.list_runs(self.plan.id), ())


class SelectionServiceTests(SharedDemoPlanTestCase):
    """The driver-decision state machine, applied through the domain (D4-D11/D32)."""

    def test_accepting_the_recommendation_pins_it_with_its_provenance(self) -> None:
        result = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "recommend", self.recommended
        )
        intent = result.plan.first_service_stop
        self.assertEqual(intent.mode, FirstStopMode.RECOMMEND)
        self.assertEqual(intent.selected_stop_id, self.recommended)
        self.assertEqual(intent.selection_source, SelectionSource.ACCEPTED_RECOMMENDATION)
        self.assertTrue(intent.pinned)
        self.assertEqual(result.plan.first_stop_state, FirstStopState.FIRST_STOP_SELECTED)
        self.assertEqual(result.previous_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertIsNone(result.previous_stop_id)

    def test_the_accept_alias_is_the_same_decision(self) -> None:
        result = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "accept", self.recommended
        )
        intent = result.plan.first_service_stop
        self.assertEqual(intent.mode, FirstStopMode.RECOMMEND)
        self.assertEqual(intent.selection_source, SelectionSource.ACCEPTED_RECOMMENDATION)
        self.assertTrue(intent.pinned)

    def test_a_manual_choice_is_manual_mode_with_manual_provenance(self) -> None:
        result = self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        intent = result.plan.first_service_stop
        self.assertEqual(intent.mode, FirstStopMode.MANUAL)
        self.assertEqual(intent.selected_stop_id, MANUAL_STOP)
        self.assertEqual(intent.selection_source, SelectionSource.MANUAL_CHOICE)
        self.assertTrue(intent.pinned)
        self.assertEqual(self.services.routes.list_runs(DEMO_PLAN_ID), ())

    def test_the_selection_survives_a_reload_from_sqlite(self) -> None:
        self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        reloaded = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        intent = reloaded.first_service_stop
        self.assertEqual(intent.mode, FirstStopMode.MANUAL)
        self.assertEqual(intent.selected_stop_id, MANUAL_STOP)
        self.assertEqual(intent.selection_source, SelectionSource.MANUAL_CHOICE)
        self.assertTrue(intent.pinned)
        self.assertEqual(reloaded.first_stop_state, FirstStopState.FIRST_STOP_SELECTED)

    def test_cancelling_returns_to_awaiting_with_a_null_stop_and_source(self) -> None:
        self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        result = self.services.selections.clear_first_stop(DEMO_PLAN_ID)
        intent = result.plan.first_service_stop
        self.assertIsNone(intent.selected_stop_id)
        self.assertIsNone(intent.selection_source)
        self.assertFalse(intent.pinned)
        self.assertEqual(result.plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertEqual(result.previous_stop_id, MANUAL_STOP)
        self.assertEqual(result.previous_state, FirstStopState.FIRST_STOP_SELECTED)
        reloaded = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.assertIsNone(reloaded.first_service_stop.selected_stop_id)
        self.assertIsNone(reloaded.first_service_stop.selection_source)

    def test_cancelling_with_nothing_selected_is_a_conflict(self) -> None:
        with self.assertRaises(Conflict) as caught:
            self.services.selections.clear_first_stop(DEMO_PLAN_ID)
        self.assertEqual(caught.exception.code, "illegal_state")

    def test_an_unknown_stop_is_refused(self) -> None:
        with self.assertRaises(UnknownStop) as caught:
            self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", "S99-NOPE")
        self.assertIn("S99-NOPE", str(caught.exception))

    def test_a_disabled_stop_cannot_be_the_first_stop(self) -> None:
        with self.assertRaises(InvalidInput) as caught:
            self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", DISABLED_STOP)
        self.assertIn("disabled", str(caught.exception))
        self.assertIsNone(
            self.services.plans.get_plan(DEMO_PLAN_ID).plan.first_service_stop.selected_stop_id
        )

    def test_an_unknown_mode_is_refused(self) -> None:
        for mode in ("auto", "RECOMMEND", "", None, 3):
            with self.subTest(mode=mode):
                with self.assertRaises(InvalidInput):
                    self.services.selections.select_first_stop(DEMO_PLAN_ID, mode, MANUAL_STOP)

    def test_accepting_a_rejected_candidate_is_refused(self) -> None:
        with self.assertRaises(Conflict) as caught:
            self.services.selections.select_first_stop(DEMO_PLAN_ID, "recommend", self.rejected)
        message = str(caught.exception)
        self.assertIn(self.rejected, message)
        self.assertIn("REJECTED", message)
        self.assertIsNone(
            self.services.plans.get_plan(DEMO_PLAN_ID).plan.first_service_stop.selected_stop_id
        )

    def test_accepting_a_stop_the_engine_did_not_recommend_is_refused(self) -> None:
        other = MANUAL_STOP if self.recommended != MANUAL_STOP else "S02-NEAR2"
        with self.assertRaises(Conflict) as caught:
            self.services.selections.select_first_stop(DEMO_PLAN_ID, "accept", other)
        self.assertIn("recommends", str(caught.exception))

    def test_an_unknown_plan_is_refused(self) -> None:
        with self.assertRaises(UnknownPlan):
            self.services.selections.select_first_stop("nope", "manual", MANUAL_STOP)
        with self.assertRaises(UnknownPlan):
            self.services.selections.clear_first_stop("nope")

    def test_a_selection_does_not_write_a_recommendation_into_the_plan(self) -> None:
        """A recommendation is not plan state (D4/D11/D32): the stored plan carries no such field."""
        selected = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "manual", MANUAL_STOP
        ).plan
        self.assertFalse(hasattr(selected, "recommended_stop_id"))
        self.assertNotIn("recommended", selected.first_service_stop.describe())
        self.assertEqual(selected.first_service_stop.selected_stop_id, MANUAL_STOP)

    def test_an_unknown_build_cannot_route_a_plan_whose_mode_is_not_implemented(self) -> None:
        plan = replace(
            self.services.plans.create_demo_plan({}).plan, route_mode=RouteMode.FASTEST
        )
        services = ApiServices(
            new_database_file(self.scratch),
            plan_repository_factory=lambda connection, *, data_provenance: InMemoryPlanRepository(
                {plan.id: plan}, data_provenance=data_provenance
            ),
        )
        self.addCleanup(services.close)
        for call in (
            lambda: services.recommendations.recommend(plan.id),
            lambda: services.selections.select_first_stop(plan.id, "manual", MANUAL_STOP),
            lambda: services.routes.committed_route(plan.id),
            lambda: services.routes.optimize_and_record(plan.id),
        ):
            with self.subTest(call=call):
                with self.assertRaises(CapabilityNotImplemented):
                    call()


class RouteServiceTests(SharedDemoPlanTestCase):
    """The committed route of the current selection, from the real optimizer (v2 sections 12/15)."""

    def setUp(self) -> None:
        super().setUp()
        self.plan = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "recommend", self.recommended
        ).plan

    def test_the_route_is_the_optimizer_route_for_the_selection(self) -> None:
        result = self.services.routes.committed_route(DEMO_PLAN_ID)
        direct = solve_route(plan=self.plan, travel_matrix=demo_matrix())
        self.assertEqual(result.solution.order, direct.order)
        self.assertEqual(result.solution.timelines, direct.timelines)
        self.assertEqual(result.solution.metrics, direct.metrics)
        self.assertEqual(result.solution.user_baseline, direct.user_baseline)
        self.assertEqual(result.solution.algorithm_baseline, direct.algorithm_baseline)
        self.assertEqual(result.solution.violations, direct.violations)
        self.assertEqual(result.solution.status, direct.status)
        self.assertEqual(result.solution.order[0], self.recommended)

    def test_both_baselines_are_the_engines_own_evaluations(self) -> None:
        result = self.services.routes.committed_route(DEMO_PLAN_ID)
        baseline = evaluate_order(
            plan=self.plan,
            travel_matrix=demo_matrix(),
            order=self.plan.user_baseline_order(),
        )
        self.assertEqual(result.solution.user_baseline.duration_sec, baseline.metrics.duration_sec)
        self.assertEqual(result.solution.user_baseline.distance_m, baseline.metrics.distance_m)
        self.assertNotEqual(
            result.solution.user_baseline.baseline_kind,
            result.solution.algorithm_baseline.baseline_kind,
        )

    def test_a_route_request_appends_no_run(self) -> None:
        self.services.routes.committed_route(DEMO_PLAN_ID)
        self.assertEqual(self.services.routes.list_runs(DEMO_PLAN_ID), ())

    def test_no_selection_means_no_route(self) -> None:
        self.services.selections.clear_first_stop(DEMO_PLAN_ID)
        with self.assertRaises(NoFirstStopSelected) as caught:
            self.services.routes.committed_route(DEMO_PLAN_ID)
        self.assertEqual(caught.exception.code, "no_first_stop_selected")
        with self.assertRaises(NoFirstStopSelected):
            self.services.routes.optimize_and_record(DEMO_PLAN_ID)
        self.assertEqual(self.services.routes.list_runs(DEMO_PLAN_ID), ())

    def test_an_unknown_plan_is_refused(self) -> None:
        with self.assertRaises(UnknownPlan):
            self.services.routes.committed_route("nope")
        with self.assertRaises(UnknownPlan):
            self.services.routes.list_runs("nope")


class RunRecordingTests(SharedDemoPlanTestCase):
    """``optimize_and_record`` appends exactly one immutable row per recalculation (U11/D38)."""

    def setUp(self) -> None:
        super().setUp()
        self.plan = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "recommend", self.recommended
        ).plan

    def test_the_first_recalculation_is_an_optimize_run(self) -> None:
        result = self.services.routes.optimize_and_record(DEMO_PLAN_ID)
        run = result.run
        self.assertEqual(len(self.services.routes.list_runs(DEMO_PLAN_ID)), 1)
        self.assertEqual(run.run_kind, RunKind.OPTIMIZE)
        self.assertEqual(run.plan_id, DEMO_PLAN_ID)
        self.assertEqual(run.order, result.solution.order)
        self.assertEqual(run.violations, result.solution.violations)
        self.assertEqual(run.inputs_fingerprint, self.plan.inputs_fingerprint())
        self.assertEqual(run.route_fingerprint, result.route_fingerprint)
        self.assertEqual(run.cost_policy.name, smart_route_elapsed_policy().name)
        self.assertEqual(run.data_provenance, DataProvenance.DEMO_SYNTHETIC)
        self.assertEqual(run.status, RunStatus.OK)
        self.assertEqual(run.metrics.after, result.solution.metrics)
        self.assertEqual(run.metrics.algorithm_baseline, result.solution.algorithm_baseline)
        self.assertEqual(run.metrics.user_baseline.baseline_kind.value, "user_supplied")
        # ``created_at_utc`` is when the row was created, not the plan's departure time (a
        # different fact that stays on the plan); the pinned-clock case is covered below.
        self.assertNotEqual(run.created_at_utc, self.plan.departure_time)
        self.assertEqual(run.created_at_utc.microsecond, 0)
        self.assertIs(run.created_at_utc.tzinfo, timezone.utc)
        self.assertEqual(run.recommendation.ranked_stop_ids[0], self.recommended)
        self.assertEqual(run.recommendation.recommended_stop_id, run.top_k[0].stop_id)
        self.assertEqual(run.recommendation.status, RecommendationStatus.RECOMMENDED)
        self.assertEqual(len(run.top_k), 10)
        self.assertEqual(run.top_k[0].stop_id, self.recommended)
        # The run records the ENGINE's ranked list, so the stored ranking is exactly a direct
        # engine call's ranking on the same stored plan - and both facts of the row (what was
        # recommended and what the driver committed) coexist.
        direct = evaluate_first_stop_candidates(plan=self.plan, travel_matrix=demo_matrix())
        self.assertEqual(run.recommendation.ranked_stop_ids, direct.ranked_ids())
        self.assertEqual(run.recommendation.recommended_stop_id, direct.recommended_stop_id)
        self.assertEqual(run.top_k_stop_ids, direct.ranked_ids()[: len(run.top_k)])
        self.assertEqual(run.order[0], self.recommended)

    def test_the_tzdata_version_recorded_is_the_one_in_use(self) -> None:
        from core.time import tzdata

        run = self.services.routes.optimize_and_record(DEMO_PLAN_ID).run
        self.assertEqual(run.tzdata_version, tzdata.tzdata_version())

    def test_a_second_recalculation_appends_a_second_row_and_leaves_the_first(self) -> None:
        first = self.services.routes.optimize_and_record(DEMO_PLAN_ID).run
        second = self.services.routes.optimize_and_record(DEMO_PLAN_ID).run
        self.assertEqual(second.run_kind, RunKind.REOPTIMIZE)
        self.assertNotEqual(first.id, second.id)
        runs = self.services.routes.list_runs(DEMO_PLAN_ID)
        self.assertEqual([run.id for run in runs], [first.id, second.id])
        # The first row is history: it is exactly what it was before the second run.
        self.assertEqual(runs[0], first)
        self.assertEqual(self.services.routes.get_run(first.id), first)
        self.assertEqual(self.services.routes.get_run(second.id), second)

    def test_an_unknown_run_id_is_refused(self) -> None:
        self.services.routes.optimize_and_record(DEMO_PLAN_ID)
        with self.assertRaises(NotFound):
            self.services.routes.get_run("run-does-not-exist")

    def test_the_recommendation_recorded_is_history_not_plan_state(self) -> None:
        run = self.services.routes.optimize_and_record(DEMO_PLAN_ID).run
        self.assertIsNotNone(run.recommendation.recommended_stop_id)
        stored = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.assertEqual(stored.first_service_stop.selected_stop_id, self.recommended)
        self.assertEqual(
            stored.first_service_stop.selection_source, SelectionSource.ACCEPTED_RECOMMENDATION
        )

    def test_a_manual_choice_records_an_honest_recommendation_payload(self) -> None:
        """The recorded recommendation is the engine's own ranking, never a re-written one.

        The driver's manual choice changes which stop is committed first; it never changes what the
        engine recommended, so the stored ranking stays inside the committed route (the route visits
        exactly the enabled stops) and names the stop the engine actually recommended.
        """
        manual = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "manual", MANUAL_STOP
        ).plan
        run = self.services.routes.optimize_and_record(DEMO_PLAN_ID).run
        self.assertEqual(run.order[0], MANUAL_STOP)
        self.assertEqual(manual.first_service_stop.selected_stop_id, MANUAL_STOP)
        for stop_id in run.recommendation.ranked_stop_ids:
            self.assertIn(stop_id, run.order)
        if run.recommendation.is_available:
            self.assertIn(run.recommendation.recommended_stop_id, run.order)


class LastRankedManualChoiceTests(ServiceEngineTestCase):
    """The recorded recommendation and the driver's committed order are two different facts (D32).

    The driver manually selects the stop the engine ranks **last**, so the committed route starts
    somewhere the recommendation does not put first. Both facts must survive in the row: the engine's
    own recommendation (with ``top_k`` as its head) and the driver's committed ``order``.
    """

    def test_the_run_records_the_engines_ranking_and_the_drivers_first_stop(self) -> None:
        self.seed_demo_plan()
        report = evaluate_first_stop_candidates(
            plan=self.services.plans.get_plan(DEMO_PLAN_ID).plan, travel_matrix=demo_matrix()
        )
        last_ranked = report.ranked[-1].stop_id
        self.assertNotEqual(last_ranked, report.recommended_stop_id)

        selected = self.services.selections.select_first_stop(
            DEMO_PLAN_ID, "manual", last_ranked
        ).plan
        result = self.services.routes.optimize_and_record(DEMO_PLAN_ID)
        run = result.run
        direct = evaluate_first_stop_candidates(plan=selected, travel_matrix=demo_matrix())

        # Both facts of the row coexist; neither overwrites the other.
        self.assertEqual(run.order[0], last_ranked)
        self.assertEqual(selected.first_service_stop.selected_stop_id, last_ranked)
        self.assertEqual(run.recommendation.recommended_stop_id, direct.recommended_stop_id)
        self.assertNotEqual(run.recommendation.recommended_stop_id, run.order[0])

        # The recommendation is the engine's own ranking on the same plan ...
        self.assertEqual(run.recommendation.status, direct.status)
        self.assertEqual(run.recommendation.ranked_stop_ids, direct.ranked_ids())
        self.assertEqual(run.recommendation.inputs_fingerprint, direct.inputs_fingerprint)
        self.assertEqual(run.recommendation.diagnostics, direct.diagnostics)

        # ... and the recorded recommendation, its top-K and the run's order never disagree.
        self.assertEqual(run.recommendation.recommended_stop_id, run.top_k[0].stop_id)
        self.assertEqual(
            run.top_k_stop_ids, run.recommendation.ranked_stop_ids[: len(run.top_k)]
        )
        self.assertEqual(
            run.top_k_stop_ids, direct.ranked_ids()[: len(run.top_k)]
        )
        self.assertEqual(len(self.services.routes.list_runs(DEMO_PLAN_ID)), 1)


class NonDemoTravelMatrixTests(ServiceEngineTestCase):
    """The stored run metrics come from the engine and the CONFIGURED matrix (D22/D38).

    The recorded user baseline is the one the engine produced for this service's travel matrix. It
    is never re-derived with the demo fixture: a configured matrix that is not ``demo_matrix()``
    must be priced through, not replaced - and the run's baseline must agree with the route
    endpoint's baseline for the same selection.
    """

    services_kwargs = {"travel_matrix": ScaledDemoMatrix()}

    def test_the_recorded_user_baseline_matches_the_route_endpoint(self) -> None:
        self.seed_demo_plan()
        self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        result = self.services.routes.optimize_and_record(DEMO_PLAN_ID)
        route = self.services.routes.committed_route(DEMO_PLAN_ID)

        self.assertEqual(len(self.services.routes.list_runs(DEMO_PLAN_ID)), 1)
        self.assertEqual(
            result.run.metrics.user_baseline, route.solution.user_baseline
        )
        self.assertEqual(result.run.metrics.after, route.solution.metrics)
        self.assertEqual(
            result.run.metrics.algorithm_baseline, route.solution.algorithm_baseline
        )

        # The configured matrix really priced the run: the demo fixture would have given different
        # numbers, which is exactly what the old cross-check against ``demo_matrix()`` compared.
        plan = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        demo_baseline = evaluate_order(
            plan=plan, travel_matrix=demo_matrix(), order=plan.user_baseline_order()
        ).metrics
        self.assertNotEqual(
            route.solution.user_baseline.duration_sec, demo_baseline.duration_sec
        )
        self.assertEqual(route.solution.order[0], MANUAL_STOP)


class RunCreatedAtTests(ServiceEngineTestCase):
    """``created_at_utc`` is when the row was created, from the container's injectable clock."""

    services_kwargs = {"clock": lambda: FIXED_NOW}

    def test_the_pinned_clock_is_recorded_verbatim_and_survives_storage(self) -> None:
        self.seed_demo_plan()
        plan = self.services.plans.get_plan(DEMO_PLAN_ID).plan
        self.services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        run = self.services.routes.optimize_and_record(DEMO_PLAN_ID).run
        self.assertEqual(run.created_at_utc, FIXED_NOW)
        self.assertNotEqual(run.created_at_utc, plan.departure_time)
        # The stored row is the one that was appended, and reading it back keeps the instant.
        self.assertEqual(self.services.routes.get_run(run.id).created_at_utc, FIXED_NOW)

    def test_a_sub_second_clock_is_stored_as_the_whole_second_it_will_be_written_as(self) -> None:
        """The storage convention is whole seconds; the seam truncates to it, never silently later."""
        services = ApiServices(
            new_database_file(self.scratch),
            clock=lambda: FIXED_NOW.replace(microsecond=123456),
        )
        self.addCleanup(services.close)
        services.plans.create_demo_plan({})
        services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        run = services.routes.optimize_and_record(DEMO_PLAN_ID).run
        self.assertEqual(run.created_at_utc, FIXED_NOW)
        self.assertEqual(run.created_at_utc.microsecond, 0)
        self.assertIsNotNone(run.created_at_utc.tzinfo)

    def test_a_naive_clock_is_refused_instead_of_assumed_to_be_utc(self) -> None:
        services = ApiServices(
            new_database_file(self.scratch), clock=lambda: datetime(2026, 9, 11, 12, 34, 56)
        )
        self.addCleanup(services.close)
        services.plans.create_demo_plan({})
        services.selections.select_first_stop(DEMO_PLAN_ID, "manual", MANUAL_STOP)
        with self.assertRaises(StorageError):
            services.routes.optimize_and_record(DEMO_PLAN_ID)


class RunIdTests(unittest.TestCase):
    """Run ids are API-assigned, deterministic and collision-free across recalculation."""

    def test_the_same_recalculation_names_the_same_row(self) -> None:
        first = run_id_for(
            plan_id="p", run_kind=RunKind.OPTIMIZE, route_fingerprint="f" * 64, sequence=0
        )
        again = run_id_for(
            plan_id="p", run_kind=RunKind.OPTIMIZE, route_fingerprint="f" * 64, sequence=0
        )
        self.assertEqual(first, again)
        self.assertTrue(first.startswith("run-"))

    def test_two_recalculations_cannot_collide(self) -> None:
        ids = {
            run_id_for(
                plan_id="p",
                run_kind=RunKind.OPTIMIZE if index == 0 else RunKind.REOPTIMIZE,
                route_fingerprint="f" * 64,
                sequence=index,
            )
            for index in range(6)
        }
        self.assertEqual(len(ids), 6)


class SingleFlightTests(ServiceEngineTestCase):
    """The per-plan computation lock: one exhaustive loop at a time, honestly refused otherwise."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_demo_plan()

    def test_a_held_lock_makes_every_computation_plan_busy(self) -> None:
        lock = self.services.recommendations.plan_lock(DEMO_PLAN_ID)
        self.assertTrue(lock.acquire(0))
        try:
            for call in (
                lambda: self.services.recommendations.recommend(DEMO_PLAN_ID),
                lambda: self.services.routes.committed_route(DEMO_PLAN_ID),
                lambda: self.services.routes.optimize_and_record(DEMO_PLAN_ID),
                lambda: self.services.selections.select_first_stop(
                    DEMO_PLAN_ID, "manual", MANUAL_STOP
                ),
            ):
                with self.subTest(call=call):
                    with self.assertRaises(PlanBusy) as caught:
                        call()
                    self.assertEqual(caught.exception.code, "plan_busy")
                    self.assertIn("no background job queue", str(caught.exception))
        finally:
            lock.release()
        # The refused requests wrote nothing at all.
        self.assertEqual(self.services.routes.list_runs(DEMO_PLAN_ID), ())
        self.assertIsNone(
            self.services.plans.get_plan(DEMO_PLAN_ID).plan.first_service_stop.selected_stop_id
        )

    def test_the_lock_is_released_after_a_successful_computation(self) -> None:
        self.services.recommendations.recommend(DEMO_PLAN_ID)
        self.assertFalse(self.services.recommendations.plan_lock(DEMO_PLAN_ID).locked())

    def test_reads_do_not_take_the_computation_lock(self) -> None:
        lock = self.services.routes.plan_lock(DEMO_PLAN_ID)
        self.assertTrue(lock.acquire(0))
        try:
            self.assertEqual(self.services.routes.list_runs(DEMO_PLAN_ID), ())
            self.assertEqual(self.services.plans.get_plan(DEMO_PLAN_ID).plan.id, DEMO_PLAN_ID)
        finally:
            lock.release()


class ServiceConstructionTests(unittest.TestCase):
    """The container wires the engine-facing services and keeps the U13 surface intact."""

    def test_the_three_engine_services_are_exposed(self) -> None:
        scratch = new_scratch_directory("api-u14-wiring")
        self.addCleanup(shutil.rmtree, scratch, True)
        services = ApiServices(new_database_file(scratch))
        self.addCleanup(services.close)
        self.assertIsInstance(services.recommendations, RecommendationService)
        self.assertIsInstance(services.selections, SelectionService)
        self.assertIsInstance(services.routes, RouteService)
        self.assertIsNotNone(services.plans)
        self.assertIsNotNone(services.settings)
        # The U13 surface still works on the same container.
        self.assertEqual(services.plans.create_demo_plan({}).plan.id, DEMO_PLAN_ID)


if __name__ == "__main__":
    unittest.main()
