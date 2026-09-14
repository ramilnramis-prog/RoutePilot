"""Stage 3 U11 (D38): immutable optimization-run history.

Covers the approved schema's run table (``docs/STORAGE_SCHEMA.md`` section 5) through the domain
record ``core.model.optimization_run`` and the SQLite repository
``storage.sqlite.optimization_run_repository``:

* exact round-trip of a complete run - every typed field, the three stored ``RouteMetrics``
  baselines, explicit violations, the route order, the recommendation payload, the top-K candidate
  payload and the stored savings - and of a minimal run with ``tzdata_version`` and ``top_k``
  absent;
* the payloads are also built from **real engine output** (the exhaustive first-stop evaluation
  report and a committed ``RouteSolution``), so the round-trip is not only a fixture echo;
* append-only history: two appends keep both rows, ``list_for_plan`` returns them in the documented
  deterministic order (``created_at_utc`` then insertion order for equal timestamps), ``latest`` is
  the newest run, and the repository exposes no update and no delete API at all;
* a run for an unknown plan fails loudly and creates no plan; a duplicate run id is never an
  overwrite;
* loud load-time validation (D38 acceptance item 5): hand-edited ``metrics_json``, ``order_json``,
  ``violations_json``, recommendation payload, top-K payload, stored enum, cost policy or timestamp
  raises the error type the domain (or, for malformed stored bytes, storage) owns - a
  calendar-invalid timestamp raises the storage error, never a bare ``ValueError``;
* no database file is created: every database here is ``:memory:``.

Deterministic and offline: no wall clock (the run's own timestamp is always pinned), no files, no
network.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from typing import Any

from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.engine.optimizer.solve import solve_route
from core.model.cost_policy import (
    ComponentStatus,
    CostComponent,
    RouteCostPolicy,
    smart_route_elapsed_policy,
)
from core.model.first_stop import (
    CandidateDiagnostic,
    CandidateMetrics,
    FirstStopCandidate,
    FirstStopIntent,
    RecommendationStatus,
)
from core.model.ids import PlanId, StopId
from core.model.optimization_run import (
    ALGORITHM_NAME,
    ALGORITHM_VERSION,
    OptimizationRun,
    OptimizationRunMetrics,
    OptimizationRunRecommendation,
    RunKind,
    RunStatus,
    decode_route_metrics_json,
    encode_route_metrics_json,
    encode_run_metrics_json,
    run_metrics_from_solution,
)
from core.model.solution import (
    BaselineKind,
    RouteMetrics,
    RouteSolution,
    Violation,
    ViolationKind,
)
from core.model.value_objects import DataProvenance
from core.repositories import RouteOptimizationRunRepository
from core.validation.errors import (
    InvalidCostPolicyError,
    InvalidOptimizationRunError,
    InvalidRoutePlanError,
    UnsupportedFeatureError,
    ValidationError,
)
from storage import StoredRunError, StorageError
from storage.sqlite.database import connect, migrate
from storage.sqlite.optimization_run_repository import SqliteRouteOptimizationRunRepository
from tests.support import FixedTravelMatrix, build_plan, stop

PROVENANCE = DataProvenance.DEMO_SYNTHETIC


def at(hour: int, minute: int = 0, second: int = 0, day: int = 11) -> datetime:
    """A UTC instant on the fixture date; the tests never read a wall clock."""
    return datetime(2026, 9, day, hour, minute, second, tzinfo=timezone.utc)


#: The run's own timestamp; every fixture pins it so the stored text is exact.
CREATED_AT = at(5)
_RESOLVED_AT = at(4)

_INPUTS_FINGERPRINT = "inputs-fingerprint-1"
_ROUTE_FINGERPRINT = "route-fingerprint-1"


def route_metrics(
    *,
    travel_sec: int,
    service_sec: int,
    waiting_sec: int = 0,
    distance_m: float,
    feasible: bool = True,
    baseline_kind: BaselineKind | None = None,
    finish_arrival: datetime = CREATED_AT,
) -> RouteMetrics:
    """A complete-route metrics value whose duration is exactly travel + waiting + service.

    ``duration_sec`` keeps its :class:`RouteMetrics` meaning - the whole elapsed route duration,
    service time included (v2 section 15) - so the stored metrics are the domain's own numbers.
    """
    return RouteMetrics(
        distance_m=distance_m,
        duration_sec=travel_sec + waiting_sec + service_sec,
        waiting_sec=waiting_sec,
        travel_sec=travel_sec,
        service_sec=service_sec,
        finish_arrival=finish_arrival,
        feasible=feasible,
        baseline_kind=baseline_kind,
    )


def run_metrics(
    *,
    user_distance_m: float = 12000.0,
    user_travel_sec: int = 3600,
    user_service_sec: int = 600,
    user_waiting_sec: int = 0,
    algorithm_distance_m: float = 11000.0,
    algorithm_travel_sec: int = 3300,
    algorithm_service_sec: int = 600,
    algorithm_waiting_sec: int = 60,
    after_distance_m: float = 10000.0,
    after_travel_sec: int = 3000,
    after_service_sec: int = 600,
    after_waiting_sec: int = 30,
    after_feasible: bool = True,
) -> OptimizationRunMetrics:
    """The stored ``metrics_json`` value: the driver's BEFORE, the internal benchmark, the route."""
    return OptimizationRunMetrics.of(
        user_baseline=route_metrics(
            travel_sec=user_travel_sec,
            service_sec=user_service_sec,
            waiting_sec=user_waiting_sec,
            distance_m=user_distance_m,
            baseline_kind=BaselineKind.USER_SUPPLIED,
        ),
        algorithm_baseline=route_metrics(
            travel_sec=algorithm_travel_sec,
            service_sec=algorithm_service_sec,
            waiting_sec=algorithm_waiting_sec,
            distance_m=algorithm_distance_m,
            baseline_kind=BaselineKind.ALGORITHM_GREEDY,
        ),
        after=route_metrics(
            travel_sec=after_travel_sec,
            service_sec=after_service_sec,
            waiting_sec=after_waiting_sec,
            distance_m=after_distance_m,
            feasible=after_feasible,
        ),
    )


def candidate(
    stop_id: str,
    *,
    complete_travel_time: int = 3000,
    waiting_time: int = 30,
    distance_m: float = 10000.0,
    total_service_time: int = 600,
) -> FirstStopCandidate:
    """One fully feasible ranked candidate, consistent with its own measured breakdown."""
    return FirstStopCandidate(
        stop_id=StopId(stop_id),
        travel_time=1200,
        estimated_arrival=at(2),
        waiting_time=waiting_time,
        lateness=0,
        estimated_complete_route_duration=(complete_travel_time + waiting_time + total_service_time),
        feasible=True,
        service_window_start=None,
        score=float(complete_travel_time + waiting_time),
        explanation=(
            (CostComponent.TRAVEL_TIME.value, float(complete_travel_time)),
            (CostComponent.WAITING_TIME.value, float(waiting_time)),
            (CostComponent.DISTANCE.value, float(distance_m)),
        ),
        complete_travel_time=complete_travel_time,
        complete_waiting_time=waiting_time,
        total_service_time=total_service_time,
        estimated_finish=at(3),
        estimated_service_start=at(2, 0, 30),
        violating_stop_ids=(),
        max_lateness=0,
        metrics=CandidateMetrics(
            travel_sec=complete_travel_time,
            waiting_sec=waiting_time,
            distance_m=distance_m,
        ),
    )


def ranked_recommendation() -> OptimizationRunRecommendation:
    """A positive recommendation over two known stops: what the run showed (never a decision)."""
    return OptimizationRunRecommendation(
        status=RecommendationStatus.RECOMMENDED,
        recommended_stop_id=StopId("b"),
        ranked_stop_ids=(StopId("b"), StopId("a")),
        resolved_at=_RESOLVED_AT,
        inputs_fingerprint=_INPUTS_FINGERPRINT,
    )


def make_run(
    run_id: str = "run-1",
    *,
    plan_id: str = "plan-1",
    created_at_utc: datetime = CREATED_AT,
    run_kind: RunKind = RunKind.OPTIMIZE,
    order: tuple[str, ...] = ("b", "a"),
    recommendation: OptimizationRunRecommendation | None = None,
    top_k: tuple[FirstStopCandidate, ...] | None = None,
    violations: tuple[Violation, ...] = (),
    metrics: OptimizationRunMetrics | None = None,
    status: RunStatus | None = None,
    tzdata_version: str | None = "2026a",
    data_provenance: DataProvenance = PROVENANCE,
    cost_policy: RouteCostPolicy | None = None,
) -> OptimizationRun:
    """A complete, valid run by default; every stored field is overridable for one scenario."""
    if recommendation is None:
        recommendation = ranked_recommendation()
    if top_k is None and recommendation.is_available:
        top_k = (candidate("b"), candidate("a", complete_travel_time=3100, distance_m=10500.0))
    if status is None:
        status = RunStatus.OK if not violations else RunStatus.HAS_INFEASIBLE_WINDOWS
    return OptimizationRun(
        id=run_id,
        plan_id=PlanId(plan_id),
        run_kind=run_kind,
        algorithm=ALGORITHM_NAME,
        algorithm_version=ALGORITHM_VERSION,
        inputs_fingerprint=_INPUTS_FINGERPRINT,
        route_fingerprint=_ROUTE_FINGERPRINT,
        cost_policy=cost_policy if cost_policy is not None else smart_route_elapsed_policy(),
        data_provenance=data_provenance,
        status=status,
        order=tuple(StopId(value) for value in order),
        recommendation=recommendation,
        violations=violations,
        metrics=metrics if metrics is not None else run_metrics(),
        created_at_utc=created_at_utc,
        tzdata_version=tzdata_version,
        top_k=top_k,
    )


def sub_second_run_metrics() -> OptimizationRunMetrics:
    """A metrics payload whose route instant carries microseconds the stored text cannot hold.

    ``finish_arrival`` is the one instant inside a stored route metrics payload, and the whole-second
    convention cannot represent the fraction, so this payload must be refused rather than truncated.
    """
    return OptimizationRunMetrics.of(
        user_baseline=route_metrics(
            travel_sec=3600,
            service_sec=600,
            distance_m=12000.0,
            baseline_kind=BaselineKind.USER_SUPPLIED,
        ),
        algorithm_baseline=route_metrics(
            travel_sec=3300,
            service_sec=600,
            waiting_sec=60,
            distance_m=11000.0,
            baseline_kind=BaselineKind.ALGORITHM_GREEDY,
        ),
        after=route_metrics(
            travel_sec=3000,
            service_sec=600,
            waiting_sec=30,
            distance_m=10000.0,
            finish_arrival=CREATED_AT.replace(microsecond=1),
        ),
    )


def make_minimal_run(run_id: str = "run-minimal", *, plan_id: str = "plan-1") -> OptimizationRun:
    """A run with the driver's first stop still unresolved: no route, no top-K, no tzdata."""
    return OptimizationRun(
        id=run_id,
        plan_id=PlanId(plan_id),
        run_kind=RunKind.PREVIEW,
        algorithm=ALGORITHM_NAME,
        algorithm_version=ALGORITHM_VERSION,
        inputs_fingerprint=_INPUTS_FINGERPRINT,
        route_fingerprint=_ROUTE_FINGERPRINT,
        cost_policy=smart_route_elapsed_policy(),
        data_provenance=PROVENANCE,
        status=RunStatus.UNRESOLVED_FIRST_STOP,
        order=(),
        recommendation=OptimizationRunRecommendation(
            status=RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE
        ),
        violations=(),
        metrics=run_metrics(),
        created_at_utc=CREATED_AT,
    )


class RunRepositoryTestCase(unittest.TestCase):
    """Shared ``:memory:`` database, approved schema, plan rows and run repository."""

    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        self.repository = SqliteRouteOptimizationRunRepository(self.connection)
        self.plan_id = "plan-1"
        self.save_plan(self.plan_id)

    # -- fixtures -------------------------------------------------------- #
    def save_plan(self, plan_id: str, *stop_ids: str) -> None:
        """A stored plan row written with raw SQL: U10 owns the plan write path, not this module.

        Only the columns the run's foreign key needs are written, so the fixture stays honest and
        does not borrow another unit's repository (and its ``data_provenance`` guard).
        """
        ids = stop_ids or (plan_id,)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO route_plans (
                    id, timezone, departure_label, departure_latitude, departure_longitude,
                    departure_time_utc, finish_label, finish_latitude, finish_longitude,
                    route_mode, first_stop_mode, window_end_policy, order_overrides_json,
                    cost_policy_json, data_provenance, created_at_utc, updated_at_utc
                ) VALUES (?, 'Europe/Moscow', 'Warehouse', 55.75, 37.62, '2026-09-11T01:00:00Z',
                          'Depot', 55.70, 37.55, 'SMART_ROUTE', 'recommend',
                          'service_finish_before_end', '{"version": 1, "constraints": []}',
                          '{"name": "smart_route_elapsed_v1", "weights": {}, "notes": "",
                            "provisional": false}',
                          'DEMO_SYNTHETIC', '2026-09-11T00:00:00Z', '2026-09-11T00:00:00Z')
                """,
                (plan_id,),
            )
            for index, stop_id in enumerate(ids):
                self.connection.execute(
                    """
                    INSERT INTO route_stops (
                        id, plan_id, input_position, raw_address, geocode_status,
                        service_window_kind, service_status, enabled,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, 'pending', 'unrestricted', 'pending', 1,
                              '2026-09-11T00:00:00Z', '2026-09-11T00:00:00Z')
                    """,
                    (f"{plan_id}-{stop_id}", plan_id, index, f"{stop_id} street 1"),
                )

    def sql(self, query: str, *parameters: Any) -> list[sqlite3.Row]:
        return list(self.connection.execute(query, parameters).fetchall())

    def scalar(self, query: str, *parameters: Any) -> Any:
        return self.connection.execute(query, parameters).fetchone()[0]

    def run_rows(self) -> list[sqlite3.Row]:
        return self.sql("SELECT * FROM route_optimization_runs ORDER BY created_at_utc, rowid")

    def stored_text(self, run_id: str, column: str) -> str:
        return self.scalar(
            f"SELECT {column} FROM route_optimization_runs WHERE id = ?", run_id
        )

    def mutate_run(self, run_id: str, **assignments: Any) -> None:
        """Hand-edit a legally stored row, to prove the load path refuses it (D38 item 5)."""
        columns = ", ".join(f"{column} = ?" for column in assignments)
        with self.connection:
            self.connection.execute(
                f"UPDATE route_optimization_runs SET {columns} WHERE id = ?",
                (*assignments.values(), run_id),
            )


# --------------------------------------------------------------------------- #
# round-trip
# --------------------------------------------------------------------------- #
class RoundTripTests(RunRepositoryTestCase):
    def test_a_complete_run_round_trips_exactly(self) -> None:
        run = make_run("run-complete")
        self.repository.append(run)

        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertEqual(loaded, run)
        self.assertEqual(loaded.id, "run-complete")
        self.assertEqual(loaded.plan_id, self.plan_id)
        self.assertIs(loaded.run_kind, RunKind.OPTIMIZE)
        self.assertEqual(loaded.algorithm, ALGORITHM_NAME)
        self.assertEqual(loaded.algorithm_version, ALGORITHM_VERSION)
        self.assertEqual(loaded.inputs_fingerprint, _INPUTS_FINGERPRINT)
        self.assertEqual(loaded.route_fingerprint, _ROUTE_FINGERPRINT)
        self.assertEqual(loaded.tzdata_version, "2026a")
        self.assertIs(loaded.data_provenance, PROVENANCE)
        self.assertIs(loaded.status, RunStatus.OK)
        self.assertEqual(list(loaded.order), [StopId("b"), StopId("a")])
        self.assertEqual(loaded.created_at_utc, CREATED_AT)
        self.assertEqual(loaded.created_at_utc.tzinfo, timezone.utc)
        self.assertEqual(loaded.cost_policy, smart_route_elapsed_policy())

        # The three baselines survive as the domain's own metrics objects, labels included.
        self.assertIs(loaded.metrics.user_baseline.baseline_kind, BaselineKind.USER_SUPPLIED)
        self.assertIs(
            loaded.metrics.algorithm_baseline.baseline_kind, BaselineKind.ALGORITHM_GREEDY
        )
        self.assertIsNone(loaded.metrics.after.baseline_kind)
        self.assertEqual(loaded.metrics, run.metrics)
        self.assertEqual(loaded.metrics.user_baseline.distance_m, 12000.0)
        self.assertEqual(loaded.metrics.user_baseline.duration_sec, 4200)
        self.assertEqual(loaded.metrics.algorithm_baseline.distance_m, 11000.0)
        self.assertEqual(loaded.metrics.saved_distance_m, 2000.0)
        self.assertEqual(loaded.metrics.saved_duration_sec, 570)

        # The recommendation is history: ids in rank order, never a selection on any plan.
        self.assertEqual(loaded.recommendation, run.recommendation)
        self.assertEqual(list(loaded.recommendation.ranked_stop_ids), [StopId("b"), StopId("a")])
        self.assertEqual(loaded.recommendation.recommended_stop_id, StopId("b"))
        self.assertEqual(loaded.recommendation.resolved_at, _RESOLVED_AT)
        self.assertEqual(loaded.recommendation.inputs_fingerprint, _INPUTS_FINGERPRINT)

        # The top-K payload is every field of the candidate, including its measured breakdown.
        self.assertEqual(loaded.top_k, run.top_k)
        self.assertEqual(loaded.top_k_stop_ids, (StopId("b"), StopId("a")))
        self.assertEqual(loaded.top_k[0].metrics, run.top_k[0].metrics)
        self.assertEqual(loaded.top_k[1].score, run.top_k[1].score)
        self.assertEqual(loaded.top_k[0].explanation, run.top_k[0].explanation)

        # A recommendation was stored as history and never as plan state (D32 item 8).
        self.assertIsNone(
            self.scalar("SELECT first_stop_selected_stop_id FROM route_plans WHERE id = ?", self.plan_id)
        )

    def test_an_infeasible_run_round_trips_with_its_violations(self) -> None:
        violation = Violation(
            stop_id=StopId("a"),
            kind=ViolationKind.TIME_WINDOW_INFEASIBLE,
            message="stop a cannot be served before its window closes",
            service_start=at(2, 30),
            service_window_end=at(2),
        )
        run = make_run(
            "run-infeasible",
            recommendation=OptimizationRunRecommendation(
                status=RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE,
                diagnostics=(
                    CandidateDiagnostic(
                        stop_id=StopId("a"),
                        code=ViolationKind.TIME_WINDOW_INFEASIBLE.value,
                        message=violation.message,
                        candidate_stop_id=StopId("a"),
                        reason="candidate first stop a misses a hard window",
                        violation_kind=ViolationKind.TIME_WINDOW_INFEASIBLE,
                    ),
                ),
            ),
            top_k=None,
            violations=(violation,),
            metrics=run_metrics(
                after_distance_m=9000.0,
                after_travel_sec=2900,
                after_waiting_sec=120,
                after_feasible=False,
            ),
        )
        self.repository.append(run)

        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertEqual(loaded, run)
        self.assertIs(loaded.status, RunStatus.HAS_INFEASIBLE_WINDOWS)
        self.assertEqual(loaded.violations, (violation,))
        self.assertEqual(loaded.violations[0].service_start, at(2, 30))
        self.assertEqual(loaded.violations[0].service_window_end, at(2))
        self.assertEqual(loaded.recommendation.diagnostics, run.recommendation.diagnostics)
        self.assertIs(
            loaded.recommendation.diagnostics[0].violation_kind,
            ViolationKind.TIME_WINDOW_INFEASIBLE,
        )
        self.assertIsNone(loaded.top_k)

    def test_a_minimal_run_without_the_optional_fields_round_trips(self) -> None:
        run = make_minimal_run()
        self.repository.append(run)

        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertEqual(loaded, run)
        self.assertIsNone(loaded.tzdata_version)
        self.assertIsNone(loaded.top_k)
        self.assertEqual(loaded.order, ())
        self.assertFalse(loaded.has_committed_route)
        self.assertIs(loaded.status, RunStatus.UNRESOLVED_FIRST_STOP)
        row = self.run_rows()[0]
        self.assertIsNone(row["tzdata_version"])
        self.assertIsNone(row["top_k_json"])

    def test_every_run_kind_and_provenance_round_trips(self) -> None:
        cases = (
            (RunKind.REOPTIMIZE, DataProvenance.DEMO_SYNTHETIC),
            (RunKind.PREVIEW, DataProvenance.REAL_ROUTING),
            (RunKind.OPTIMIZE, DataProvenance.REAL_ROUTING),
        )
        for index, (kind, provenance) in enumerate(cases):
            self.repository.append(
                make_run(
                    f"run-{index}",
                    created_at_utc=at(5, 0, index),
                    run_kind=kind,
                    data_provenance=provenance,
                )
            )
        loaded = self.repository.list_for_plan(self.plan_id)
        self.assertEqual([run.run_kind for run in loaded], [case[0] for case in cases])
        self.assertEqual([run.data_provenance for run in loaded], [case[1] for case in cases])

    def test_the_payloads_of_real_engine_output_round_trip(self) -> None:
        """The stored payloads are built from the engine's own report and solution, not a fixture."""
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            stop("c", 55.84, 37.74),
            plan_id=self.plan_id,
            first_service_stop=FirstStopIntent.recommend(),
        )
        matrix = FixedTravelMatrix()

        report = evaluate_first_stop_candidates(plan=plan, travel_matrix=matrix)
        self.assertIs(report.status, RecommendationStatus.RECOMMENDED)
        recommendation = report.to_recommendation()
        top_two = report.top(2)
        self.assertEqual(len(top_two), 2)

        committed = solve_route(
            plan=build_plan(
                stop("a", 55.80, 37.70),
                stop("b", 55.82, 37.72),
                stop("c", 55.84, 37.74),
                plan_id=self.plan_id,
                first_service_stop=FirstStopIntent.manual_choice(report.ranked[0].stop_id),
            ),
            travel_matrix=matrix,
        )
        self.assertIsInstance(committed, RouteSolution)

        stored_metrics = run_metrics_from_solution(committed)
        run = make_run(
            "run-engine",
            order=tuple(committed.order),
            recommendation=OptimizationRunRecommendation.of(recommendation),
            top_k=top_two,
            violations=committed.violations,
            metrics=stored_metrics,
        )
        self.repository.append(run)

        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertEqual(loaded, run)
        # The recommendation stores every ranked id; the top-K stores the candidates that were shown.
        self.assertEqual(
            list(loaded.recommendation.ranked_stop_ids), list(report.ranked_ids())
        )
        self.assertEqual(
            [candidate.stop_id for candidate in loaded.top_k], list(report.ranked_ids()[:2])
        )
        self.assertEqual(loaded.top_k, top_two)
        self.assertEqual(loaded.top_k[0].metrics, top_two[0].metrics)
        self.assertEqual(loaded.metrics, stored_metrics)
        self.assertEqual(loaded.order, committed.order)
        self.assertIs(loaded.metrics.user_baseline.baseline_kind, BaselineKind.USER_SUPPLIED)
        self.assertIs(
            loaded.metrics.algorithm_baseline.baseline_kind, BaselineKind.ALGORITHM_GREEDY
        )
        self.assertEqual(loaded.violations, committed.violations)


# --------------------------------------------------------------------------- #
# exact instants: the whole-second storage convention is never silently truncated
# --------------------------------------------------------------------------- #
class SubSecondInstantTests(RunRepositoryTestCase):
    """A sub-second instant cannot be represented, so it is refused before any row is written.

    The stored text is ``YYYY-MM-DDTHH:MM:SSZ`` (schema section 1), so a truncating write would break
    the exact round-trip this repository promises. The refusal is the same rule the U10 plan
    repository applies to a sub-second ``departure_time``.
    """

    def test_a_sub_second_created_at_utc_is_refused_and_writes_no_row(self) -> None:
        for microsecond in (1, 500_000, 999_999):
            with self.subTest(microsecond=microsecond):
                run = make_run(
                    f"run-{microsecond}",
                    created_at_utc=CREATED_AT.replace(microsecond=microsecond),
                )
                with self.assertRaises(StoredRunError) as caught:
                    self.repository.append(run)
                message = str(caught.exception)
                self.assertIn("created_at_utc", message)
                self.assertIn("sub-second", message)
                self.assertIn(run.id, message)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)

    def test_a_sub_second_instant_inside_a_payload_is_refused_and_writes_no_row(self) -> None:
        run = make_run("run-sub-second", metrics=sub_second_run_metrics(), top_k=None)
        with self.assertRaises(InvalidOptimizationRunError) as caught:
            self.repository.append(run)
        message = str(caught.exception)
        self.assertIn("finish_arrival", message)
        self.assertIn("sub-second", message)
        self.assertIn(CREATED_AT.replace(microsecond=1).isoformat(), message)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)

    def test_a_whole_second_run_still_round_trips_exactly(self) -> None:
        run = make_run("run-whole")
        self.repository.append(run)

        self.assertEqual(
            self.scalar(
                "SELECT created_at_utc FROM route_optimization_runs WHERE id = 'run-whole'"
            ),
            "2026-09-11T05:00:00Z",
        )
        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertEqual(loaded, run)
        self.assertEqual(loaded.created_at_utc, CREATED_AT)
        self.assertEqual(loaded.created_at_utc.microsecond, 0)
        self.assertEqual(
            loaded.metrics.after.finish_arrival,
            CREATED_AT,
            "the payload instant must survive the round trip unchanged",
        )
        self.assertEqual(
            self.stored_text("run-whole", "metrics_json"),
            encode_run_metrics_json(run.metrics),
        )
        self.assertIn(
            '"finish_arrival":"2026-09-11T05:00:00Z"',
            self.stored_text("run-whole", "metrics_json"),
        )


# --------------------------------------------------------------------------- #
# the run record's own cross-field rule: a recommendation belongs to its route
# --------------------------------------------------------------------------- #
class RecommendationInsideRunTests(RunRepositoryTestCase):
    """D32: a run's recommendation may only name stops of that run's own committed order."""

    def outside_order_recommendation(self) -> OptimizationRunRecommendation:
        """A well-formed recommendation that names a stop this run never committed."""
        return OptimizationRunRecommendation(
            status=RecommendationStatus.RECOMMENDED,
            recommended_stop_id=StopId("zzz"),
            ranked_stop_ids=(StopId("zzz"),),
            resolved_at=_RESOLVED_AT,
            inputs_fingerprint=_INPUTS_FINGERPRINT,
        )

    def contradictory_run(self) -> OptimizationRun:
        """A run that marks one of its own ranked candidates as a violating stop (v2 section 14).

        ``status='has_infeasible_windows'`` (derived by :func:`make_run` from the stored violation),
        order ``('b', 'a')``, a recommendation ranking ``('b', 'a')`` and a violation for stop
        ``'b'``: an infeasible complete route is never a ranked candidate (v2 section 14, D32).
        """
        return make_run(
            "run-contradictory",
            order=("b", "a"),
            recommendation=ranked_recommendation(),
            violations=(
                Violation(
                    stop_id=StopId("b"),
                    kind=ViolationKind.TIME_WINDOW_INFEASIBLE,
                    message="stop b cannot be served before its window closes",
                    service_start=at(2, 30),
                    service_window_end=at(2),
                ),
            ),
            metrics=run_metrics(after_feasible=False),
        )

    def test_constructing_a_run_whose_recommendation_leaves_the_order_is_refused(self) -> None:
        with self.assertRaises(InvalidOptimizationRunError) as caught:
            make_run(
                "run-outside",
                recommendation=self.outside_order_recommendation(),
                top_k=(candidate("zzz"),),
            )
        message = str(caught.exception)
        self.assertIn("outside its own order", message)
        self.assertIn("zzz", message)
        self.assertIn("D32", message)

    def test_appending_it_is_refused_and_writes_no_row(self) -> None:
        with self.assertRaises(InvalidOptimizationRunError):
            self.repository.append(
                make_run("run-outside", recommendation=self.outside_order_recommendation())
            )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)

    def test_constructing_and_appending_a_run_that_ranks_a_violating_stop_is_refused(self) -> None:
        """The exact contradictory case is refused before any SQL runs, so nothing is stored.

        Enforcing this only on the read path let ``append()`` store a run that ``list_for_plan()``
        then refused forever, which is the 'append must not be able to store unreadable history'
        rule this class already proves for the recommendation/order rule (v2 section 14, D32).
        """
        with self.assertRaises(InvalidOptimizationRunError) as constructed:
            self.contradictory_run()
        message = str(constructed.exception)
        self.assertIn("never a ranked candidate", message)
        self.assertIn("'b'", message)
        self.assertIn("v2 section 14", message)

        with self.assertRaises(InvalidOptimizationRunError):
            self.repository.append(self.contradictory_run())
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)

    def test_a_stored_row_edited_to_rank_a_violating_stop_still_fails_loudly_on_load(self) -> None:
        """The same rule on the way out: the read-side pass is the hand-edited-row defence."""
        self.repository.append(make_run("run-1"))
        self.mutate_run(
            "run-1",
            status=RunStatus.HAS_INFEASIBLE_WINDOWS.value,
            violations_json=json.dumps(
                [
                    {
                        "kind": ViolationKind.TIME_WINDOW_INFEASIBLE.value,
                        "stop_id": "b",
                        "message": "stop b cannot be served before its window closes",
                        "service_start": None,
                        "service_window_end": None,
                    }
                ]
            ),
        )
        with self.assertRaises(StoredRunError) as caught:
            self.repository.list_for_plan(self.plan_id)
        self.assertIn("never a ranked candidate", str(caught.exception))

    def test_a_stored_row_edited_to_break_the_rule_still_fails_loudly_on_load(self) -> None:
        """The same rule, enforced on the way out of storage as well as on the way in."""
        self.repository.append(make_run("run-1"))
        payload = json.loads(self.stored_text("run-1", "first_stop_recommendation_json"))
        payload["recommended_stop_id"] = "zzz"
        payload["ranked_stop_ids"] = ["zzz"]
        self.mutate_run(
            "run-1",
            first_stop_recommendation_json=json.dumps(payload),
            top_k_json=json.dumps(
                [
                    {**json.loads(self.stored_text("run-1", "top_k_json"))[0], "stop_id": "zzz"},
                ]
            ),
        )
        with self.assertRaises(StoredRunError) as caught:
            self.repository.list_for_plan(self.plan_id)
        self.assertIn("outside its own order", str(caught.exception))


# --------------------------------------------------------------------------- #
# the standalone route-metrics codec
# --------------------------------------------------------------------------- #
class RouteMetricsCodecTests(unittest.TestCase):
    """``decode_route_metrics_json`` is the documented reverse of its encoder."""

    def test_a_whole_second_route_metrics_round_trips(self) -> None:
        metrics = route_metrics(
            travel_sec=3000,
            service_sec=600,
            waiting_sec=30,
            distance_m=10000.0,
            baseline_kind=BaselineKind.ALGORITHM_GREEDY,
        )
        text = encode_route_metrics_json(metrics)
        self.assertEqual(decode_route_metrics_json(text), metrics)
        self.assertIn('"finish_arrival":"2026-09-11T05:00:00Z"', text)

    def test_malformed_stored_text_raises_the_domain_error(self) -> None:
        for text in ("{not json", "[1, 2]", '"a string"', "", "   "):
            with self.subTest(text=text):
                with self.assertRaises(InvalidOptimizationRunError):
                    decode_route_metrics_json(text)

    def test_stored_text_with_a_missing_key_raises_the_domain_error(self) -> None:
        payload = json.loads(
            encode_route_metrics_json(
                route_metrics(travel_sec=3000, service_sec=600, distance_m=10000.0)
            )
        )
        del payload["waiting_sec"]
        with self.assertRaises(InvalidOptimizationRunError) as caught:
            decode_route_metrics_json(json.dumps(payload))
        self.assertIn("missing", str(caught.exception))

    def test_a_calendar_invalid_stored_instant_raises_the_domain_error(self) -> None:
        payload = json.loads(
            encode_route_metrics_json(
                route_metrics(travel_sec=3000, service_sec=600, distance_m=10000.0)
            )
        )
        payload["finish_arrival"] = "2026-02-30T05:00:00Z"
        with self.assertRaises(InvalidOptimizationRunError):
            decode_route_metrics_json(json.dumps(payload))


# --------------------------------------------------------------------------- #
# append-only history
# --------------------------------------------------------------------------- #
class AppendOnlyTests(RunRepositoryTestCase):
    def test_two_appends_keep_both_rows_and_list_them_deterministically(self) -> None:
        self.repository.append(make_run("run-1", created_at_utc=at(5)))
        self.repository.append(make_run("run-2", created_at_utc=at(6)))

        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 2)
        loaded = self.repository.list_for_plan(self.plan_id)
        self.assertEqual([run.id for run in loaded], ["run-1", "run-2"])
        # Repeated reads agree: the order is a property of the data, not of the read.
        self.assertEqual(
            [run.id for run in self.repository.list_for_plan(self.plan_id)], ["run-1", "run-2"]
        )

    def test_equal_timestamps_keep_the_append_order(self) -> None:
        """Storage keeps whole seconds, so equal timestamps are normal, not exceptional."""
        self.repository.append(make_run("first", created_at_utc=CREATED_AT, order=("b", "a")))
        self.repository.append(make_run("second", created_at_utc=CREATED_AT, order=("a", "b")))
        self.repository.append(make_run("third", created_at_utc=CREATED_AT, order=("b", "a")))

        loaded = self.repository.list_for_plan(self.plan_id)
        self.assertEqual([run.id for run in loaded], ["first", "second", "third"])
        self.assertEqual(self.repository.latest(self.plan_id).id, "third")

    def test_latest_is_the_newest_run_or_none(self) -> None:
        self.assertIsNone(self.repository.latest(self.plan_id))
        self.assertIsNone(self.repository.latest("plan-without-runs"))

        self.repository.append(make_run("older", created_at_utc=at(5)))
        self.repository.append(make_run("newer", created_at_utc=at(7)))
        self.repository.append(make_run("middle", created_at_utc=at(6)))

        latest = self.repository.latest(self.plan_id)
        self.assertEqual(latest.id, "newer")
        # ``latest`` is exactly the run ``list_for_plan`` puts last, in every ordering case.
        self.assertEqual(self.repository.list_for_plan(self.plan_id)[-1], latest)

    def test_runs_of_other_plans_are_not_returned(self) -> None:
        self.save_plan("plan-2")
        self.repository.append(make_run("run-1", plan_id=self.plan_id))
        self.repository.append(make_run("run-other", plan_id="plan-2"))

        self.assertEqual(
            [run.id for run in self.repository.list_for_plan(self.plan_id)], ["run-1"]
        )
        self.assertEqual(
            [run.id for run in self.repository.list_for_plan("plan-2")], ["run-other"]
        )
        self.assertEqual(self.repository.latest("plan-2").id, "run-other")

    def test_an_unknown_plan_has_no_runs_and_gains_none(self) -> None:
        self.assertEqual(self.repository.list_for_plan("ghost"), ())
        self.assertIsNone(self.repository.latest("ghost"))
        self.assertEqual(self.sql("SELECT * FROM route_plans WHERE id = 'ghost'"), [])

    def test_the_repository_has_no_update_and_no_delete_api(self) -> None:
        for forbidden in ("update", "delete", "remove", "save", "replace", "clear", "delete_run"):
            with self.subTest(method=forbidden):
                self.assertFalse(hasattr(self.repository, forbidden))
        self.assertEqual(
            {name for name in dir(self.repository) if not name.startswith("_")},
            {"append", "list_for_plan", "latest"},
        )

    def test_a_second_append_of_the_same_run_id_is_refused_not_an_overwrite(self) -> None:
        self.repository.append(make_run("run-1", order=("b", "a")))
        with self.assertRaises(StoredRunError) as caught:
            self.repository.append(make_run("run-1", order=("a", "b")))
        self.assertIn("already stored", str(caught.exception))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 1)
        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertEqual(list(loaded.order), [StopId("b"), StopId("a")])

    def test_a_run_for_an_unknown_plan_fails_loudly_and_creates_nothing(self) -> None:
        with self.assertRaises(StoredRunError) as caught:
            self.repository.append(make_run("run-ghost", plan_id="no-such-plan"))
        message = str(caught.exception)
        self.assertIn("no plan", message)
        self.assertIn("never creates one", message)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM route_plans WHERE id = 'no-such-plan'"), 0
        )

    def test_the_port_is_satisfied_by_the_sqlite_repository(self) -> None:
        self.assertIsInstance(self.repository, RouteOptimizationRunRepository)

    def test_append_needs_a_run(self) -> None:
        with self.assertRaises(StorageError) as caught:
            self.repository.append({"id": "run-1"})  # type: ignore[arg-type]
        self.assertIn("OptimizationRun", str(caught.exception))


# --------------------------------------------------------------------------- #
# loud load-time validation
# --------------------------------------------------------------------------- #
class StoredPayloadValidationTests(RunRepositoryTestCase):
    """Every hand-edited stored payload must fail with the error its owner defines."""

    def setUp(self) -> None:
        super().setUp()
        self.repository.append(make_run("run-1"))

    def assert_load_fails(self, expected: type[Exception], *, contains: str = "") -> None:
        with self.assertRaises(expected) as caught:
            self.repository.list_for_plan(self.plan_id)
        if contains:
            self.assertIn(contains, str(caught.exception))

    def assert_load_raises(self, expected: type[Exception], *, contains: str = "") -> None:
        """Assert the read fails with the error's *category*: the domain's or storage's (D26).

        Malformed JSON can fail inside the JSON parser (a domain error) or before it, so the test
        pins the owner - ``core.validation`` or ``storage`` - rather than guessing which.
        """
        with self.assertRaises(expected) as caught:
            self.repository.list_for_plan(self.plan_id)
        if contains:
            self.assertIn(contains, str(caught.exception))

    def test_malformed_metrics_json_is_refused(self) -> None:
        self.mutate_run("run-1", metrics_json="{not json")
        self.assert_load_raises(ValidationError, contains="metrics_json is not valid JSON")

    def test_metrics_json_with_an_unknown_key_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        payload["unknown_baseline"] = payload["after"]
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="unknown")

    def test_metrics_json_with_a_missing_baseline_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        del payload["algorithm_baseline"]
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="missing")

    def test_a_saving_that_contradicts_the_baselines_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        payload["saved_distance_m"] = 1.0
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="saved_distance_m")

    def test_a_baseline_carrying_the_wrong_label_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        payload["algorithm_baseline"]["baseline_kind"] = BaselineKind.USER_SUPPLIED.value
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="algorithm_baseline")

    def test_a_negative_stored_metric_is_refused_by_the_domain(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        payload["after"]["waiting_sec"] = -30
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidRoutePlanError, contains="waiting_sec")

    def test_a_non_integer_stored_metric_is_refused_by_the_domain(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        payload["after"]["travel_sec"] = 3000.5
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="whole seconds")

    def test_a_malformed_stored_instant_inside_the_payload_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "metrics_json"))
        payload["after"]["finish_arrival"] = "2026-09-11 05:00:00"
        self.mutate_run("run-1", metrics_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="finish_arrival")

    def test_malformed_order_json_is_refused(self) -> None:
        self.mutate_run("run-1", order_json="not json at all")
        self.assert_load_raises(ValidationError, contains="order_json is not valid JSON")

    def test_order_json_that_is_not_an_array_is_refused(self) -> None:
        self.mutate_run("run-1", order_json=json.dumps({"order": ["b", "a"]}))
        self.assert_load_fails(InvalidOptimizationRunError, contains="order_json")

    def test_order_json_with_a_blank_stop_id_is_refused(self) -> None:
        self.mutate_run("run-1", order_json=json.dumps(["b", "  "]))
        self.assert_load_fails(InvalidOptimizationRunError, contains="order_json")

    def test_order_json_that_empties_a_committed_run_is_refused(self) -> None:
        self.mutate_run("run-1", order_json="[]")
        self.assert_load_fails(InvalidOptimizationRunError, contains="unresolved_first_stop")

    def test_malformed_violations_json_is_refused(self) -> None:
        self.mutate_run("run-1", violations_json="[}")
        self.assert_load_raises(ValidationError, contains="violations_json is not valid JSON")

    def test_a_violation_with_an_unknown_kind_is_refused(self) -> None:
        self.mutate_run(
            "run-1",
            violations_json=json.dumps(
                [
                    {
                        "kind": "not_a_violation_kind",
                        "stop_id": "a",
                        "message": "boom",
                        "service_start": None,
                        "service_window_end": None,
                    }
                ]
            ),
            status=RunStatus.HAS_INFEASIBLE_WINDOWS.value,
        )
        self.assert_load_fails(InvalidOptimizationRunError, contains="violations_json[0].kind")

    def test_a_violation_without_a_message_is_refused_by_the_domain(self) -> None:
        self.mutate_run(
            "run-1",
            violations_json=json.dumps(
                [
                    {
                        "kind": ViolationKind.TIME_WINDOW_INFEASIBLE.value,
                        "stop_id": "a",
                        "message": "",
                        "service_start": None,
                        "service_window_end": None,
                    }
                ]
            ),
            status=RunStatus.HAS_INFEASIBLE_WINDOWS.value,
        )
        self.assert_load_fails(InvalidRoutePlanError, contains="message")

    def test_a_status_of_infeasible_windows_without_violations_is_refused(self) -> None:
        self.mutate_run("run-1", status=RunStatus.HAS_INFEASIBLE_WINDOWS.value)
        self.assert_load_fails(InvalidOptimizationRunError, contains="explicit violation")

    def test_a_status_of_ok_with_violations_is_refused(self) -> None:
        self.mutate_run(
            "run-1",
            violations_json=json.dumps(
                [
                    {
                        "kind": ViolationKind.TIME_WINDOW_INFEASIBLE.value,
                        "stop_id": "a",
                        "message": "late",
                        "service_start": None,
                        "service_window_end": None,
                    }
                ]
            ),
        )
        self.assert_load_fails(InvalidOptimizationRunError, contains="status='ok'")

    def test_malformed_recommendation_json_is_refused(self) -> None:
        self.mutate_run("run-1", first_stop_recommendation_json="{oops")
        self.assert_load_raises(
            ValidationError, contains="first_stop_recommendation_json is not valid JSON"
        )

    def test_a_recommendation_with_an_unknown_status_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "first_stop_recommendation_json"))
        payload["status"] = "definitely_recommended"
        self.mutate_run("run-1", first_stop_recommendation_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="status")

    def test_a_recommendation_with_a_missing_key_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "first_stop_recommendation_json"))
        del payload["ranked_stop_ids"]
        self.mutate_run("run-1", first_stop_recommendation_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="missing")

    def test_a_recommendation_for_a_stop_outside_the_order_is_refused(self) -> None:
        """The run's own rule couples the recommendation to the route it belongs to (D32)."""
        payload = json.loads(self.stored_text("run-1", "first_stop_recommendation_json"))
        payload["recommended_stop_id"] = "zzz"
        payload["ranked_stop_ids"] = ["zzz"]
        self.mutate_run(
            "run-1",
            first_stop_recommendation_json=json.dumps(payload),
            # The stored candidates follow the top-K column, so the ranked head stays coherent and
            # only the route reference is broken - which is the rule under test.
            top_k_json=json.dumps(
                [
                    {**json.loads(self.stored_text("run-1", "top_k_json"))[0], "stop_id": "zzz"},
                ]
            ),
        )
        with self.assertRaises(StoredRunError) as caught:
            self.repository.list_for_plan(self.plan_id)
        self.assertIn("outside its own order", str(caught.exception))

    def test_malformed_top_k_json_is_refused(self) -> None:
        self.mutate_run("run-1", top_k_json="[1")
        self.assert_load_raises(ValidationError, contains="top_k_json is not valid JSON")

    def test_top_k_with_an_unknown_candidate_key_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "top_k_json"))
        payload[0]["estimated_total"] = 1
        self.mutate_run("run-1", top_k_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="unknown")

    def test_top_k_that_is_not_the_ranked_head_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "top_k_json"))
        payload.reverse()
        self.mutate_run("run-1", top_k_json=json.dumps(payload))
        self.assert_load_fails(InvalidOptimizationRunError, contains="rank order")

    def test_a_candidate_contradicting_its_own_feasibility_is_refused_by_the_domain(self) -> None:
        payload = json.loads(self.stored_text("run-1", "top_k_json"))
        payload[0]["feasible"] = False
        self.mutate_run("run-1", top_k_json=json.dumps(payload))
        self.assert_load_fails(InvalidRoutePlanError, contains="feasible")

    def test_top_k_with_a_non_text_column_is_refused(self) -> None:
        self.mutate_run("run-1", top_k_json=7)
        self.assert_load_fails(InvalidOptimizationRunError, contains="top_k_json")

    def test_a_json_column_holding_a_blob_is_refused(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE route_optimization_runs SET order_json = X'00' WHERE id = 'run-1'"
            )
        self.assert_load_fails(StoredRunError, contains="order_json")

    def test_malformed_cost_policy_json_is_refused(self) -> None:
        self.mutate_run("run-1", cost_policy_json="not json")
        self.assert_load_fails(StoredRunError, contains="cost_policy_json")

    def test_an_unknown_stored_cost_policy_name_is_refused_by_the_domain(self) -> None:
        payload = json.loads(self.stored_text("run-1", "cost_policy_json"))
        payload["name"] = "mystery_policy_v9"
        self.mutate_run("run-1", cost_policy_json=json.dumps(payload))
        self.assert_load_fails(InvalidCostPolicyError, contains="mystery_policy_v9")

    def test_an_unknown_stored_cost_component_is_refused_by_the_domain(self) -> None:
        payload = json.loads(self.stored_text("run-1", "cost_policy_json"))
        payload["weights"] = {"teleportation_penalty": 1.0}
        self.mutate_run("run-1", cost_policy_json=json.dumps(payload))
        self.assert_load_fails(InvalidCostPolicyError, contains="teleportation_penalty")

    def test_a_weight_for_a_non_implemented_component_is_refused(self) -> None:
        payload = json.loads(self.stored_text("run-1", "cost_policy_json"))
        payload["weights"] = {CostComponent.U_TURN_PENALTY.value: 2.0}
        self.mutate_run("run-1", cost_policy_json=json.dumps(payload))
        self.assert_load_fails(UnsupportedFeatureError, contains="u_turn_penalty")
        self.assertIs(ComponentStatus.REQUIRES_PROVIDER.value, "requires_provider")

    def test_an_empty_required_column_is_refused(self) -> None:
        """Every NOT NULL column is also required to be non-empty by the run record itself."""
        with self.connection:
            self.connection.execute(
                "UPDATE route_optimization_runs SET algorithm = '' WHERE id = 'run-1'"
            )
        self.assert_load_fails(InvalidOptimizationRunError, contains="algorithm")

    def test_the_schema_refuses_a_null_not_null_column(self) -> None:
        """The DDL's own NOT NULL constraint is the first line of defence for a required column."""
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                self.connection.execute(
                    "UPDATE route_optimization_runs SET algorithm = NULL WHERE id = 'run-1'"
                )

    def test_append_refuses_a_cost_policy_this_build_does_not_implement(self) -> None:
        policy = RouteCostPolicy(
            name="not_a_registered_policy",
            weights={CostComponent.TRAVEL_TIME: 1.0},
        )
        with self.assertRaises(StorageError) as caught:
            self.repository.append(make_run("run-policy", cost_policy=policy))
        self.assertIn("not a policy this build implements", str(caught.exception))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 1)


# --------------------------------------------------------------------------- #
# stored enums and timestamps
# --------------------------------------------------------------------------- #
class StoredEnumAndTimestampTests(RunRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository.append(make_run("run-1"))

    def test_the_schema_itself_refuses_an_unknown_stored_enum(self) -> None:
        """The first line of defence is the approved DDL's own CHECK constraints (schema section 5).

        The mutation is attempted through raw SQL exactly as a hand-edit would, and the database
        refuses it; the tests above then prove the domain rejects the same values a second time when
        a row does reach the reader.
        """
        for column, value in (
            ("run_kind", "teleport"),
            ("status", "probably_fine"),
            ("data_provenance", "MAYBE_REAL"),
        ):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.connection:
                        self.connection.execute(
                            f"UPDATE route_optimization_runs SET {column} = ? WHERE id = 'run-1'",
                            (value,),
                        )
        # Nothing was written, so the stored row is still exactly what append() produced.
        (loaded,) = self.repository.list_for_plan(self.plan_id)
        self.assertIs(loaded.run_kind, RunKind.OPTIMIZE)
        self.assertIs(loaded.status, RunStatus.OK)

    def test_the_domain_record_refuses_an_unknown_enum_before_any_storage(self) -> None:
        """The same three values are rejected by the pure record, with no database involved."""
        for field, value in (
            ("run_kind", "teleport"),
            ("status", "probably_fine"),
            ("data_provenance", "MAYBE_REAL"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(InvalidOptimizationRunError) as caught:
                    dataclasses.replace(make_run("run-1"), **{field: value})
                self.assertIn(field, str(caught.exception))
                self.assertIn(value, str(caught.exception))

    def test_a_calendar_invalid_stored_timestamp_raises_the_storage_error(self) -> None:
        self.mutate_run("run-1", created_at_utc="2026-02-30T01:00:00Z")
        with self.assertRaises(StoredRunError) as caught:
            self.repository.list_for_plan(self.plan_id)
        self.assertIn("not a real calendar date", str(caught.exception))
        # The interpreter's own ValueError must never escape the read path.
        self.assertNotIsInstance(caught.exception, ValueError)

    def test_a_stored_timestamp_with_an_offset_is_refused(self) -> None:
        for text in ("2026-09-11T05:00:00+03:00", "2026-09-11 05:00:00", "2026-09-11T05:00:00"):
            with self.subTest(text=text):
                self.mutate_run("run-1", created_at_utc=text)
                with self.assertRaises(StoredRunError) as caught:
                    self.repository.latest(self.plan_id)
                self.assertIn("trailing Z", str(caught.exception))

    def test_a_calendar_invalid_timestamp_also_fails_on_latest(self) -> None:
        self.mutate_run("run-1", created_at_utc="2026-13-01T00:00:00Z")
        with self.assertRaises(StoredRunError):
            self.repository.latest(self.plan_id)


# --------------------------------------------------------------------------- #
# construction and database hygiene
# --------------------------------------------------------------------------- #
class ConstructionTests(RunRepositoryTestCase):
    def test_a_connection_without_row_factory_is_refused(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(StorageError):
            SqliteRouteOptimizationRunRepository(connection)

    def test_a_database_without_the_run_table_is_refused(self) -> None:
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(StorageError) as caught:
            SqliteRouteOptimizationRunRepository(connection)
        self.assertIn("route_optimization_runs", str(caught.exception))

    def test_no_database_file_is_created(self) -> None:
        self.repository.append(make_run("run-1"))
        self.assertEqual(self.connection.execute("PRAGMA database_list").fetchone()[2], "")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 1)

    def test_the_plan_cascade_still_removes_runs(self) -> None:
        """Deleting a plan is U10's port, but the cascade the run table declares must hold."""
        self.repository.append(make_run("run-1"))
        with self.connection:
            self.connection.execute("DELETE FROM route_plans WHERE id = ?", (self.plan_id,))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)
