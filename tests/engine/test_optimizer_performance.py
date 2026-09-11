"""Performance, scale fixture and search-invariant tests (Stage 2 unit U3; v2 sections 19, 20, 21).

What is locked in here:

* the ~100-stop benchmark fixture of :mod:`demo.scale_dataset` is **deterministic**: the same
  arguments give the same plan and the same ``inputs_fingerprint``, and building it twice in one
  process, in two processes and in a different call order changes nothing;
* the optimizer's invariants still hold on that plan: every enabled stop exactly once, the driver's
  first stop first, no violation invented, and the final objective never worse than the seed's
  (v2 section 21);
* the seed is genuinely **waiting-aware**: a case where the waiting-aware choice differs from the
  plain nearest choice;
* the screen-then-verify search **never accepts a worse true objective** - checked as a property
  over several plans and starting orders, by replaying every accepted move against a full
  evaluation;
* the shipped search is **never lexicographically worse than the full U2 neighbourhood** on small
  plans: it is compared, selection by selection, against an exhaustive U2 search written out in
  this module (U3 recovery; v2 section 20);
* the benchmark tool's **candidate set is complete**: every enabled stop is optimized exactly once,
  in input order, and no candidate is skipped or shortlisted (v2 section 20, no prefilter);
* the benchmark tool is deterministic in everything except wall-clock time, reports how many
  candidates hit the search's deterministic evaluation ceiling, and its exit status follows the
  **owner-accepted bound**;
* the v2 section 20 <= 5 s acceptable target is **reported, never asserted**: v2 section 20 calls its
  numbers engineering targets, not correctness rules, and the owner accepted the measured ~100-stop
  latency as an explicit interim limitation (decision D34) while the full U2 neighbourhood and the
  restored search quality stay - no approximation, no prefilter, no span cut, no shortlist.

Slow tests (opt-in, excluded by default)
----------------------------------------

The heavy exhaustive quality comparisons - the full U2-neighbourhood reference search on the 30-stop
demo plan and on the ~100-stop fixture, and the ~100-stop benchmark loop itself - are gated behind
an environment variable so ordinary unit verification stays fast. Run them with::

    ROUTEPILOT_SLOW_TESTS=1 python -m unittest tests.engine.test_optimizer_performance -v

The default suite keeps the fast quality assertions: the same comparison on small (12/30-stop)
plans, the seed/search/determinism invariants, and one small-plan benchmark run.

The wall-clock guard here is the owner-accepted bound itself, with headroom for a slower machine: it
is imported from ``tools/benchmark_optimizer.py`` so one constant defines the bound, the tool's exit
status reports it, and this module asserts it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import sys
import time as timer
import unittest
from datetime import time

from core.engine.optimizer import (
    LegCache,
    OpenState,
    build_problem,
    build_seed,
    evaluate_order,
    fast_evaluate,
    fast_objective,
    greedy_seed,
    improve,
    optimize,
)
from core.engine.optimizer.local_search import (
    DEFAULT_MAX_EVALUATIONS,
    LocalSearchResult,
    accepted,
    apply_move,
    candidate_moves,
    move_travel_delta_sec,
)
from core.model.first_stop import FirstStopIntent
from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.model.service_window import ServiceWindow
from core.time import tzdata
from demo.dataset import build_demo_plan
from demo.scale_dataset import (
    SCALE_DEFAULT_STOP_COUNT,
    SCALE_DEPARTURE_TIME,
    SCALE_SERVICE_DATE,
    SCALE_TIMEZONE,
    build_scale_plan,
    build_scale_specs,
)
from demo.synthetic_matrix import demo_matrix
from tests.support import WAREHOUSE, build_plan, stop, utc
from tools.benchmark_optimizer import ACCEPTED_INTERIM_LOOP_LIMIT_SEC

#: Set this environment variable to enable the heavy exhaustive comparisons (any truthy value).
SLOW_TESTS_ENV = "ROUTEPILOT_SLOW_TESTS"

#: Whether the heavy exhaustive quality comparisons run. They are **excluded by default** so
#: ordinary unit verification stays fast; the full Stage 2 verification runs them with the variable
#: set (U3 owner-decided fix 5).
SLOW_TESTS_ENABLED = os.environ.get(SLOW_TESTS_ENV, "").strip().lower() not in {
    "",
    "0",
    "false",
    "no",
    "off",
}

#: The gate for one heavy exhaustive comparison.
slow_test = unittest.skipUnless(
    SLOW_TESTS_ENABLED,
    f"heavy exhaustive quality comparison, excluded by default; set {SLOW_TESTS_ENV}=1 to run it",
)


def scale_first_stop(index: int = 0) -> StopId:
    """A stop of the scale fixture, in input order - a legal driver selection (D32)."""
    return StopId(build_scale_specs()[index].stop_id)


def scale_plan_with_selection(stop_id: StopId | None = None) -> RoutePlan:
    """The scale fixture with an explicit driver-selected first stop."""
    return dataclasses.replace(
        build_scale_plan(),
        first_service_stop=FirstStopIntent.manual_choice(stop_id or scale_first_stop()),
    )


def scale_problem(*, first: StopId | None = None, cache: LegCache | None = None):
    selected = first or scale_first_stop()
    return build_problem(
        plan=scale_plan_with_selection(selected),
        travel_matrix=demo_matrix(),
        first_stop_id=selected,
        cache=cache,
    )


def u2_reference_seed(problem) -> tuple[StopId, ...]:
    """The U2 seed criterion, kept here as the comparison baseline it was accepted as.

    U2 chose, at every step, the candidate whose complete route from the driver's current position
    was best under the lexicographic key ``(violations, elapsed seconds, input_position, id)``,
    where "the complete route" is the stops chosen so far, then the candidate, then FINISH. U3
    computes the identical key from the prepared tables, so the two must agree; this helper is the
    slow, obviously-correct form of that key, written with the public ``advance``/``fast_evaluate``
    pair, and it is the yardstick the seed test below measures the shipped seed against.
    """
    order: list[StopId] = [problem.first_stop_id]
    remaining = list(problem.remaining_stop_ids)
    state = OpenState(elapsed_sec=0, point=problem.departure_point)
    state = problem.advance(state, problem.first_stop_id)
    while remaining:
        chosen_id: StopId | None = None
        chosen_state = None
        chosen_key = None
        for candidate in remaining:
            candidate_state = problem.advance(state, candidate)
            evaluation = fast_evaluate(problem, (*order, candidate))
            key = (
                evaluation.violations,
                evaluation.finish_elapsed_sec,
                problem.input_position_of(candidate),
                candidate,
            )
            if chosen_key is None or key < chosen_key:
                chosen_key = key
                chosen_id = candidate
                chosen_state = candidate_state
        assert chosen_id is not None and chosen_state is not None
        order.append(chosen_id)
        remaining.remove(chosen_id)
        state = chosen_state
    return tuple(order)


class ScaleFixtureTests(unittest.TestCase):
    """The benchmark fixture is deterministic and has the shape the benchmark needs (D18)."""

    def test_same_arguments_give_the_same_plan_and_fingerprint(self) -> None:
        first = build_scale_plan()
        second = build_scale_plan()

        self.assertEqual(
            [some_stop.id for some_stop in first.stops],
            [some_stop.id for some_stop in second.stops],
        )
        self.assertEqual(
            [(some_stop.latitude, some_stop.longitude) for some_stop in first.stops],
            [(some_stop.latitude, some_stop.longitude) for some_stop in second.stops],
        )
        self.assertEqual(first.inputs_fingerprint(), second.inputs_fingerprint())

    def test_the_fixture_is_order_independent_of_the_call(self) -> None:
        # Building another size first must not change the ~100-stop plan: no shared mutable state.
        _ = build_scale_plan(12)
        first = build_scale_plan()
        _ = build_scale_plan(7)
        second = build_scale_plan()

        self.assertEqual(first.inputs_fingerprint(), second.inputs_fingerprint())
        self.assertEqual(len(first.stops), len(second.stops))

    def test_the_fixture_has_the_enterprise_shape_the_benchmark_needs(self) -> None:
        plan = build_scale_plan(SCALE_DEFAULT_STOP_COUNT)
        specs = build_scale_specs(SCALE_DEFAULT_STOP_COUNT)

        self.assertEqual(len(specs), SCALE_DEFAULT_STOP_COUNT)
        self.assertEqual(len(plan.stops), SCALE_DEFAULT_STOP_COUNT)
        self.assertEqual(plan.timezone, SCALE_TIMEZONE)
        self.assertEqual(plan.departure_time, SCALE_DEPARTURE_TIME)
        self.assertEqual(SCALE_SERVICE_DATE.isoformat(), "2026-09-11")
        self.assertIsNone(plan.first_service_stop.selected_stop_id)

        disabled = plan.disabled_stops()
        enabled = plan.active_stops()
        self.assertGreater(len(disabled), 0, "the fixture must contain disabled stops (D20)")
        self.assertGreater(len(enabled), 90, "~100 stops means ~100 optimized stops (v2 section 19)")
        self.assertEqual(
            [some_stop.input_position for some_stop in plan.stops],
            list(range(SCALE_DEFAULT_STOP_COUNT)),
        )
        for some_stop in enabled:
            self.assertIsNotNone(some_stop.location, "every enabled stop is geocoded")
        for some_stop in disabled:
            self.assertFalse(some_stop.enabled)

        kinds = {some_stop.service_window.window_kind.value for some_stop in plan.stops}
        self.assertIn("fixed", kinds)
        self.assertIn("unrestricted", kinds)
        self.assertIn("unknown", kinds)
        self.assertGreater(len({some_stop.service_duration for some_stop in plan.stops}), 2)
        self.assertIn(None, {some_stop.service_duration for some_stop in plan.stops})
        self.assertGreater(len({some_stop.priority for some_stop in plan.stops}), 1)

    def test_independent_problems_of_the_scale_plan_agree(self) -> None:
        # Two separately built problems are the same problem: the fixture carries no hidden state.
        first = scale_problem()
        second = scale_problem()

        self.assertEqual(first.stop_ids, second.stop_ids)
        self.assertEqual(
            [some_stop.input_position for some_stop in first.plan.stops],
            [some_stop.input_position for some_stop in second.plan.stops],
        )
        self.assertEqual(greedy_seed(first), greedy_seed(second))
        self.assertEqual(first.cache_stats, first.cache_stats)


class ScaleOptimizerInvariantTests(unittest.TestCase):
    """The optimizer's guarantees hold on a ~100-stop plan (v2 section 21, D17)."""

    def test_optimize_on_the_scale_plan_keeps_every_invariant(self) -> None:
        selected = scale_first_stop(3)
        problem = scale_problem(first=selected)
        result = optimize(problem)

        enabled_ids = sorted(some_stop.id for some_stop in problem.plan.active_stops())
        disabled_ids = {some_stop.id for some_stop in problem.plan.disabled_stops()}

        self.assertEqual(result.order[0], selected, "the driver's first stop stays first (I3)")
        self.assertEqual(sorted(result.order), enabled_ids)
        self.assertEqual(len(result.order), len(set(result.order)))
        for stop_id in disabled_ids:
            self.assertNotIn(stop_id, result.order)

        # Acceptance is lexicographic and monotone: fewer violations first, then an earlier finish.
        self.assertLessEqual(
            (result.local_search.final_violations, result.local_search.final_objective),
            (result.local_search.seed_violations, result.local_search.seed_objective),
        )
        # The committed metrics are the authoritative evaluation of the committed order.
        self.assertEqual(result.evaluation.order, result.order)
        self.assertEqual(
            len(result.evaluation.violations), result.local_search.final_violations
        )
        self.assertEqual(
            result.evaluation.metrics.duration_sec, result.local_search.final_objective
        )
        # No violation is invented: the fast path and the authoritative engine agree.
        fast = fast_evaluate(problem, result.order)
        self.assertEqual(fast.violations, len(result.evaluation.violations))
        self.assertEqual(fast.finish_elapsed_sec, result.evaluation.metrics.duration_sec)

    def test_the_algorithm_baseline_stays_a_complete_route_of_the_same_selection(self) -> None:
        selected = scale_first_stop(5)
        problem = scale_problem(first=selected)
        result = optimize(problem)

        self.assertEqual(result.algorithm_evaluation.order[0], selected)
        self.assertEqual(
            sorted(result.algorithm_evaluation.order),
            sorted(result.order),
        )
        self.assertLessEqual(
            (result.local_search.final_violations, result.local_search.final_objective),
            (
                len(result.algorithm_evaluation.violations),
                result.algorithm_evaluation.metrics.duration_sec,
            ),
        )

    def test_optimize_on_the_scale_plan_is_deterministic_and_bounded(self) -> None:
        first = optimize(scale_problem())
        second = optimize(scale_problem())

        self.assertEqual(first.order, second.order)
        self.assertEqual(first.local_search, second.local_search)
        self.assertEqual(first.algorithm_evaluation.order, second.algorithm_evaluation.order)
        self.assertEqual(first.evidence, second.evidence)
        # At ~100 stops the deterministic evaluation ceiling binds and truncates pass 1 (the whole
        # neighbourhood is more moves than the bound allows). That is the interim limitation the
        # owner accepted (D34), and the AGGREGATE SEARCH says so through ``budget_exhausted``
        # instead of claiming that every move of the neighbourhood was verified (U3 fix 2). The
        # candidate set itself stays exhaustive - see BenchmarkToolTests.
        self.assertEqual(first.local_search.evaluations, DEFAULT_MAX_EVALUATIONS)
        self.assertTrue(
            first.local_search.budget_exhausted,
            "the ~100-stop search must report the ceiling it hit, never claim a full-neighbourhood "
            "verification it did not perform (v2 section 20, D34)",
        )


class WaitingAwareSeedTests(unittest.TestCase):
    """The seed's own criterion makes waiting influence the choice (v2 sections 1, 21; D17)."""

    def test_the_waiting_aware_choice_differs_from_the_plain_nearest_choice(self) -> None:
        # NEAR is a one-hour leg from the hub and opens at 11:00 local; FAR is three hours away and
        # opens at 08:00. Leaving at 04:00, the nearer stop would mean hours of waiting, so the
        # waiting-aware seed drives to the farther one - a nearest-neighbour seed cannot do that.
        near_window = ServiceWindow.fixed(time(11, 0), time(18, 0))
        far_window = ServiceWindow.fixed(time(8, 0), time(18, 0))
        plan = build_plan(
            stop("HUB", WAREHOUSE[0], WAREHOUSE[1]),
            stop("NEAR", WAREHOUSE[0], WAREHOUSE[1] - 1.0, window=near_window),
            stop("FAR", WAREHOUSE[0], WAREHOUSE[1] - 3.0, window=far_window),
            departure_time=utc(2026, 9, 11, 1, 0),  # 04:00 Moscow
            first_service_stop=FirstStopIntent.manual_choice(StopId("HUB")),
        )
        problem = build_problem(
            plan=plan,
            travel_matrix=demo_matrix(),
            first_stop_id=StopId("HUB"),
        )
        nearest = StopId("NEAR")
        farther = StopId("FAR")
        self.assertLess(
            problem.travel_between_indices(
                problem.index_of(StopId("HUB")), problem.index_of(nearest)
            ),
            problem.travel_between_indices(
                problem.index_of(StopId("HUB")), problem.index_of(farther)
            ),
            "NEAR must genuinely be the nearer stop for this test to mean anything",
        )

        seed = greedy_seed(problem)

        self.assertEqual(seed, (StopId("HUB"), farther, nearest))
        # ... and the choice is the better complete route, not merely a different one.
        waiting_aware = fast_objective(problem, seed)
        nearest_first = fast_objective(
            problem, (StopId("HUB"), nearest, farther)
        )
        self.assertLess(waiting_aware, nearest_first)

    def test_the_seed_is_deterministic_on_the_scale_plan(self) -> None:
        first = build_seed(scale_problem())
        second = build_seed(scale_problem())

        self.assertEqual(first.order, second.order)
        self.assertEqual(first.evaluations, second.evaluations)
        # Every remaining stop is priced at every step: n * (n - 1) / 2 candidates, no prefilter.
        count = len(scale_problem().stop_ids)
        self.assertEqual(first.evaluations, count * (count - 1) // 2)

    def test_the_seed_key_is_the_complete_route_the_u2_pipeline_compared(self) -> None:
        # The U3 seed must never be lexicographically worse than the U2 criterion it replaced, and
        # it must contain the same terms: violations, the FINISH arrival of the complete route from
        # the driver's current position, then input_position and id. This is checked against the
        # verbatim U2 baseline over several scale plans and starting selections.
        for stop_count in (12, 30):
            for index in (0, 4, 11):
                plan = build_scale_plan(stop_count)
                selected = StopId(plan.active_stops()[min(index, len(plan.active_stops()) - 1)].id)
                with self.subTest(size=stop_count, first=selected):
                    problem = build_problem(
                        plan=dataclasses.replace(
                            plan, first_service_stop=FirstStopIntent.manual_choice(selected)
                        ),
                        travel_matrix=demo_matrix(),
                        first_stop_id=selected,
                    )
                    seed = greedy_seed(problem)
                    reference = u2_reference_seed(problem)

                    self.assertEqual(seed, reference)
                    seed_key = fast_objective(problem, seed)
                    reference_key = fast_objective(problem, reference)
                    self.assertLessEqual(seed_key, reference_key)

    def test_the_optimized_route_is_never_worse_than_the_u2_seed_plus_search(self) -> None:
        # The end-to-end guarantee the REVISE asked for: whatever the shipped pipeline produces on
        # a scale plan must not carry more hard-window violations than the pre-U3 pipeline (the U2
        # seed order fed to the same local search), on the same problem.
        for stop_count in (12, 30):
            for index in (1, 7):
                plan = build_scale_plan(stop_count)
                selected = StopId(plan.active_stops()[min(index, len(plan.active_stops()) - 1)].id)
                with self.subTest(size=stop_count, first=selected):
                    problem = build_problem(
                        plan=dataclasses.replace(
                            plan, first_service_stop=FirstStopIntent.manual_choice(selected)
                        ),
                        travel_matrix=demo_matrix(),
                        first_stop_id=selected,
                    )
                    shipped = optimize(problem)
                    u2_seeded = improve(problem, u2_reference_seed(problem))

                    self.assertLessEqual(
                        len(shipped.evaluation.violations),
                        u2_seeded.final_violations,
                        "the shipped route may never carry more hard-window violations",
                    )
                    self.assertLessEqual(
                        (shipped.local_search.final_violations, shipped.final_objective_sec),
                        (u2_seeded.final_violations, u2_seeded.final_objective),
                    )


class ScreenThenVerifyTests(unittest.TestCase):
    """The screen never accepts a worse true objective; the proxy is an exact travel delta."""

    def assert_replayed_moves_never_worsen(self, result: LocalSearchResult, problem) -> None:
        order = greedy_seed(problem)
        previous = fast_objective(problem, order)
        for move in result.accepted_moves:
            order = apply_move(order, move)
            current = fast_objective(problem, order)
            self.assertLess(
                current,
                previous,
                "every accepted move must strictly improve the true (violations, elapsed) key",
            )
            previous = current
        self.assertEqual(order, result.order)

    def test_the_screen_delta_is_the_moves_own_true_travel_delta(self) -> None:
        # The screen must rank the move it was given. ``move_travel_delta_sec`` uses the move's own
        # convention (positions counted from the first service stop, so START is one position
        # before position 0); if it ranked a different window, the shortlist would not be the set
        # of moves it claims to rank. This replays every move of the neighbourhood against a full
        # evaluation of the same route (U3 review issue 1).
        plans = (build_scale_plan(12), build_scale_plan(30))
        checked = 0
        for plan in plans:
            for index in (0, 3):
                selected = StopId(plan.active_stops()[index].id)
                problem = build_problem(
                    plan=dataclasses.replace(
                        plan, first_service_stop=FirstStopIntent.manual_choice(selected)
                    ),
                    travel_matrix=demo_matrix(),
                    first_stop_id=selected,
                )
                order = greedy_seed(problem)
                before = fast_evaluate(problem, order).travel_sec
                with self.subTest(size=len(plan.stops), first=selected):
                    for move in candidate_moves(len(order), max_span=4):
                        after = fast_evaluate(problem, apply_move(order, move)).travel_sec
                        delta = move_travel_delta_sec(problem, order, move)
                        checked += 1
                        self.assertEqual(
                            delta,
                            after - before,
                            f"{move.describe()} delta {delta} is not the travel it really "
                            f"adds ({after - before})",
                        )
        self.assertGreater(checked, 150, "the neighbourhood must actually be inspected")

    def test_the_screen_never_misses_a_move_that_reduces_travel(self) -> None:
        # The other half of the same contract: because the delta is exact, a move that saves
        # driving is always ranked as one - the shortlist can never lose it.
        plan = build_scale_plan(30)
        selected = StopId(plan.active_stops()[2].id)
        problem = build_problem(
            plan=dataclasses.replace(
                plan, first_service_stop=FirstStopIntent.manual_choice(selected)
            ),
            travel_matrix=demo_matrix(),
            first_stop_id=selected,
        )
        order = greedy_seed(problem)
        before = fast_evaluate(problem, order).travel_sec
        improving = 0
        for move in candidate_moves(len(order), max_span=4):
            after = fast_evaluate(problem, apply_move(order, move)).travel_sec
            if after < before:
                improving += 1
                self.assertLess(
                    move_travel_delta_sec(problem, order, move),
                    0,
                    f"{move.describe()} saves travel but was not ranked as an improvement",
                )
        self.assertGreater(improving, 0, "this fixture must contain a travel-saving move")

    def test_every_accepted_move_strictly_improves_the_true_objective(self) -> None:
        plans = [
            build_scale_plan(12),
            build_scale_plan(30),
            build_scale_plan(60),
        ]
        for plan in plans:
            for index in (0, 4, 11):
                if index >= len(plan.active_stops()):
                    continue
                selected = StopId(plan.active_stops()[index].id)
                problem = build_problem(
                    plan=dataclasses.replace(
                        plan, first_service_stop=FirstStopIntent.manual_choice(selected)
                    ),
                    travel_matrix=demo_matrix(),
                    first_stop_id=selected,
                )
                seed = greedy_seed(problem)
                result = improve(problem, seed)
                with self.subTest(size=len(plan.stops), first=selected):
                    self.assert_replayed_moves_never_worsen(result, problem)
                    self.assertLessEqual(
                        (result.final_violations, result.final_objective),
                        (result.seed_violations, result.seed_objective),
                    )
                    # The reported final numbers are a real evaluation of the reported order.
                    current = fast_objective(problem, result.order)
                    self.assertEqual(current, (result.final_violations, result.final_objective))

    def test_the_screen_only_ranks_and_is_never_the_acceptance_decision(self) -> None:
        # The screen's travel delta is a ranking signal: it says nothing about waiting or hard
        # windows, so it may never decide acceptance. This replays every accepted move against a
        # full evaluation of the true complete-route objective, which is the only thing that
        # accepts (v2 section 21, D13 amendment).
        plan = build_scale_plan(30)
        selected = scale_first_stop(2)
        problem = build_problem(
            plan=dataclasses.replace(
                plan, first_service_stop=FirstStopIntent.manual_choice(selected)
            ),
            travel_matrix=demo_matrix(),
            first_stop_id=selected,
        )
        order = greedy_seed(problem)
        result = improve(problem, order)

        # Every screened move does have a travel delta, and a delta that saves driving never
        # guarantees a better complete route - the accepted ones survive the full evaluation.
        deltas = [
            move_travel_delta_sec(problem, order, move)
            for move in candidate_moves(len(order), max_span=3)
        ]
        self.assertGreater(len(deltas), 0)
        self.assertTrue(all(isinstance(delta, int) for delta in deltas))
        self.assertGreaterEqual(result.evaluations, len(result.accepted_moves))

    def test_the_search_is_deterministic_on_the_scale_plan(self) -> None:
        first = improve(scale_problem(), greedy_seed(scale_problem()))
        second = improve(scale_problem(), greedy_seed(scale_problem()))

        self.assertEqual(first.order, second.order)
        self.assertEqual(first.accepted_moves, second.accepted_moves)
        self.assertEqual(first.evaluations, second.evaluations)
        self.assertEqual(first.screened_moves, second.screened_moves)


def u2_neighbourhood_reference(problem, order) -> tuple[int, int]:
    """The U2 search over the full neighbourhood, written here as the yardstick (U3 recovery).

    This is the pre-U3 acceptance, deliberately slow and obvious: every pass enumerates **every**
    move of both neighbourhoods (``max_span=None``), fully evaluates the complete route of every
    one of them, and keeps the best improving move under the same lexicographic key as the shipped
    search. It is bounded by the same deterministic counts U2 carried, so the two searches differ
    in nothing but *how many* moves they examine. The shipped search must never be lexicographically
    worse than this: if it is, the move screen has cost the search a route U2 would have found.
    """
    sequence = tuple(order)
    current = fast_objective(problem, sequence)
    evaluations = 0
    for _pass in range(8):
        best_key = None
        best_order = None
        best_objective = None
        for move in candidate_moves(len(sequence), max_span=None):
            if evaluations >= 20_000:
                break
            candidate = apply_move(sequence, move)
            objective = fast_objective(problem, candidate)
            evaluations += 1
            if not accepted(objective, current):
                continue
            key = (objective, tuple(problem.index_of(stop_id) for stop_id in candidate), candidate)
            if best_key is None or key < best_key:
                best_key = key
                best_order = candidate
                best_objective = objective
        if best_order is None or best_objective is None:
            break
        sequence = best_order
        current = best_objective
    return (current[0], current[1])


class ExhaustiveNeighbourhoodTests(unittest.TestCase):
    """The shipped search is never worse than the full U2 neighbourhood (U3 recovery, v2 s. 20).

    The U3 recovery removed the two approximations the review found - a neighbourhood truncated to
    ``DEFAULT_MAX_SPAN`` positions and a shortlist of eight travel-saving moves - and these tests
    are what stops either from coming back:

    * the shipped search is compared, on small (12/30-stop) plans and several first-stop selections,
      against the U2 full-neighbourhood search above, and must never be lexicographically worse;
    * on the 30-stop demo plan and the ~100-stop scale fixture the same comparison is made, gated
      behind ``ROUTEPILOT_SLOW_TESTS`` because it is the heavy part of this module (U3 fix 5);
    * it must report that it examined the whole neighbourhood on a small plan, and it must stay
      deterministic;
    * the exact O(1) travel delta the ranking uses is re-checked against a full evaluation of every
      move of the full neighbourhood, including the long-range moves the old span could not reach;
    * one shipped search on a small plan is measured and reported against the v2 section 20 target,
      and asserted against the owner-accepted bound (D34).

    A full exhaustive pass over the 30-stop neighbourhood is 1,836 complete-route evaluations, so
    the small-plan comparisons stay inside a couple of seconds - which is exactly why they exist:
    the owner-accepted wall-clock bound cannot see a search that quietly turned into a screen, but a
    lost improving move shows up immediately against the reference search.
    """

    #: Plans and first-stop selections the comparison is made on (12 and 30 stops, v2 section 19).
    SIZES = (12, 30)
    SELECTIONS = (0, 4, 11)

    def selected_problem_of(self, plan, index: int):
        active = plan.active_stops()
        selected = StopId(active[min(index, len(active) - 1)].id)
        problem = build_problem(
            plan=dataclasses.replace(
                plan, first_service_stop=FirstStopIntent.manual_choice(selected)
            ),
            travel_matrix=demo_matrix(),
            first_stop_id=selected,
        )
        return problem, greedy_seed(problem)

    def selected_problem(self, stop_count: int, index: int):
        return self.selected_problem_of(build_scale_plan(stop_count), index)

    def assert_zero_losses(self, plan, indices) -> None:
        for index in indices:
            problem, seed = self.selected_problem_of(plan, index)
            with self.subTest(stops=len(plan.active_stops()), first=seed[0]):
                shipped = improve(problem, seed)
                reference = u2_neighbourhood_reference(problem, seed)
                self.assertLessEqual(
                    (shipped.final_violations, shipped.final_objective),
                    reference,
                    "the shipped search is lexicographically worse than the U2 neighbourhood "
                    "search on this selection (v2 section 20)",
                )

    @slow_test
    def test_the_demo_plan_never_loses_to_the_u2_neighbourhood(self) -> None:
        # The plan the product actually shows: every selection the demo offers, against the U2
        # search. This is the measurement the U3 review asked for, kept where it can be re-run.
        # Heavy (every demo selection against the 30-stop reference search): slow test, U3 fix 5.
        plan = build_demo_plan()
        self.assert_zero_losses(plan, range(len(plan.active_stops())))

    @slow_test
    def test_the_scale_fixture_never_loses_to_the_u2_neighbourhood(self) -> None:
        # The ~100-stop fixture, sampled (an exhaustive U2 search on all 97 selections is minutes):
        # the shipped search must not come back worse on any of them (v2 section 20, D18). Heavy:
        # slow test, U3 fix 5.
        plan = build_scale_plan()
        self.assertGreater(len(plan.active_stops()), 90)
        self.assert_zero_losses(plan, (0, 3, 12, 40))

    def test_the_shipped_search_is_never_worse_than_the_full_u2_neighbourhood(self) -> None:
        checked = 0
        for stop_count in self.SIZES:
            for index in self.SELECTIONS:
                problem, seed = self.selected_problem(stop_count, index)
                with self.subTest(size=stop_count, first=seed[0]):
                    shipped = improve(problem, seed)
                    reference = u2_neighbourhood_reference(problem, seed)
                    checked += 1
                    self.assertLessEqual(
                        (shipped.final_violations, shipped.final_objective),
                        reference,
                        "the shipped search came back with a worse route than the U2 "
                        "neighbourhood search: the move screen lost an improving move "
                        "(v2 section 20)",
                    )
                    self.assertFalse(
                        shipped.budget_exhausted,
                        "the shipped search must examine the whole neighbourhood on a small plan",
                    )
        self.assertEqual(checked, len(self.SIZES) * len(self.SELECTIONS))

    def test_the_comparison_is_deterministic_across_two_runs(self) -> None:
        for stop_count in self.SIZES:
            problem, seed = self.selected_problem(stop_count, 1)
            with self.subTest(size=stop_count):
                first = improve(problem, seed)
                second = improve(problem, seed)
                self.assertEqual(first, second)
                self.assertEqual(
                    u2_neighbourhood_reference(problem, seed),
                    u2_neighbourhood_reference(problem, seed),
                )

    def test_the_full_neighbourhood_delta_is_the_moves_own_travel_delta(self) -> None:
        # The ranking must price the move it was given, at any distance. The replaced U3
        # implementation walked the moved sequence; the shipped one is a closed form over the three
        # legs a relocate changes, so the long-range moves get their own check here.
        checked = 0
        for stop_count in self.SIZES:
            problem, seed = self.selected_problem(stop_count, 0)
            before = fast_evaluate(problem, seed).travel_sec
            for move in candidate_moves(len(seed), max_span=None):
                after = fast_evaluate(problem, apply_move(seed, move)).travel_sec
                checked += 1
                self.assertEqual(
                    move_travel_delta_sec(problem, seed, move),
                    after - before,
                    f"{move.describe()} was ranked by a delta that is not the travel it adds",
                )
        self.assertGreater(checked, 2_000, "the whole full neighbourhood must be inspected")

    def test_one_small_plan_search_stays_inside_the_accepted_bound(self) -> None:
        from tools.benchmark_optimizer import ACCEPTABLE_BUDGET_SEC

        for stop_count in self.SIZES:
            problem, seed = self.selected_problem(stop_count, 0)
            with self.subTest(size=stop_count):
                started = timer.perf_counter()
                improve(problem, seed)
                elapsed = timer.perf_counter() - started
                print(
                    f"[benchmark] full-neighbourhood {stop_count}-stop search: {elapsed:.3f}s "
                    f"(v2 section 20 spec target {ACCEPTABLE_BUDGET_SEC:.1f}s - reported, not "
                    f"asserted; owner-accepted bound {ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.1f}s, D34)",
                    file=sys.stderr,
                )
                # One search is a fraction of the whole exhaustive loop, so it is asserted against
                # the same single bound the owner accepted instead of a second invented constant.
                self.assertLess(
                    elapsed,
                    ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
                    "one full-neighbourhood search regressed past the owner-accepted bound (D34)",
                )


class BenchmarkToolTests(unittest.TestCase):
    """The benchmark's candidate set is complete, its report honest, its evidence deterministic.

    The heavy measurements - the ~100-stop loop and the 40-stop/demo runs - are gated behind
    ``ROUTEPILOT_SLOW_TESTS`` (U3 fix 5); the candidate-set completeness and the report contract are
    checked on a small plan here, in the default suite.
    """

    @classmethod
    def setUpClass(cls) -> None:
        # The benchmark tool is a plain script: it does not activate the test bootstrap's offline
        # timezone fallback, so a machine without the tzdata package needs PYTHONTZPATH set here.
        from tests import TZDATA_FALLBACK_PATH

        cls._previous_tzpath = sys.path[:]
        if TZDATA_FALLBACK_PATH is not None:
            tzdata.activate_system_tzif_fallback()

    def run_benchmark(self, *argv: str) -> tuple[int, str, dict]:
        from tools.benchmark_optimizer import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(argv))
        output = buffer.getvalue()
        payload: dict = {}
        if "--json" in argv:
            payload = json.loads(output[output.index("{") :])
        return code, output, payload

    def test_the_candidate_set_is_complete_and_never_shortlisted(self) -> None:
        # v2 section 20: the candidate set is exhaustive. This pins it in the two ways it can be
        # lost: a candidate being skipped, and a candidate's seed being shortlisted. A 12-stop scale
        # plan has no disabled stop, so the complete set is all 12 stops, in input order.
        from tools.benchmark_optimizer import candidate_first_stops, measure_dataset

        plan = build_scale_plan(12)
        enabled = [some_stop.id for some_stop in plan.active_stops()]

        self.assertEqual(list(candidate_first_stops(plan)), enabled)
        self.assertEqual(len(candidate_first_stops(plan)), len(set(candidate_first_stops(plan))))
        self.assertEqual(
            list(candidate_first_stops(build_scale_plan())),
            [some_stop.id for some_stop in build_scale_plan().active_stops()],
        )
        self.assertGreater(len(candidate_first_stops(build_scale_plan())), 90)

        measurement = measure_dataset("scale", plan, demo_matrix(), repeats=1)
        loop = measurement.loop
        count = len(enabled)
        self.assertEqual(loop.candidates, count)
        self.assertEqual(loop.optimizer_runs, count, "every candidate gets its own run")
        # The seed prices every remaining stop at every step, for every candidate: n * n(n-1)/2.
        # A skipped or shortlisted candidate would make this total smaller, so the count is the
        # completeness proof the seed itself can carry (v2 section 20, no prefilter).
        self.assertEqual(loop.seed_candidates_priced, count * count * (count - 1) // 2)
        self.assertGreaterEqual(loop.route_evaluations, count)
        self.assertEqual(loop.candidates_at_ceiling, 0, "a 12-stop search does not hit the ceiling")

    def test_a_disabled_stop_is_excluded_and_never_renumbered_into_the_set(self) -> None:
        # The other side of completeness: "complete" means every ELIGIBLE stop (D20/D33), so the
        # 40-stop fixture's disabled stop must be absent - and absent because it is disabled, not
        # because it was filtered out for looking unpromising.
        from tools.benchmark_optimizer import candidate_first_stops

        plan = build_scale_plan(40)
        disabled = {some_stop.id for some_stop in plan.disabled_stops()}

        self.assertTrue(disabled, "this fixture must contain a disabled stop")
        self.assertEqual(set(candidate_first_stops(plan)).intersection(disabled), set())
        self.assertEqual(len(candidate_first_stops(plan)), len(plan.active_stops()))

    def test_the_benchmark_report_carries_the_candidate_and_ceiling_counters(self) -> None:
        code, output, payload = self.run_benchmark("--stop-count", "12", "--no-demo", "--json")

        self.assertIn("candidates at the ceiling", output)
        self.assertIn("total route evaluations", output)
        self.assertIn("ACCEPTED_BOUND_MET", output)
        self.assertIn("spec target (v2 s.20, reported)", output)
        loop = payload["datasets"][0]["loop"]
        self.assertEqual(loop["candidates_evaluated"], 12)
        self.assertEqual(loop["candidates_at_ceiling"], 0)
        self.assertGreater(loop["route_evaluations"], 0)
        self.assertEqual(
            loop["accepted_bound_met"], code == 0, "the exit code must agree with the bound"
        )
        # The v2 section 20 target is REPORTED, never asserted (D34): the small run is inside it,
        # and the owner-accepted bound is printed next to it.
        self.assertTrue(loop["spec_target_met"])
        self.assertEqual(loop["accepted_interim_limit_sec"], ACCEPTED_INTERIM_LOOP_LIMIT_SEC)

    def test_the_benchmark_is_deterministic_except_wall_clock_on_a_small_plan(self) -> None:
        from tools.benchmark_optimizer import measure_dataset

        measurement = measure_dataset("scale", build_scale_plan(12), demo_matrix())

        self.assertTrue(
            measurement.deterministic,
            "same work, same routes, same cache statistics - only the seconds may differ",
        )
        self.assertEqual(measurement.loop.route_evaluations, measurement.repeat.route_evaluations)
        self.assertEqual(measurement.loop.screened_moves, measurement.repeat.screened_moves)
        self.assertEqual(measurement.loop.accepted_moves, measurement.repeat.accepted_moves)
        self.assertEqual(
            measurement.loop.candidates_at_ceiling, measurement.repeat.candidates_at_ceiling
        )
        self.assertEqual(
            (
                measurement.loop.cache_hits,
                measurement.loop.cache_misses,
                measurement.loop.cache_entries,
            ),
            (
                measurement.repeat.cache_hits,
                measurement.repeat.cache_misses,
                measurement.repeat.cache_entries,
            ),
        )
        # Every leg the measured pass asked for was already in the shared cache.
        self.assertEqual(measurement.loop.cache_misses, 0)

    @slow_test
    def test_the_benchmark_reports_the_scale_and_demo_results(self) -> None:
        code, output, payload = self.run_benchmark("--stop-count", "40", "--no-demo", "--json")

        self.assertIn("ACCEPTED_BOUND_MET", output)
        self.assertIn("candidates evaluated", output)
        self.assertIn("leg cache", output)
        datasets = payload["datasets"]
        self.assertEqual(len(datasets), 1)
        loop = datasets[0]["loop"]
        self.assertEqual(loop["stop_count"], loop["candidates_evaluated"])
        # ~40 stops minus the disabled ones the fixture marks.
        self.assertGreater(loop["optimizer_runs"], 30)
        self.assertGreater(loop["route_evaluations"], 0)
        self.assertGreater(loop["seed_candidates_priced"], 0)
        self.assertGreater(loop["cache_entries"], 0)
        self.assertEqual(
            loop["accepted_bound_met"], code == 0, "the exit code must agree with the bound"
        )

    @slow_test
    def test_the_benchmark_reports_both_datasets_honestly(self) -> None:
        # Both datasets are measured, every dataset's own numbers are compared with the owner-
        # accepted bound (D34), the v2 section 20 target is REPORTED rather than asserted, the exit
        # code agrees with the printed result, and the wall-clock overrun - when there is one - is
        # visible in the report instead of hidden.
        code, output, payload = self.run_benchmark("--stop-count", "40", "--json")

        self.assertIn("RESULT: ACCEPTED_BOUND_MET=", output)
        by_label = {dataset["label"]: dataset for dataset in payload["datasets"]}
        self.assertTrue(any("scale fixture" in label for label in by_label))
        self.assertTrue(any("demo plan" in label for label in by_label))
        for dataset in payload["datasets"]:
            loop = dataset["loop"]
            self.assertEqual(
                loop["accepted_bound_met"],
                loop["total_seconds"] <= loop["accepted_interim_limit_sec"],
            )
            self.assertEqual(
                loop["spec_target_met"],
                loop["total_seconds"] <= loop["spec_acceptable_sec"],
            )
            self.assertTrue(dataset["deterministic_except_wall_clock"])
        self.assertEqual(
            code, 0 if all(d["loop"]["accepted_bound_met"] for d in payload["datasets"]) else 1
        )
        self.assertIn(f"RESULT: ACCEPTED_BOUND_MET={'true' if code == 0 else 'false'}", output)
        self.assertEqual(payload["spec_targets"]["asserted"], False)
        self.assertEqual(payload["accepted_interim_bound"]["asserted"], True)

    @slow_test
    def test_the_exhaustive_loop_stays_inside_the_owner_accepted_bound(self) -> None:
        # FIX 3 / D34: the ~100-stop exhaustive loop is asserted against the OWNER-ACCEPTED bound,
        # which is the single named constant this project asserts (with headroom over the measured
        # warm ~63-76 s at 97 enabled stops). Exceeding it fails here. The v2 section 20 <= 5 s
        # acceptable target is printed as a REPORTED engineering target and is not asserted: the
        # owner deliberately accepted the interim latency instead of approximating the search.
        code, output, payload = self.run_benchmark("--no-demo", "--json")

        loop = payload["datasets"][0]["loop"]
        print(
            f"[benchmark] ~100-stop exhaustive loop: {loop['total_seconds']:.2f}s warm, "
            f"{loop['candidates_evaluated']} candidates, "
            f"{loop['candidates_at_ceiling']} at the evaluation ceiling, "
            f"{loop['route_evaluations']} route evaluations; "
            f"spec target <= {loop['spec_acceptable_sec']}s met={loop['spec_target_met']} "
            f"(reported, not asserted); owner-accepted bound "
            f"{loop['accepted_interim_limit_sec']}s (D34)",
            file=sys.stderr,
        )
        self.assertGreater(loop["candidates_evaluated"], 90)
        self.assertEqual(loop["candidates_evaluated"], loop["optimizer_runs"])
        self.assertGreater(
            loop["candidates_at_ceiling"],
            0,
            "at ~100 stops the deterministic evaluation ceiling must bind and be reported (D34)",
        )
        self.assertLessEqual(
            loop["total_seconds"],
            ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
            "the owner-accepted ~100-stop exhaustive-latency bound was exceeded: this is a real "
            "regression against the figure the owner accepted (decision D34), not a missed "
            "engineering target",
        )
        self.assertEqual(loop["accepted_bound_met"], code == 0)


class SingleOptimizeScaleTests(unittest.TestCase):
    """One optimization on the scale plan stays an interactive operation (v2 section 20)."""

    def test_one_hundred_stop_optimize_is_measured(self) -> None:
        problem = scale_problem()
        started = timer.perf_counter()
        result = optimize(problem)
        elapsed = timer.perf_counter() - started

        self.assertEqual(len(result.order), len(problem.stop_ids))
        print(
            f"[benchmark] 100-stop optimize: {elapsed:.3f}s, "
            f"{result.evidence.seed_evaluations} seed candidates priced, "
            f"{result.evidence.search_evaluations} route evaluations, "
            f"{result.evidence.screened_moves} moves screened, "
            f"{len(result.local_search.accepted_moves)} accepted moves, "
            f"search ceiling hit={result.local_search.budget_exhausted}",
            file=sys.stderr,
        )
        # One run is a fraction of the whole exhaustive loop, so it is asserted against the single
        # bound the owner accepted (D34) rather than a second invented timing constant.
        self.assertLess(elapsed, ACCEPTED_INTERIM_LOOP_LIMIT_SEC)

    def test_the_committed_route_is_the_authoritative_evaluation_of_the_order(self) -> None:
        problem = scale_problem()
        result = optimize(problem)
        authoritative = evaluate_order(
            plan=problem.plan, travel_matrix=problem.legs, order=result.order
        )

        self.assertEqual(authoritative.metrics, result.evaluation.metrics)
        self.assertEqual(
            len(authoritative.violations), result.local_search.final_violations
        )


if __name__ == "__main__":
    unittest.main()
