"""Complete-route evaluation and the three baselines (Stage 2 unit U1; v2 sections 12/15/21/30).

What is locked in here:

* a complete route includes the FINISH leg, so ``duration_sec == travel + waiting + service``
  and moving FINISH further away really moves the metrics;
* START is never a service stop and FINISH never appears in an order;
* every enabled stop is visited exactly once and disabled stops are excluded;
* a missed hard window is an explicit ``Violation`` with ``feasible=False`` and solution status
  ``has_infeasible_windows`` - never a numeric penalty inside the metrics;
* the USER baseline is the ``input_position`` order and ignores the driver's selection;
* a committed route needs an explicit selection and keeps it first (I3/I4, D32);
* identical inputs produce identical metrics and fingerprints.

All travel data is synthetic (``DEMO_SYNTHETIC``) and is never presented as road routing.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, time, timedelta, timezone

from core.engine.optimizer import (
    RouteEvaluation,
    build_solution,
    evaluate_order,
    solve,
    user_baseline_order,
)
from core.model.first_stop import FirstStopIntent, FirstStopMode
from core.model.ids import StopId
from core.model.service_window import ServiceWindow
from core.model.solution import (
    BaselineKind,
    RouteMetrics,
    SolutionStatus,
    ViolationKind,
)
from core.validation.errors import (
    InvalidOrderError,
    InvalidRoutePlanError,
)
from tests.support import WAREHOUSE, FixedTravelMatrix, build_plan, place, point, stop, utc

STOP_A = StopId("A")
STOP_B = StopId("B")
STOP_C = StopId("C")
STOP_OFF = StopId("OFF")

#: The synthetic warehouse and depot as points (``WAREHOUSE`` is the raw coordinate pair).
START = point(*WAREHOUSE)
DEPOT = point(55.70, 37.55)


def chosen(stop_id: StopId, *, mode: FirstStopMode = FirstStopMode.RECOMMEND) -> FirstStopIntent:
    """The driver's explicit choice of the first service stop (D6/D32)."""
    return FirstStopIntent.manual_choice(stop_id, mode=mode)


def three_stop_plan(**kwargs):
    """Three enabled stops along one line, plus one disabled stop. Input order == A, B, C."""
    return build_plan(
        stop("A", WAREHOUSE[0], WAREHOUSE[1]),
        stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25),
        stop("C", WAREHOUSE[0], WAREHOUSE[1] - 0.50),
        stop("OFF", WAREHOUSE[0], WAREHOUSE[1] - 0.75, enabled=False),
        **kwargs,
    )


def _secs(seconds: int) -> timedelta:
    """A timedelta of whole seconds, for comparing instants without magic numbers."""
    return timedelta(seconds=seconds)


class FinishLegTests(unittest.TestCase):
    """v2 section 15: the final leg to FINISH is part of the complete route."""

    def test_metrics_equal_travel_waiting_service_including_finish_leg(self) -> None:
        plan = build_plan(stop("A", WAREHOUSE[0], WAREHOUSE[1] - 0.25))
        matrix = FixedTravelMatrix()

        result = evaluate_order(plan=plan, travel_matrix=matrix, order=[STOP_A])

        stop_a = point(55.75, 37.37)
        travel = matrix.travel_time_seconds(START, stop_a)
        leg_to_finish = matrix.travel_time_seconds(stop_a, DEPOT)
        expected_distance = matrix.distance_meters(START, stop_a) + matrix.distance_meters(
            stop_a, DEPOT
        )

        self.assertEqual(result.metrics.travel_sec, travel + leg_to_finish)
        self.assertEqual(result.metrics.waiting_sec, 0)
        self.assertEqual(result.metrics.service_sec, 600)
        self.assertEqual(
            result.metrics.duration_sec,
            result.metrics.travel_sec + result.metrics.waiting_sec + result.metrics.service_sec,
        )
        self.assertAlmostEqual(result.metrics.distance_m, expected_distance)
        self.assertEqual(
            result.metrics.finish_arrival,
            plan.departure_time + _secs(result.metrics.duration_sec),
        )
        self.assertTrue(result.feasible)
        self.assertTrue(result.metrics.feasible)
        self.assertIsNone(result.metrics.baseline_kind)

    def test_finish_leg_is_really_included(self) -> None:
        near = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1] - 0.25),
            finish=place("Depot", 55.70, 37.55),
        )
        far = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1] - 0.25),
            finish=place("Far depot", 50.00, 37.55),
        )
        matrix = FixedTravelMatrix()
        stop_a = point(55.75, 37.37)

        near_result = evaluate_order(plan=near, travel_matrix=matrix, order=[STOP_A])
        far_result = evaluate_order(plan=far, travel_matrix=matrix, order=[STOP_A])

        extra = matrix.travel_time_seconds(stop_a, point(50.00, 37.55)) - (
            matrix.travel_time_seconds(stop_a, DEPOT)
        )
        self.assertGreater(extra, 0)
        self.assertEqual(
            far_result.metrics.travel_sec, near_result.metrics.travel_sec + extra
        )
        self.assertEqual(
            far_result.metrics.duration_sec, near_result.metrics.duration_sec + extra
        )
        self.assertEqual(
            far_result.metrics.finish_arrival,
            near_result.metrics.finish_arrival + _secs(extra),
        )
        # Moving FINISH only moves the route's end, never a service stop.
        self.assertEqual(far_result.order, near_result.order)
        self.assertEqual(
            [timeline.estimated_departure for timeline in far_result.timelines],
            [timeline.estimated_departure for timeline in near_result.timelines],
        )

    def test_empty_order_travels_only_to_finish(self) -> None:
        plan = build_plan()
        matrix = FixedTravelMatrix()

        result = evaluate_order(plan=plan, travel_matrix=matrix, order=[])

        leg_to_finish = matrix.travel_time_seconds(START, DEPOT)
        self.assertEqual(result.order, ())
        self.assertEqual(result.timelines, ())
        self.assertEqual(result.violations, ())
        self.assertEqual(result.metrics.travel_sec, leg_to_finish)
        self.assertEqual(result.metrics.waiting_sec, 0)
        self.assertEqual(result.metrics.service_sec, 0)
        self.assertEqual(result.metrics.duration_sec, leg_to_finish)
        self.assertEqual(
            result.metrics.finish_arrival,
            plan.departure_time + _secs(leg_to_finish),
        )
        self.assertAlmostEqual(
            result.metrics.distance_m, matrix.distance_meters(START, DEPOT)
        )

    def test_finish_arrival_is_timezone_aware_utc(self) -> None:
        # A stop two hours from the warehouse plus a two-hour return leg: exactly 4h of driving,
        # so the same instant printed in the plan's zone is visibly not UTC.
        plan = build_plan(
            stop("A", WAREHOUSE[0] + 2.0, WAREHOUSE[1]),
            finish=place("Depot", *WAREHOUSE),
        )
        result = evaluate_order(plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A])

        finish_arrival = result.metrics.finish_arrival
        self.assertEqual(result.metrics.travel_sec, 4 * 3600)
        self.assertEqual(result.metrics.service_sec, 600)
        self.assertIsNotNone(finish_arrival.tzinfo)
        self.assertEqual(finish_arrival.utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(finish_arrival, plan.departure_time + _secs(4 * 3600 + 600))
        # Europe/Moscow is UTC+3, and no wall-clock value ever leaks into storage (D2).
        self.assertEqual(
            finish_arrival.astimezone(plan.load_timezone()).hour, finish_arrival.hour + 3
        )


class RouteShapeTests(unittest.TestCase):
    """START/FINISH and stop-set invariants of a complete route (I1/I2, spec section 27)."""

    def test_order_visits_every_enabled_stop_once_and_excludes_disabled(self) -> None:
        plan = three_stop_plan()
        result = evaluate_order(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=[STOP_A, STOP_B, STOP_C],
        )

        self.assertEqual(result.order, (STOP_A, STOP_B, STOP_C))
        self.assertEqual(
            [timeline.stop_id for timeline in result.timelines], [STOP_A, STOP_B, STOP_C]
        )
        self.assertEqual(sorted(timeline.stop_id for timeline in result.timelines), sorted(
            some_stop.id for some_stop in plan.active_stops()
        ))
        self.assertNotIn(STOP_OFF, result.order)
        self.assertEqual(
            [some_stop.id for some_stop in plan.disabled_stops()], [STOP_OFF]
        )

    def test_start_is_never_a_service_stop_and_finish_never_appears_in_order(self) -> None:
        plan = three_stop_plan()
        result = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A, STOP_B, STOP_C]
        )

        self.assertNotIn(plan.departure.label, result.order)
        self.assertNotIn(plan.finish.label, result.order)
        self.assertEqual(len(result.order), len(result.timelines))
        self.assertEqual(len(result.order), len(set(result.order)))
        # START and FINISH are PlaceRefs, never stops: they cannot be ordered or timed.
        self.assertNotIn(plan.departure, plan.stops)
        self.assertNotIn(plan.finish, plan.stops)
        self.assertTrue(
            all(
                timeline.stop_id not in (plan.departure.label, plan.finish.label)
                for timeline in result.timelines
            )
        )

    def test_order_with_a_disabled_stop_is_rejected(self) -> None:
        plan = three_stop_plan()
        with self.assertRaises(InvalidOrderError):
            evaluate_order(
                plan=plan,
                travel_matrix=FixedTravelMatrix(),
                order=[STOP_A, STOP_B, STOP_C, STOP_OFF],
            )

    def test_order_that_omits_a_stop_is_rejected(self) -> None:
        plan = three_stop_plan()
        with self.assertRaises(InvalidOrderError):
            evaluate_order(
                plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A, STOP_B]
            )


class InfeasibilityTests(unittest.TestCase):
    """A hard window miss is explicit data, never a number inside the metrics (D13 amendment)."""

    def _late_plan(self):
        """Two stops; stop A is served first, which makes B's 03:30-04:30 window impossible."""
        matrix = FixedTravelMatrix()
        late_arrival = matrix.travel_time_seconds(START, point(55.75, 37.37))
        return (
            build_plan(
                stop("A", WAREHOUSE[0], WAREHOUSE[1]),
                stop(
                    "B",
                    WAREHOUSE[0],
                    WAREHOUSE[1] - 0.25,
                    window=ServiceWindow.fixed(time(3, 30), time(4, 30)),
                ),
                first_service_stop=chosen(STOP_A),
            ),
            late_arrival,
        )

    def test_hard_window_violation_is_explicit_and_marks_the_route_infeasible(self) -> None:
        plan, _ = self._late_plan()
        result = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A, STOP_B]
        )

        self.assertFalse(result.feasible)
        self.assertFalse(result.metrics.feasible)
        self.assertEqual(result.infeasible_stop_ids, (STOP_B,))
        self.assertEqual(len(result.violations), 1)
        violation = result.violations[0]
        self.assertEqual(violation.stop_id, STOP_B)
        self.assertIs(violation.kind, ViolationKind.TIME_WINDOW_INFEASIBLE)
        self.assertTrue(violation.message)
        # Stop A itself is perfectly feasible: only B's window is missed.
        self.assertTrue(result.timelines[0].is_infeasible is False)

    def test_infeasible_route_produces_solution_status_has_infeasible_windows(self) -> None:
        plan, _ = self._late_plan()
        full_order = [STOP_A, STOP_B]

        solution = build_solution(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=full_order,
            algorithm_order=full_order,
        )

        self.assertIs(solution.status, SolutionStatus.HAS_INFEASIBLE_WINDOWS)
        self.assertTrue(solution.has_infeasible_windows)
        self.assertFalse(solution.metrics.feasible)
        self.assertEqual([v.stop_id for v in solution.violations], [STOP_B])
        self.assertEqual(solution.infeasible_stop_ids(), (STOP_B,))

    def test_no_numeric_penalty_field_is_used_for_infeasibility(self) -> None:
        plan, _ = self._late_plan()
        matrix = FixedTravelMatrix()
        order = [STOP_A, STOP_B]

        result = evaluate_order(plan=plan, travel_matrix=matrix, order=order)

        # The metrics are exactly the measured route: a late stop adds no hidden penalty.
        self.assertEqual(
            result.metrics.duration_sec,
            result.metrics.travel_sec + result.metrics.waiting_sec + result.metrics.service_sec,
        )
        self.assertEqual(result.metrics.service_sec, 2 * 600)
        self.assertEqual(result.metrics.waiting_sec, 0)
        self.assertEqual(
            result.metrics.travel_sec,
            # Stop A sits on the warehouse, so the first leg is exactly 0s.
            matrix.travel_time_seconds(point(55.75, 37.62), point(55.75, 37.37))
            + matrix.travel_time_seconds(point(55.75, 37.37), DEPOT),
        )
        self.assertFalse(result.metrics.feasible)
        # Infeasibility is carried by the violation data, and no penalty-bearing field exists.
        self.assertNotIn("penalty", {f.name for f in dataclasses.fields(result.metrics)})
        self.assertNotIn("violation", {f.name for f in dataclasses.fields(result.metrics)})

    def test_metrics_reject_a_duration_that_is_not_the_measured_route(self) -> None:
        kwargs = {
            "distance_m": 10.0,
            "duration_sec": 1000,
            "waiting_sec": 0,
            "travel_sec": 600,
            "service_sec": 600,
            "finish_arrival": utc(2026, 9, 11, 2, 0),
            "feasible": True,
        }
        with self.assertRaises(InvalidRoutePlanError):
            RouteMetrics(**{**kwargs, "duration_sec": 9999})

    def test_metrics_reject_negative_durations(self) -> None:
        for field_name in ("travel_sec", "waiting_sec", "service_sec", "duration_sec"):
            with self.subTest(field=field_name):
                kwargs = {
                    "distance_m": 10.0,
                    "duration_sec": 0,
                    "waiting_sec": 0,
                    "travel_sec": 0,
                    "service_sec": 0,
                    "finish_arrival": utc(2026, 9, 11, 2, 0),
                    "feasible": True,
                    field_name: -1,
                }
                with self.assertRaises(InvalidRoutePlanError):
                    RouteMetrics(**kwargs)

    def test_metrics_reject_a_naive_finish_arrival(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            RouteMetrics(
                distance_m=10.0,
                duration_sec=0,
                waiting_sec=0,
                travel_sec=0,
                service_sec=0,
                finish_arrival=datetime(2026, 9, 11, 2, 0),
                feasible=True,
            )


class BaselineTests(unittest.TestCase):
    """The USER baseline is input order; the ALGORITHM baseline is labelled and internal (D22)."""

    def test_user_baseline_follows_input_position_and_ignores_the_selection(self) -> None:
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1]),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25),
            stop("C", WAREHOUSE[0], WAREHOUSE[1] - 0.50),
            input_positions=[2, 0, 1],
            first_service_stop=chosen(STOP_A),
        )

        # The stops are held in input order, whatever order the caller passed them in.
        self.assertEqual([some_stop.id for some_stop in plan.stops], [STOP_B, STOP_C, STOP_A])
        self.assertEqual(user_baseline_order(plan), (STOP_B, STOP_C, STOP_A))
        self.assertEqual(user_baseline_order(plan), plan.user_baseline_order())
        self.assertEqual(plan.first_service_stop.selected_stop_id, STOP_A)
        self.assertNotEqual(user_baseline_order(plan)[0], plan.first_service_stop.selected_stop_id)

    def test_user_baseline_omits_disabled_stops_without_renumbering(self) -> None:
        plan = three_stop_plan()
        self.assertEqual(user_baseline_order(plan), (STOP_A, STOP_B, STOP_C))

    def test_build_solution_labels_both_baselines_and_keeps_the_algorithm_one_internal(
        self,
    ) -> None:
        plan = three_stop_plan(first_service_stop=chosen(STOP_C))
        matrix = FixedTravelMatrix()

        solution = build_solution(
            plan=plan,
            travel_matrix=matrix,
            order=[STOP_C, STOP_A, STOP_B],
            algorithm_order=[STOP_B, STOP_C, STOP_A],
        )

        self.assertIsNotNone(solution.user_baseline)
        self.assertIsNotNone(solution.algorithm_baseline)
        self.assertIs(solution.user_baseline.baseline_kind, BaselineKind.USER_SUPPLIED)
        self.assertIs(
            solution.algorithm_baseline.baseline_kind, BaselineKind.ALGORITHM_GREEDY
        )
        expected_user = evaluate_order(
            plan=plan, travel_matrix=matrix, order=[STOP_A, STOP_B, STOP_C]
        ).metrics
        self.assertEqual(solution.user_baseline.duration_sec, expected_user.duration_sec)
        self.assertEqual(solution.user_baseline.travel_sec, expected_user.travel_sec)
        self.assertEqual(solution.user_baseline.finish_arrival, expected_user.finish_arrival)
        # The algorithm baseline is a comparison figure, not the user's BEFORE route.
        self.assertNotEqual(
            (solution.algorithm_baseline.distance_m, solution.algorithm_baseline.duration_sec),
            (solution.user_baseline.distance_m, solution.user_baseline.duration_sec),
        )
        self.assertIsNone(solution.recommendation)
        self.assertIs(solution.provenance, matrix.provenance)
        self.assertEqual(solution.inputs_fingerprint, plan.inputs_fingerprint())
        self.assertEqual(len(solution.order), len(solution.timelines))

    def test_build_solution_starts_the_baseline_at_the_selected_first_stop(self) -> None:
        plan = three_stop_plan(first_service_stop=chosen(STOP_C))

        solution = build_solution(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=[STOP_C, STOP_A, STOP_B],
            algorithm_order=[STOP_A, STOP_B, STOP_C],
        )

        expected = evaluate_order(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=[STOP_C, STOP_A, STOP_B],
        ).metrics
        self.assertEqual(solution.algorithm_baseline.duration_sec, expected.duration_sec)
        self.assertEqual(solution.algorithm_baseline.travel_sec, expected.travel_sec)


class CommitmentTests(unittest.TestCase):
    """A committed route needs an explicit driver choice and keeps it first (I3/I4, D32)."""

    def test_build_solution_raises_without_a_selection(self) -> None:
        plan = three_stop_plan()  # FirstStopIntent.recommend(): nothing chosen yet
        with self.assertRaises(InvalidRoutePlanError):
            build_solution(
                plan=plan,
                travel_matrix=FixedTravelMatrix(),
                order=[STOP_A, STOP_B, STOP_C],
                algorithm_order=[STOP_A, STOP_B, STOP_C],
            )

    def test_build_solution_raises_when_the_order_does_not_start_with_the_selection(
        self,
    ) -> None:
        plan = three_stop_plan(first_service_stop=chosen(STOP_B))
        with self.assertRaises(InvalidRoutePlanError):
            build_solution(
                plan=plan,
                travel_matrix=FixedTravelMatrix(),
                order=[STOP_A, STOP_B, STOP_C],
                algorithm_order=[STOP_A, STOP_B, STOP_C],
            )

    def test_build_solution_raises_on_an_empty_order(self) -> None:
        plan = three_stop_plan(first_service_stop=chosen(STOP_A))
        with self.assertRaises(InvalidRoutePlanError):
            build_solution(
                plan=plan,
                travel_matrix=FixedTravelMatrix(),
                order=[],
                algorithm_order=[STOP_A, STOP_B, STOP_C],
            )

    def test_build_solution_accepts_a_manual_first_stop_choice(self) -> None:
        plan = three_stop_plan(
            first_service_stop=chosen(STOP_B, mode=FirstStopMode.MANUAL)
        )
        solution = build_solution(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=[STOP_B, STOP_C, STOP_A],
            algorithm_order=[STOP_B, STOP_C, STOP_A],
        )
        self.assertEqual(solution.order, (STOP_B, STOP_C, STOP_A))
        self.assertIs(solution.status, SolutionStatus.OK)
        self.assertEqual(solution.first_service_stop, plan.first_service_stop)


class DeterminismTests(unittest.TestCase):
    """Identical inputs produce identical metrics, timelines and fingerprints."""

    def test_evaluation_is_deterministic_twice(self) -> None:
        plan = three_stop_plan(first_service_stop=chosen(STOP_A))
        order = [STOP_A, STOP_C, STOP_B]

        first = evaluate_order(plan=plan, travel_matrix=FixedTravelMatrix(), order=order)
        second = evaluate_order(plan=plan, travel_matrix=FixedTravelMatrix(), order=order)

        self.assertEqual(first, second)
        self.assertEqual(first.metrics, second.metrics)
        self.assertEqual(first.timelines, second.timelines)
        self.assertEqual(first.violations, second.violations)
        self.assertEqual(plan.inputs_fingerprint(), plan.inputs_fingerprint())
        self.assertEqual(
            [timeline.stop_id for timeline in first.timelines], [STOP_A, STOP_C, STOP_B]
        )

    def test_solution_is_deterministic_twice(self) -> None:
        plan = three_stop_plan(first_service_stop=chosen(STOP_A))
        order = [STOP_A, STOP_C, STOP_B]

        first = build_solution(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=order,
            algorithm_order=[STOP_A, STOP_B, STOP_C],
        )
        second = build_solution(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=order,
            algorithm_order=[STOP_A, STOP_B, STOP_C],
        )

        self.assertEqual(first.order, second.order)
        self.assertEqual(first.metrics, second.metrics)
        self.assertEqual(first.timelines, second.timelines)
        self.assertEqual(first.user_baseline, second.user_baseline)
        self.assertEqual(first.algorithm_baseline, second.algorithm_baseline)
        self.assertEqual(first.inputs_fingerprint, second.inputs_fingerprint)
        self.assertEqual(len(first.inputs_fingerprint), 64)

    def test_fingerprint_is_independent_of_the_selection(self) -> None:
        # The inputs fingerprint deliberately excludes the driver's decision (D4/D33), so a
        # committed route and its recommendation share it; only the route order differs.
        no_choice = three_stop_plan()
        with_choice = three_stop_plan(first_service_stop=chosen(STOP_A))

        self.assertEqual(
            no_choice.inputs_fingerprint(), with_choice.inputs_fingerprint()
        )


class RouteEvaluationContractTests(unittest.TestCase):
    """The evaluation type keeps order, timelines, violations and metrics consistent."""

    def test_solve_is_implemented_and_returns_a_committed_route(self) -> None:
        # Stage 2 unit U2 replaced the not-implemented boundary with the real optimizer; the
        # committed route still keeps the driver's selected first stop first (I3/I4, D32).
        plan = three_stop_plan(first_service_stop=chosen(STOP_A))

        solution = solve(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            algorithm_order=[STOP_A, STOP_B, STOP_C],
        )

        self.assertEqual(solution.order[0], STOP_A)
        self.assertIs(solution.status, SolutionStatus.OK)
        self.assertEqual(len(solution.order), 3)
        self.assertEqual(len(solution.timelines), 3)
        self.assertIsNotNone(solution.user_baseline)
        self.assertIsNotNone(solution.algorithm_baseline)

    def test_solve_requires_a_driver_selected_first_stop(self) -> None:
        # Nothing selects a stop automatically: the engine recommends, the driver decides.
        plan = three_stop_plan()  # FirstStopIntent.recommend(): nothing chosen yet
        with self.assertRaises(InvalidRoutePlanError):
            solve(plan=plan, travel_matrix=FixedTravelMatrix())

    def test_evaluation_rejects_timelines_that_do_not_follow_the_order(self) -> None:
        plan = three_stop_plan()
        result = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A, STOP_B, STOP_C]
        )

        with self.assertRaises(InvalidRoutePlanError):
            RouteEvaluation(
                order=result.order,
                timelines=tuple(reversed(result.timelines)),
                violations=result.violations,
                metrics=result.metrics,
                feasible=result.feasible,
            )

    def test_evaluation_rejects_feasibility_that_contradicts_the_metrics(self) -> None:
        plan = three_stop_plan()
        result = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A, STOP_B, STOP_C]
        )

        with self.assertRaises(InvalidRoutePlanError):
            RouteEvaluation(
                order=result.order,
                timelines=result.timelines,
                violations=result.violations,
                metrics=result.metrics,
                feasible=False,
            )

    def test_evaluation_exposes_finish_arrival_from_its_metrics(self) -> None:
        plan = three_stop_plan()
        result = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=[STOP_A, STOP_B, STOP_C]
        )
        self.assertEqual(result.finishes_at, result.metrics.finish_arrival)


if __name__ == "__main__":
    unittest.main()
