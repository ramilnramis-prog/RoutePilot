"""The deterministic route optimizer (Stage 2 unit U2; v2 sections 7, 12, 15, 19, 20, 21, 30).

What is locked in here:

* the fast inner-loop path is **semantically identical** to the authoritative
  ``evaluate_order`` - on orders with waiting, with lateness, with unknown and unrestricted
  windows and under both window-end policies (v2 section 9, D29);
* a complete route still means ``START -> enabled stops -> FINISH`` with the final leg included
  (v2 section 15), and the objective the optimizer minimizes is the elapsed time of exactly that
  route;
* the greedy seed starts at the driver's selected first stop, is deterministic, and is
  **not** nearest-neighbour: waiting genuinely changes its choice (v2 sections 1 and 21, D17);
* local improvement never worsens the accepted objective and never increases the violation count,
  and it never turns hard infeasibility into a numeric penalty (v2 section 21, D13 amendment);
* exactly the enabled stops appear once, disabled stops never appear, START is never a service
  stop and FINISH is never in the order (v2 section 2, spec section 27);
* the leg cache is deterministic, correct and measurably reused (v2 section 20);
* the committed route's own fingerprint depends on the selected first stop and on the order,
  while the recommendation fingerprint does not (v2 section 7, D33);
* optimization is deterministic across repeated runs (no wall-clock, no randomness).

Every travel matrix here is synthetic (``DEMO_SYNTHETIC``); nothing in this file is road routing.
"""

from __future__ import annotations

import dataclasses
import itertools
import sys
import time as timer
import unittest
from datetime import time

from core.engine.optimizer import (
    LegCache,
    RouteProblem,
    build_problem,
    build_solution,
    evaluate_order,
    fast_evaluate,
    fast_route_evaluation,
    greedy_seed,
    improve,
    optimize,
    route_fingerprint,
    solve,
    solve_route,
)
from core.engine.optimizer.route_problem import RouteProblem as RouteProblemClass
from core.engine.providers import ProviderCapabilities
from core.model.first_stop import FirstStopIntent, FirstStopMode
from core.model.ids import StopId
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.solution import BaselineKind, SolutionStatus, ViolationKind
from core.model.value_objects import DataProvenance, GeoPoint
from core.validation.errors import (
    InvalidOrderError,
    InvalidRoutePlanError,
    MissingServiceDurationError,
    NonexistentLocalTimeError,
    StopNotGeocodedError,
)
from demo.dataset import HEADLINE_STOP_IDS, build_demo_plan
from demo.synthetic_matrix import demo_matrix
from tests.support import (
    BERLIN,
    FixedTravelMatrix,
    WAREHOUSE,
    build_plan,
    place,
    point,
    stop,
)

STOP_A = StopId("A")
STOP_B = StopId("B")
STOP_C = StopId("C")
STOP_D = StopId("D")
STOP_OFF = StopId("OFF")

#: The synthetic warehouse and depot as points.
START = point(*WAREHOUSE)
DEPOT = point(55.70, 37.55)

EIGHT = time(8, 0)
EIGHTEEN = time(18, 0)
LATE = time(23, 0)
FINISH_BEFORE_END = WindowEndPolicy.SERVICE_FINISH_BEFORE_END
START_BEFORE_END = WindowEndPolicy.SERVICE_START_BEFORE_END


def chosen(
    stop_id: StopId, *, mode: FirstStopMode = FirstStopMode.RECOMMEND
) -> FirstStopIntent:
    """The driver's explicit choice of the first service stop (D6/D32)."""
    return FirstStopIntent.manual_choice(stop_id, mode=mode)


def fixed(start: time, end: time) -> ServiceWindow:
    return ServiceWindow.fixed(start, end)


def simple_plan(**kwargs):
    """Four enabled stops along one line plus one disabled stop; input order A, B, C, D."""
    return build_plan(
        stop("A", WAREHOUSE[0], WAREHOUSE[1]),
        stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25),
        stop("C", WAREHOUSE[0], WAREHOUSE[1] - 0.50),
        stop("D", WAREHOUSE[0], WAREHOUSE[1] - 0.75),
        stop("OFF", WAREHOUSE[0], WAREHOUSE[1] - 1.00, enabled=False),
        **kwargs,
    )


def waiting_plan(*, departure_hour: int, window_end_policy=None):
    """A fixed first stop, then a near stop that opens late and a far stop that opens on arrival.

    Departure is 04:00 Moscow by default. ``HUB`` sits at the warehouse, so the driver's first
    stop is forced and the seed's real decision is the *second* stop: the nearest remaining
    customer (``NEAR``, 1h away, opens 11:00 local) or the far one (``FAR``, 3h away, opens 08:00
    local). ``departure_hour=6`` makes that decision flip, because the leg to ``FAR`` costs the
    same whether it is driven now or after waiting at ``NEAR``.
    """
    near_window = fixed(time(11, 0), time(18, 0))
    far_window = fixed(EIGHT, EIGHTEEN)
    kwargs = {"window_end_policy": window_end_policy} if window_end_policy else {}
    return build_plan(
        stop("HUB", WAREHOUSE[0], WAREHOUSE[1]),
        stop("NEAR", WAREHOUSE[0], WAREHOUSE[1] - 1.0, window=near_window),
        stop("FAR", WAREHOUSE[0], WAREHOUSE[1] - 3.0, window=far_window),
        departure_time=datetime_at(departure_hour),
        **kwargs,
    )


def datetime_at(local_hour: int):
    """A UTC instant that is ``local_hour`` local time in Europe/Moscow on the demo date."""
    return _utc(2026, 9, 11, local_hour - 3, 0)


def _utc(year: int, month: int, day: int, hour: int, minute: int):
    from datetime import datetime, timezone

    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def chaos_plan(*, window_end_policy=None):
    """Six stops mixing waiting, lateness risk, unknown hours and always-accessible hours."""
    kwargs = {"window_end_policy": window_end_policy} if window_end_policy else {}
    return build_plan(
        stop("A", 55.75, 37.62, window=fixed(time(9, 0), time(18, 0))),
        stop("B", 55.75, 37.45, window=ServiceWindow.unknown()),
        stop("C", 55.90, 37.30, window=ServiceWindow.unrestricted()),
        stop("D", 55.60, 37.70, window=fixed(time(7, 0), time(9, 0))),
        stop("E", 55.80, 37.20, window=fixed(time(10, 0), time(10, 15))),
        stop("F", 55.70, 37.80, window=fixed(time(6, 0), time(6, 30))),
        default_service_duration=900,
        **kwargs,
    )


def problem_for(plan, *, first: StopId, matrix=None, cache=None) -> RouteProblem:
    """A prepared problem, optionally on a caller-supplied cache."""
    return build_problem(
        plan=plan, travel_matrix=matrix if matrix is not None else FixedTravelMatrix(),
        first_stop_id=first, cache=cache,
    )


def _apply(order, move):
    """Apply one recorded local-search move to ``order`` (deterministic replay)."""
    from core.engine.optimizer.local_search import apply_move

    return apply_move(order, move)


class FastPathAgreementTests(unittest.TestCase):
    """The fast path is the authoritative arithmetic, not a second model of it (v2 section 15)."""

    def assert_agrees(self, plan, order, *, matrix=None, cache=None, first=STOP_A) -> None:
        problem = problem_for(plan, first=first, matrix=matrix, cache=cache)
        fast = fast_evaluate(problem, order)
        authoritative = evaluate_order(
            plan=plan,
            travel_matrix=problem.legs,
            order=order,
        )
        metrics = authoritative.metrics
        self.assertEqual(fast.finish_elapsed_sec, metrics.duration_sec)
        self.assertEqual(fast.travel_sec, metrics.travel_sec)
        self.assertEqual(fast.waiting_sec, metrics.waiting_sec)
        self.assertEqual(fast.service_sec, metrics.service_sec)
        self.assertEqual(fast.distance_m, metrics.distance_m)
        self.assertEqual(fast.violations, len(authoritative.violations))
        self.assertEqual(fast.order, authoritative.order)
        self.assertEqual(fast.finishes_at, metrics.finish_arrival)

    def test_agrees_on_orders_with_waiting(self) -> None:
        plan = chaos_plan()
        for order in itertools.permutations(problem_for(plan, first=STOP_A).stop_ids):
            with self.subTest(order=order):
                self.assert_agrees(plan, order)

    def test_agrees_on_a_route_with_lateness(self) -> None:
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1]),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25, window=fixed(time(3, 30), time(4, 30))),
        )
        self.assert_agrees(plan, (STOP_A, STOP_B))
        self.assert_agrees(plan, (STOP_B, STOP_A))

    def test_agrees_for_unknown_and_unrestricted_windows(self) -> None:
        plan = build_plan(
            stop("A", 55.75, 37.62, window=ServiceWindow.unknown()),
            stop("B", 55.75, 37.45, window=ServiceWindow.unrestricted()),
            stop("C", 55.90, 37.30, window=ServiceWindow.unknown()),
        )
        for order in itertools.permutations((STOP_A, STOP_B, STOP_C)):
            with self.subTest(order=order):
                self.assert_agrees(plan, order)

    def test_agrees_under_both_window_end_policies(self) -> None:
        # Moscow is UTC+3, so this local 09:55-10:15 window is 06:55-07:15 UTC. Serving for 40
        # minutes from 09:55 local ends at 10:35 local: infeasible under service_finish_before_end,
        # feasible under service_start_before_end - the example of v2 section 9 and D29.
        local_window = fixed(time(9, 55), time(10, 15))
        for policy in (FINISH_BEFORE_END, START_BEFORE_END):
            with self.subTest(policy=policy.value):
                plan = build_plan(
                    stop(
                        "A",
                        WAREHOUSE[0],
                        WAREHOUSE[1],
                        window=local_window,
                        service_duration=2400,
                    ),
                    stop("B", 55.75, 37.45, window=fixed(EIGHT, EIGHTEEN)),
                    window_end_policy=policy,
                )
                for order in itertools.permutations((STOP_A, STOP_B)):
                    self.assert_agrees(plan, order)
                authoritative = evaluate_order(
                    plan=plan, travel_matrix=FixedTravelMatrix(), order=(STOP_A, STOP_B)
                )
                if policy is FINISH_BEFORE_END:
                    self.assertEqual(len(authoritative.violations), 1)
                    self.assertIs(
                        authoritative.violations[0].kind,
                        ViolationKind.TIME_WINDOW_INFEASIBLE,
                    )
                    self.assertFalse(authoritative.feasible)
                else:
                    self.assertEqual(authoritative.violations, ())
                    self.assertTrue(authoritative.feasible)
                    # The overrun is still recorded as information, never as infeasibility.
                    self.assertGreater(
                        authoritative.timelines[0].finish_overtime, 0
                    )

    def test_fast_path_uses_no_per_stop_window_resolution(self) -> None:
        plan = chaos_plan()
        problem = problem_for(plan, first=STOP_A)
        self.assertGreater(problem.service_dates_precomputed, 0)
        self.assertGreater(len(problem.window_table), 0)

        # A full battery of complete routes, none of which may touch the authoritative resolver.
        for order in itertools.permutations(problem.stop_ids):
            fast = fast_evaluate(problem, order)
            self.assertEqual(fast.window_fallbacks, 0)
        self.assertEqual(problem.fallbacks_used, 0)

    def test_authoritative_fallback_is_used_when_the_table_cannot_answer(self) -> None:
        # A problem whose precomputed window table answers nothing: every window must come from
        # the authoritative core.time.tz resolver instead of being guessed, and the result must be
        # identical to the precomputed path.
        built = build_plan(
            stop("A", 55.75, 37.62, window=fixed(EIGHT, EIGHTEEN)),
            stop("B", 55.75, 37.45, window=fixed(time(9, 0), time(17, 0))),
        )
        base = problem_for(built, first=STOP_A)
        self.assertGreater(len(base.window_table), 0)
        hollow = base.without_precomputed_windows()
        self.assertEqual(hollow.window_table, {})
        order = (STOP_A, STOP_B)

        fast = fast_evaluate(hollow, order)
        self.assertGreater(fast.window_fallbacks, 0, "the fallback must have been exercised")
        authoritative = evaluate_order(plan=built, travel_matrix=hollow.legs, order=order)
        self.assertEqual(fast.finish_elapsed_sec, authoritative.metrics.duration_sec)
        self.assertEqual(fast.waiting_sec, authoritative.metrics.waiting_sec)
        self.assertEqual(fast.violations, len(authoritative.violations))
        # The precomputed table and the authoritative fallback resolve to the same windows.
        self.assertEqual(
            fast_route_evaluation(base, fast_evaluate(base, order)).metrics,
            fast_route_evaluation(hollow, fast).metrics,
        )

    def test_agrees_for_an_arrival_beyond_the_precomputed_dates(self) -> None:
        # The table covers nine local service dates here; the last leg of this route is eleven
        # days long, so the final arrival leaves the prepared horizon entirely. The lookup must
        # then be answered by the authoritative resolver (U2 contract item 2) - taking the nearest
        # prepared date's window instead invents a violation the real route does not have.
        matrix = FixedTravelMatrix(
            overrides={
                (55.75, 37.62, 55.75, 37.37): 600,  # A -> B
                (55.75, 37.37, 55.75, 36.62): 950_400,  # B -> C, eleven days
                (55.75, 36.62, 55.70, 37.55): 600,  # C -> FINISH
            }
        )
        plan = build_plan(
            stop("A", 55.75, 37.62, window=fixed(EIGHT, EIGHTEEN)),
            stop("B", 55.75, 37.37, window=fixed(EIGHT, EIGHTEEN)),
            stop("C", 55.75, 36.62, window=fixed(EIGHT, EIGHTEEN)),
            first_service_stop=chosen(STOP_A),
        )
        order = (STOP_A, STOP_B, STOP_C)
        problem = problem_for(plan, first=STOP_A, matrix=matrix)
        self.assertEqual(problem.service_dates_precomputed, 9)

        fast = fast_evaluate(problem, order)

        self.assertGreater(fast.window_fallbacks, 0, "the horizon fallback must be exercised")
        self.assert_agrees(plan, order, matrix=matrix)
        self.assertEqual(fast.violations, 0)


class WindowPreparationTests(unittest.TestCase):
    """Preparing a problem never resolves a time the route cannot use (v2 section 21, D3/D29).

    The window table covers a horizon of dates, and a route may never reach most of them. A DST
    gap inside that horizon must therefore be skipped rather than raised while the table is built:
    only an arrival that really lands on a gap date - the arrival the authoritative
    ``compute_stop_timeline`` also rejects - may raise ``NonexistentLocalTimeError``.
    """

    def test_a_window_that_falls_in_a_dst_gap_on_a_later_date_still_builds_and_optimizes(self) -> None:
        # Berlin springs forward on 2026-03-29 (02:00 -> 03:00 local), so a 02:00-02:30 window does
        # not exist on that date. Departing 2026-03-28 the route is served on 03-28 while the
        # horizon still covers 03-29: building the problem must not raise for a plan the
        # authoritative engine routes fine (U2 review issue 1).
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1], window=fixed(time(2, 0), time(2, 30))),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25, window=fixed(EIGHT, EIGHTEEN)),
            timezone_name=BERLIN,
            departure_time=_utc(2026, 3, 28, 3, 0),  # 04:00 Berlin, the day before the gap
            first_service_stop=chosen(STOP_A),
        )
        order = (STOP_A, STOP_B)
        authoritative = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=order
        )

        problem = problem_for(plan, first=STOP_A)  # must not raise
        fast = fast_evaluate(problem, order)

        self.assertGreater(problem.service_dates_precomputed, 2, "the horizon must include 03-29")
        # The gap day is inside the horizon and its local midnight exists, but its 02:00-02:30
        # window does not: that entry is skipped instead of raising while the table is built.
        self.assertIsNotNone(problem.utc_cutoffs[1])
        self.assertIn((STOP_A, 0), problem.window_table)
        self.assertNotIn((STOP_A, 1), problem.window_table)
        self.assertEqual(fast.finish_elapsed_sec, authoritative.metrics.duration_sec)
        self.assertEqual(fast.violations, len(authoritative.violations))
        self.assertEqual(fast.waiting_sec, authoritative.metrics.waiting_sec)
        self.assertEqual(optimize(problem).order, order)
        self.assertEqual(solve_route(plan=plan, travel_matrix=FixedTravelMatrix()).order, order)

    def test_a_local_midnight_inside_a_dst_gap_still_builds_and_optimizes(self) -> None:
        # America/Santiago starts DST on 2026-09-06 by moving 00:00 straight to 01:00, so local
        # midnight does not exist on that date. The day boundary is left unresolved and the window
        # is resolved authoritatively for a real arrival; building the problem cannot fail
        # (U2 review issue 1).
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1], window=fixed(time(9, 0), time(17, 0))),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25, window=fixed(time(9, 0), time(17, 0))),
            timezone_name="America/Santiago",
            departure_time=_utc(2026, 9, 1, 12, 0),
            first_service_stop=chosen(STOP_A),
        )
        order = (STOP_A, STOP_B)
        authoritative = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=order
        )

        problem = problem_for(plan, first=STOP_A)  # must not raise
        fast = fast_evaluate(problem, order)

        self.assertIn(None, problem.utc_cutoffs, "the 2026-09-06 midnight must stay unresolved")
        self.assertEqual(fast.finish_elapsed_sec, authoritative.metrics.duration_sec)
        self.assertEqual(fast.violations, len(authoritative.violations))
        self.assertEqual(optimize(problem).order, order)

    def test_an_arrival_that_really_lands_in_a_gap_still_raises(self) -> None:
        # The plan is buildable, but this route arrives on 2026-03-29 Berlin, where the stored
        # 02:00-02:30 window does not exist. The authoritative timeline raises, so the fast path
        # must raise the same error instead of pricing a window that never existed (D3).
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1], window=fixed(time(2, 0), time(2, 30))),
            timezone_name=BERLIN,
            departure_time=_utc(2026, 3, 29, 0, 30),  # 01:30 Berlin, the gap day
            first_service_stop=chosen(STOP_A),
        )
        problem = problem_for(plan, first=STOP_A)

        with self.assertRaises(NonexistentLocalTimeError):
            evaluate_order(plan=plan, travel_matrix=FixedTravelMatrix(), order=(STOP_A,))
        with self.assertRaises(NonexistentLocalTimeError):
            fast_evaluate(problem, (STOP_A,))


class StructuralErrorFidelityTests(unittest.TestCase):
    """The fast path refuses exactly what the authoritative engine refuses (v2 section 16).

    A stop with no coordinates or with no service duration anywhere is not a routing puzzle: it
    cannot be served at all. The fast inner loop must report the same error ``evaluate_order``
    reports, never substitute the departure point for a missing location or price a missing
    duration as zero seconds (U2 review issue 2).
    """

    def assert_all_paths_raise(self, plan, order, expected) -> None:
        with self.assertRaises(expected):
            evaluate_order(plan=plan, travel_matrix=FixedTravelMatrix(), order=order)
        problem = problem_for(plan, first=order[0])
        with self.assertRaises(expected):
            fast_evaluate(problem, order)
        # Even a pass that has not placed the stop yet stands in for the complete route.
        with self.assertRaises(expected):
            fast_evaluate(problem, order[:1])
        with self.assertRaises(expected):
            greedy_seed(problem)
        with self.assertRaises(expected):
            improve(problem, order)
        with self.assertRaises(expected):
            optimize(problem_for(plan, first=order[0]))

    def test_fast_path_refuses_a_stop_without_coordinates(self) -> None:
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1]),
            stop("B", None, None),
            first_service_stop=chosen(STOP_A),
        )
        self.assert_all_paths_raise(plan, (STOP_A, STOP_B), StopNotGeocodedError)

    def test_fast_path_refuses_a_stop_without_a_service_duration(self) -> None:
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1]),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25, service_duration=None),
            first_service_stop=chosen(STOP_A),
        )
        self.assert_all_paths_raise(plan, (STOP_A, STOP_B), MissingServiceDurationError)

    def test_a_plan_default_duration_keeps_the_same_plan_routable(self) -> None:
        # The negative case of the rule above: with a plan default there is nothing missing, so
        # every path still routes - the check must not degrade into "unknown duration" (v2 §17).
        plan = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1], service_duration=None),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.25, service_duration=None),
            default_service_duration=900,
            first_service_stop=chosen(STOP_A),
        )
        order = (STOP_A, STOP_B)
        authoritative = evaluate_order(
            plan=plan, travel_matrix=FixedTravelMatrix(), order=order
        )
        problem = problem_for(plan, first=STOP_A)

        self.assertEqual(fast_evaluate(problem, order).finish_elapsed_sec,
                         authoritative.metrics.duration_sec)
        self.assertEqual(greedy_seed(problem)[0], STOP_A)
        self.assertEqual(optimize(problem).order, order)


class SeedTests(unittest.TestCase):
    """The greedy seed honours the driver's first stop and is not nearest-neighbour (D17)."""

    def test_seed_starts_with_the_selected_first_stop_and_visits_every_enabled_stop_once(self) -> None:
        plan = simple_plan()
        problem = problem_for(plan, first=STOP_C)

        order = greedy_seed(problem)

        self.assertEqual(order[0], STOP_C)
        self.assertEqual(sorted(order), sorted((STOP_A, STOP_B, STOP_C, STOP_D)))
        self.assertEqual(len(order), len(set(order)))
        self.assertNotIn(STOP_OFF, order)

    def test_seed_is_deterministic(self) -> None:
        plan = simple_plan()
        first = greedy_seed(problem_for(plan, first=STOP_B))
        second = greedy_seed(problem_for(plan, first=STOP_B))
        self.assertEqual(first, second)

    def test_waiting_genuinely_influences_the_seed(self) -> None:
        hub = StopId("HUB")
        near = StopId("NEAR")
        far = StopId("FAR")

        # Nearest-neighbour would always pick NEAR: its first leg is 1h against FAR's 3h.
        probe = problem_for(waiting_plan(departure_hour=4), first=hub)
        self.assertLess(
            probe.travel_sec(probe.departure_point, probe.location_of(near)),
            probe.travel_sec(probe.departure_point, probe.location_of(far)),
        )

        # At 04:00 the 3h leg to FAR arrives exactly at its 08:00 opening, while NEAR costs 1h of
        # driving plus 6h of waiting, so the complete-route objective picks the *farther* stop.
        early = greedy_seed(problem_for(waiting_plan(departure_hour=4), first=hub))
        self.assertEqual(early, (hub, far, near))

        # Leaving at 06:00 the two choices tie exactly on the complete-route objective: driving to
        # FAR now costs the same as waiting at NEAR and driving later. The deterministic tie-break
        # then hands the choice to the earlier input_position, so the route follows the objective
        # and never depends on scan order.
        late = greedy_seed(problem_for(waiting_plan(departure_hour=6), first=hub))
        self.assertEqual(late, (hub, near, far))

        self.assertNotEqual(early, late)

    def test_seed_is_never_worse_than_the_best_route_it_could_have_chosen(self) -> None:
        # Exhaustive reference over all 720 orders of a 6-stop plan: the seed must be the best
        # order that starts with the required first stop, or a tie for it.
        plan = chaos_plan()
        problem = problem_for(plan, first=STOP_A)
        seed = greedy_seed(problem)
        seed_evaluation = fast_evaluate(problem, seed)

        best = min(
            (
                fast_evaluate(problem, order)
                for order in itertools.permutations(problem.stop_ids)
                if order[0] == STOP_A
            ),
            key=lambda evaluation: (evaluation.violations, evaluation.finish_elapsed_sec),
        )
        self.assertEqual(seed_evaluation.violations, best.violations)
        self.assertEqual(seed_evaluation.finish_elapsed_sec, best.finish_elapsed_sec)


class LocalSearchTests(unittest.TestCase):
    """Acceptance is lexicographic and monotone; infeasibility is never a penalty (v2 section 21)."""

    def test_improvement_is_monotone_under_the_lexicographic_rule(self) -> None:
        for policy in (FINISH_BEFORE_END, START_BEFORE_END):
            with self.subTest(policy=policy.value):
                plan = chaos_plan(window_end_policy=policy)
                problem = problem_for(plan, first=STOP_A)
                seed = greedy_seed(problem)

                result = improve(problem, seed)

                # Lexicographic acceptance: violations first, then the objective. A longer route
                # that removes a hard-window violation is an improvement; an equally violating
                # route may never finish later (v2 section 21, D13 amendment).
                self.assertLessEqual(result.final_violations, result.seed_violations)
                if result.final_violations == result.seed_violations:
                    self.assertLessEqual(result.final_objective, result.seed_objective)
                self.assertEqual(result.order[0], STOP_A)
                self.assertEqual(sorted(result.order), sorted(problem.stop_ids))
                evaluated = fast_evaluate(problem, result.order)
                self.assertEqual(evaluated.finish_elapsed_sec, result.final_objective)
                self.assertEqual(evaluated.violations, result.final_violations)
                self.assertGreaterEqual(result.evaluations, 0)

    def test_no_accepted_move_ever_increases_the_violation_count(self) -> None:
        # The monotonicity guarantee is replayed move by move, not just on the final result.
        plan = _demo_plan_with_selected_first_stop("S01-NEAR")
        problem = problem_for(plan, first=StopId("S01-NEAR"), matrix=demo_matrix())
        seed_order = greedy_seed(problem)
        result = improve(problem, seed_order)

        order = seed_order
        previous = fast_evaluate(problem, order)
        for move in result.accepted_moves:
            order = _apply(order, move)
            current = fast_evaluate(problem, order)
            self.assertLessEqual(current.violations, previous.violations)
            if current.violations == previous.violations:
                self.assertLessEqual(current.finish_elapsed_sec, previous.finish_elapsed_sec)
            previous = current
        self.assertEqual(order, result.order)

    def test_local_search_is_deterministic_and_bounded(self) -> None:
        plan = chaos_plan()
        first = improve(problem_for(plan, first=STOP_A), greedy_seed(problem_for(plan, first=STOP_A)))
        second = improve(problem_for(plan, first=STOP_A), greedy_seed(problem_for(plan, first=STOP_A)))

        self.assertEqual(first.order, second.order)
        self.assertEqual(first.accepted_moves, second.accepted_moves)
        self.assertEqual(first.evaluations, second.evaluations)
        self.assertEqual(first.seed_objective, second.seed_objective)
        self.assertEqual(first.final_objective, second.final_objective)
        self.assertLessEqual(first.evaluations, 20_000)

    def test_a_never_worsening_search_from_any_order_keeps_the_first_stop(self) -> None:
        plan = simple_plan()
        problem = problem_for(plan, first=STOP_B)
        # A deliberately bad - but valid - order: all three remaining stops reversed.
        start_order = (STOP_B, STOP_D, STOP_C, STOP_A)

        result = improve(problem, start_order)

        self.assertEqual(result.order[0], STOP_B)
        self.assertEqual(sorted(result.order), sorted((STOP_A, STOP_B, STOP_C, STOP_D)))
        self.assertLessEqual(result.final_violations, result.seed_violations)
        self.assertLessEqual(
            (result.final_violations, result.final_objective),
            (result.seed_violations, result.seed_objective),
        )

    def test_improvement_rejects_an_order_that_does_not_start_with_the_selection(self) -> None:
        plan = simple_plan()
        problem = problem_for(plan, first=STOP_B)
        with self.assertRaises(InvalidRoutePlanError):
            improve(problem, (STOP_A, STOP_B, STOP_C, STOP_D))

    def test_improvement_rejects_an_order_that_omits_a_stop(self) -> None:
        plan = simple_plan()
        problem = problem_for(plan, first=STOP_A)
        with self.assertRaises(InvalidOrderError):
            improve(problem, (STOP_A, STOP_B, STOP_C))

    def test_no_violation_is_ever_converted_into_a_time_penalty(self) -> None:
        # Two stops that cannot both be served in time: one violation is unavoidable, and the
        # metrics still describe the measured route exactly (D13 amendment).
        plan = build_plan(
            stop("A", 55.75, 37.62, window=fixed(time(3, 0), time(3, 30))),
            stop("B", 55.75, 37.45, window=fixed(time(3, 0), time(3, 30))),
            first_service_stop=chosen(STOP_A),
        )
        problem = problem_for(plan, first=STOP_A)
        result = optimize(problem)

        metrics = result.evaluation.metrics
        self.assertEqual(
            metrics.duration_sec, metrics.travel_sec + metrics.waiting_sec + metrics.service_sec
        )
        self.assertGreaterEqual(len(result.evaluation.violations), 1)
        self.assertFalse(result.evaluation.feasible)
        self.assertEqual(
            result.local_search.final_violations, len(result.evaluation.violations)
        )


class RouteShapeTests(unittest.TestCase):
    """START, FINISH, disabled stops and the exactly-once rule (v2 section 2, spec section 27)."""

    def test_optimized_order_contains_every_enabled_stop_once_and_no_disabled_stop(self) -> None:
        plan = simple_plan()
        result = optimize(problem_for(plan, first=STOP_C))

        self.assertEqual(sorted(result.order), sorted((STOP_A, STOP_B, STOP_C, STOP_D)))
        self.assertEqual(len(result.order), len(set(result.order)))
        self.assertEqual(
            sorted(some_stop.id for some_stop in plan.active_stops()), sorted(result.order)
        )
        self.assertNotIn(STOP_OFF, result.order)
        self.assertEqual([some_stop.id for some_stop in plan.disabled_stops()], [STOP_OFF])

    def test_start_is_never_a_service_stop_and_finish_is_never_in_the_order(self) -> None:
        plan = simple_plan()
        result = optimize(problem_for(plan, first=STOP_A))

        self.assertNotIn(plan.departure, plan.stops)
        self.assertNotIn(plan.finish, plan.stops)
        self.assertNotIn(plan.departure.label, result.order)
        self.assertNotIn(plan.finish.label, result.order)
        self.assertEqual(len(result.order), len(result.evaluation.timelines))
        for timeline in result.evaluation.timelines:
            self.assertNotIn(timeline.stop_id, (plan.departure.label, plan.finish.label))

    def test_the_finish_leg_is_part_of_the_optimized_objective(self) -> None:
        near = build_plan(
            stop("A", WAREHOUSE[0], WAREHOUSE[1] - 0.25),
            stop("B", WAREHOUSE[0], WAREHOUSE[1] - 0.50),
            finish=place("Depot", 55.70, 37.55),
            first_service_stop=chosen(STOP_A),
        )
        far = dataclasses.replace(near, finish=place("Far depot", 50.0, 37.55))
        matrix = FixedTravelMatrix()

        near_result = optimize(problem_for(near, first=STOP_A, matrix=matrix))
        far_result = optimize(problem_for(far, first=STOP_A, matrix=matrix))

        self.assertEqual(near_result.order, far_result.order)
        self.assertGreater(
            far_result.evaluation.metrics.travel_sec, near_result.evaluation.metrics.travel_sec
        )
        self.assertGreater(
            far_result.local_search.final_objective, near_result.local_search.final_objective
        )
        self.assertNotEqual(
            near_result.evaluation.metrics.finish_arrival,
            far_result.evaluation.metrics.finish_arrival,
        )


class RawCoordinateMatrix:
    """A synthetic matrix whose answers are sensitive to the raw coordinates (DEMO_SYNTHETIC).

    :class:`~tests.support.FixedTravelMatrix` rounds its own key to six decimals, which would mask
    a cache that merged two nearby points; this one never rounds, and its (synthetic) scale is large
    enough that a seventh-decimal coordinate difference still changes both answers.
    """

    provenance = DataProvenance.DEMO_SYNTHETIC
    capabilities = ProviderCapabilities()

    #: Synthetic scale: one degree of coordinate delta is this many seconds of driving.
    seconds_per_degree = 1_000_000_000

    def travel_time_seconds(self, origin: GeoPoint, destination: GeoPoint) -> int:
        return int(round(self._delta(origin, destination) * self.seconds_per_degree))

    def distance_meters(self, origin: GeoPoint, destination: GeoPoint) -> float:
        # Synthetic marker distance, deliberately unrounded (never real road distance).
        return self._delta(origin, destination) * self.seconds_per_degree * 12.0

    @staticmethod
    def _delta(origin: GeoPoint, destination: GeoPoint) -> float:
        return max(
            abs(destination.latitude - origin.latitude),
            abs(destination.longitude - origin.longitude),
        )


class LegCacheTests(unittest.TestCase):
    """The cache is deterministic, correct and measurably reused (v2 section 20)."""

    def test_cache_hits_and_misses_are_deterministic_and_correct(self) -> None:
        cache = LegCache(FixedTravelMatrix())
        origin = point(55.75, 37.62)
        destination = point(55.75, 37.37)
        expected = FixedTravelMatrix().travel_time_seconds(origin, destination)
        expected_distance = FixedTravelMatrix().distance_meters(origin, destination)

        self.assertEqual(cache.stats, type(cache.stats)(hits=0, misses=0, entries=0))
        for _ in range(3):
            self.assertEqual(cache.travel_time_seconds(origin, destination), expected)
            self.assertEqual(cache.distance_meters(origin, destination), expected_distance)

        stats = cache.stats
        self.assertEqual(stats.misses, 2)  # travel + distance, each priced once
        self.assertEqual(stats.hits, 4)  # the remaining two rounds of two questions
        self.assertEqual(stats.entries, 1)
        self.assertEqual(stats.lookups, 6)
        self.assertIn("hits", stats.describe())

        other = point(55.60, 37.70)
        cache.travel_time_seconds(origin, other)
        self.assertEqual(cache.stats.entries, 2)
        self.assertEqual(cache.stats.misses, 3)

    def test_repeated_optimization_is_identical_with_a_fresh_cache(self) -> None:
        plan = simple_plan()
        matrix = FixedTravelMatrix()

        cold_cache = LegCache(matrix)
        cold = optimize(problem_for(plan, first=STOP_A, matrix=matrix, cache=cold_cache))
        warm_cache = LegCache(matrix)
        warm = optimize(problem_for(plan, first=STOP_A, matrix=matrix, cache=warm_cache))

        self.assertEqual(cold.order, warm.order)
        self.assertGreater(cold_cache.stats.misses, 0)
        self.assertEqual(cold_cache.stats, warm_cache.stats)
        self.assertGreater(warm.cache_stats.entries, 0)

    def test_a_warm_optimization_pays_no_new_misses_for_the_same_legs(self) -> None:
        plan = simple_plan()
        matrix = FixedTravelMatrix()
        cache = LegCache(matrix)

        first = optimize(problem_for(plan, first=STOP_A, matrix=matrix, cache=cache))
        misses_after_first = cache.stats.misses
        second = optimize(problem_for(plan, first=STOP_A, matrix=matrix, cache=cache))

        self.assertEqual(first.order, second.order)
        # Every leg the second run asks for is already known: the cache is genuinely reused.
        self.assertEqual(cache.stats.misses, misses_after_first)
        self.assertGreater(second.cache_stats.hits, 0)

    def test_cache_reports_the_same_numbers_for_repeated_runs(self) -> None:
        plan = simple_plan()
        first_cache = LegCache(FixedTravelMatrix())
        second_cache = LegCache(FixedTravelMatrix())

        optimize(problem_for(plan, first=STOP_B, cache=first_cache))
        optimize(problem_for(plan, first=STOP_B, cache=second_cache))

        self.assertEqual(first_cache.stats, second_cache.stats)

    def test_cache_never_confuses_two_distinct_but_nearby_points(self) -> None:
        # Regression: a key rounded to fewer decimals than the query lets one point answer for
        # another, which changes a result. The raw answers below differ; the cache must not merge.
        matrix = RawCoordinateMatrix()
        cache = LegCache(matrix)
        warehouse = point(*WAREHOUSE)
        # Two distinct points that still collide once rounded to six decimals (55.750001 both).
        near_1 = point(55.7500012, WAREHOUSE[1])
        near_2 = point(55.7500016, WAREHOUSE[1])

        # The matrix is genuinely coordinate-sensitive at this scale ...
        self.assertNotEqual(
            matrix.travel_time_seconds(warehouse, near_1),
            matrix.travel_time_seconds(warehouse, near_2),
        )
        self.assertNotEqual(
            matrix.distance_meters(warehouse, near_1),
            matrix.distance_meters(warehouse, near_2),
        )

        # ... and the cache returns exactly the wrapped matrix's own answer for each point.
        for near in (near_1, near_2):
            self.assertEqual(
                cache.travel_time_seconds(warehouse, near),
                matrix.travel_time_seconds(warehouse, near),
            )
            self.assertEqual(
                cache.distance_meters(warehouse, near),
                matrix.distance_meters(warehouse, near),
            )

        stats = cache.stats
        self.assertEqual(stats.misses, 4)  # two distinct legs, two questions each
        self.assertEqual(stats.hits, 0)
        self.assertEqual(stats.entries, 2)
        # Repeating the same questions is served from the memo, unchanged.
        for near in (near_1, near_2):
            self.assertEqual(
                cache.travel_time_seconds(warehouse, near),
                matrix.travel_time_seconds(warehouse, near),
            )
        self.assertEqual(cache.stats.hits, 2)
        self.assertEqual(cache.stats.misses, 4)
        self.assertEqual(cache.stats.entries, 2)
        # Deterministic: an identical question sequence yields identical statistics.
        other = LegCache(RawCoordinateMatrix())
        for near in (near_1, near_2):
            other.travel_time_seconds(warehouse, near)
            other.distance_meters(warehouse, near)
        for near in (near_1, near_2):
            other.travel_time_seconds(warehouse, near)
        self.assertEqual(other.stats, cache.stats)


class FingerprintTests(unittest.TestCase):
    """The committed route needs its own fingerprint; a recommendation's must not move (v2 §7)."""

    def test_route_fingerprint_is_deterministic(self) -> None:
        plan = simple_plan(first_service_stop=chosen(STOP_A))
        order = (STOP_A, STOP_B, STOP_C, STOP_D)
        first = route_fingerprint(plan, order)
        second = route_fingerprint(plan, order)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_route_fingerprint_changes_with_the_selected_first_stop(self) -> None:
        base = simple_plan(first_service_stop=chosen(STOP_A))
        other = dataclasses.replace(
            base, first_service_stop=FirstStopIntent.manual_choice(STOP_B)
        )
        order_a = (STOP_A, STOP_B, STOP_C, STOP_D)
        order_b = (STOP_B, STOP_A, STOP_C, STOP_D)

        self.assertNotEqual(route_fingerprint(base, order_a), route_fingerprint(other, order_b))
        # ...while the recommendation fingerprint is deliberately independent of the selection
        # (v2 section 7, D4/D33): choosing a stop must not make the recommendation stale.
        self.assertEqual(base.inputs_fingerprint(), other.inputs_fingerprint())

    def test_route_fingerprint_changes_with_the_order(self) -> None:
        plan = simple_plan(first_service_stop=chosen(STOP_A))
        first = route_fingerprint(plan, (STOP_A, STOP_B, STOP_C, STOP_D))
        second = route_fingerprint(plan, (STOP_A, STOP_C, STOP_B, STOP_D))
        self.assertNotEqual(first, second)

    def test_route_fingerprint_follows_the_matrix_identity(self) -> None:
        plan = simple_plan(first_service_stop=chosen(STOP_A))
        order = (STOP_A, STOP_B, STOP_C, STOP_D)
        self.assertNotEqual(
            route_fingerprint(plan, order),
            route_fingerprint(plan, order, matrix_fingerprint="matrix-v2"),
        )

    def test_route_fingerprint_rejects_an_order_that_is_not_the_enabled_stops(self) -> None:
        plan = simple_plan(first_service_stop=chosen(STOP_A))
        with self.assertRaises(InvalidOrderError):
            route_fingerprint(plan, (STOP_A, STOP_B, STOP_C))
        with self.assertRaises(InvalidRoutePlanError):
            route_fingerprint(plan, ())

    def test_optimized_route_of_the_demo_plan_has_a_stable_fingerprint(self) -> None:
        plan = _demo_plan_with_selected_first_stop("S05-FARTHEST")
        result = optimize(problem_for(plan, first=StopId("S05-FARTHEST"), matrix=demo_matrix()))
        again = optimize(problem_for(plan, first=StopId("S05-FARTHEST"), matrix=demo_matrix()))

        self.assertEqual(result.order, again.order)
        self.assertEqual(
            route_fingerprint(plan, result.order), route_fingerprint(plan, again.order)
        )
        # A rotation is a different committed route, even though the stop set is the same.
        rotated = result.order[1:] + result.order[:1]
        plan_rotated = dataclasses.replace(
            plan, first_service_stop=FirstStopIntent.manual_choice(rotated[0])
        )
        self.assertNotEqual(
            route_fingerprint(plan, result.order),
            route_fingerprint(plan_rotated, rotated),
        )


class SolveRouteTests(unittest.TestCase):
    """The committed route, its baselines and the driver's decision (v2 sections 5, 30)."""

    def test_solve_route_end_to_end_on_the_demo_plan(self) -> None:
        selected = "S05-FARTHEST"
        plan = _demo_plan_with_selected_first_stop(selected)
        matrix = demo_matrix()

        solution = solve_route(plan=plan, travel_matrix=matrix)

        self.assertEqual(len(solution.order), 30)
        self.assertEqual(solution.order[0], StopId(selected))
        self.assertEqual(
            sorted(solution.order), sorted(some_stop.id for some_stop in plan.active_stops())
        )
        self.assertNotIn(StopId(HEADLINE_STOP_IDS["disabled"]), solution.order)
        self.assertEqual(len(solution.timelines), 30)
        self.assertIsNotNone(solution.user_baseline)
        self.assertIsNotNone(solution.algorithm_baseline)
        self.assertIs(solution.user_baseline.baseline_kind, BaselineKind.USER_SUPPLIED)
        self.assertIs(solution.algorithm_baseline.baseline_kind, BaselineKind.ALGORITHM_GREEDY)
        self.assertEqual(solution.inputs_fingerprint, plan.inputs_fingerprint())
        self.assertEqual(solution.first_service_stop, plan.first_service_stop)
        self.assertIs(solution.provenance, matrix.provenance)
        # The USER baseline is the input order and ignores the selection (v2 section 30).
        expected_user = evaluate_order(
            plan=plan,
            travel_matrix=matrix,
            order=tuple(some_stop.id for some_stop in plan.active_stops()),
        ).metrics
        self.assertEqual(solution.user_baseline.duration_sec, expected_user.duration_sec)
        self.assertEqual(solution.user_baseline.travel_sec, expected_user.travel_sec)
        self.assertEqual(
            solution.metrics.duration_sec,
            solution.metrics.travel_sec
            + solution.metrics.waiting_sec
            + solution.metrics.service_sec,
        )
        self.assertIs(
            solution.status,
            SolutionStatus.HAS_INFEASIBLE_WINDOWS
            if solution.violations
            else SolutionStatus.OK,
        )

    def test_solve_route_requires_a_driver_selected_first_stop(self) -> None:
        plan = build_demo_plan()  # FirstStopIntent.recommend(): awaiting the driver's choice
        self.assertIsNone(plan.first_service_stop.selected_stop_id)
        with self.assertRaises(InvalidRoutePlanError):
            solve_route(plan=plan, travel_matrix=demo_matrix())

    def test_solve_commits_the_same_route_as_solve_route(self) -> None:
        plan = _demo_plan_with_selected_first_stop("S01-NEAR")
        matrix = demo_matrix()

        via_solve = solve(plan=plan, travel_matrix=matrix)
        via_solve_route = solve_route(plan=plan, travel_matrix=matrix)

        self.assertEqual(via_solve.order, via_solve_route.order)
        self.assertEqual(via_solve.metrics, via_solve_route.metrics)
        self.assertEqual(via_solve.algorithm_baseline, via_solve_route.algorithm_baseline)

    def test_build_solution_is_unchanged_and_still_needs_the_selection_first(self) -> None:
        plan = simple_plan(first_service_stop=chosen(STOP_B))
        solution = build_solution(
            plan=plan,
            travel_matrix=FixedTravelMatrix(),
            order=(STOP_B, STOP_A, STOP_C, STOP_D),
            algorithm_order=(STOP_B, STOP_C, STOP_D, STOP_A),
        )
        self.assertEqual(solution.order[0], STOP_B)
        with self.assertRaises(InvalidRoutePlanError):
            build_solution(
                plan=simple_plan(),
                travel_matrix=FixedTravelMatrix(),
                order=(STOP_A, STOP_B, STOP_C, STOP_D),
                algorithm_order=(STOP_A, STOP_B, STOP_C, STOP_D),
            )


class DeterminismAndScaleTests(unittest.TestCase):
    """Repeated runs agree exactly; the 30-stop demo plan has a measured duration."""

    def test_optimize_is_deterministic_across_repeated_runs(self) -> None:
        plan = chaos_plan()
        first = optimize(problem_for(plan, first=STOP_A))
        second = optimize(problem_for(plan, first=STOP_A))

        self.assertEqual(first.order, second.order)
        self.assertEqual(first.evaluation.metrics, second.evaluation.metrics)
        self.assertEqual(
            first.algorithm_evaluation.metrics, second.algorithm_evaluation.metrics
        )
        self.assertEqual(first.local_search, second.local_search)
        self.assertEqual(first.cache_stats, second.cache_stats)
        self.assertEqual(route_fingerprint(plan, first.order), route_fingerprint(plan, second.order))

    def test_algorithm_baseline_is_the_greedy_seed_of_the_same_selection(self) -> None:
        plan = simple_plan(first_service_stop=chosen(STOP_D))
        problem = problem_for(plan, first=STOP_D)
        result = optimize(problem)

        self.assertEqual(result.algorithm_evaluation.order, greedy_seed(problem))
        self.assertEqual(result.algorithm_evaluation.order[0], STOP_D)
        self.assertEqual(result.evaluation.order, result.order)
        # The seed's evaluation is labelled by build_solution as the internal ALGORITHM baseline.
        solution = build_solution(
            plan=plan,
            travel_matrix=problem.legs,
            order=result.order,
            algorithm_order=result.algorithm_evaluation.order,
        )
        self.assertIs(
            solution.algorithm_baseline.baseline_kind, BaselineKind.ALGORITHM_GREEDY
        )
        self.assertNotEqual(solution.algorithm_baseline.duration_sec, -1)

    def test_thirty_stop_demo_optimize_duration_is_measured(self) -> None:
        plan = _demo_plan_with_selected_first_stop("S05-FARTHEST")
        matrix = demo_matrix()

        started = timer.perf_counter()
        problem = problem_for(plan, first=StopId("S05-FARTHEST"), matrix=matrix)
        result = optimize(problem)
        elapsed = timer.perf_counter() - started

        self.assertEqual(len(result.order), 30)
        self.assertEqual(result.order[0], StopId("S05-FARTHEST"))
        print(
            f"[benchmark] 30-stop optimize: {elapsed:.3f}s, "
            f"{result.local_search.evaluations} route evaluations, "
            f"{len(result.local_search.accepted_moves)} accepted moves, "
            f"{result.cache_stats.describe()}",
            file=sys.stderr,
        )
        self.assertLess(elapsed, 120.0, "a 30-stop optimize must stay an interactive operation")

    def test_problem_requires_an_enabled_selected_first_stop(self) -> None:
        plan = simple_plan()
        from core.engine.optimizer import RouteProblem as ExportedProblem

        self.assertIs(ExportedProblem, RouteProblemClass)
        with self.assertRaises(InvalidRoutePlanError):
            problem_for(plan, first=STOP_OFF)
        with self.assertRaises(InvalidRoutePlanError):
            problem_for(plan, first=StopId("NOPE"))


def _demo_plan_with_selected_first_stop(stop_id: str):
    """The deterministic 30-stop demo plan with an explicit driver selection (D32)."""
    return dataclasses.replace(
        build_demo_plan(), first_service_stop=FirstStopIntent.manual_choice(stop_id)
    )


if __name__ == "__main__":
    unittest.main()
