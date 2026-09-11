"""Complete-route first-stop evaluation and ranking (Stage 2 U4; v2 sections 12-14, 20, D32).

What is locked in here, beyond the Stage 1 first-leg story:

* **exhaustive candidates** - every enabled stop is evaluated, with no prefilter and no shortlist,
  and the counts prove it (v2 section 20, D34);
* **complete-route ranking** - the deterministic ranking key is the owner's 5-tuple (complete
  elapsed duration, complete travel time, complete waiting time, ``input_position``, ``stop_id``,
  D35) taken from the complete route's own metrics, the FINISH leg included, so a candidate with
  the cheapest first leg does not win by default (v2 sections 12, 15) and no weighted score is
  hidden in the tie-break;
* **complete-route feasibility** - a candidate whose remainder misses a hard window is never
  ranked, and is reported in ``rejected`` with its violating stop ids and reasons (v2 section 14);
* **no_fully_feasible_route** - when nothing is fully feasible there is no recommended id and no
  fabricated winner (v2 section 14, D9);
* **advisory only** - evaluating never mutates the plan, and never implies a selection
  (D4/D32/I5);
* **determinism** - identical inputs give identical outcomes, and the shared leg cache is reused
  across candidates without changing any result (v2 section 20).

Plans here are deliberately small (2-7 stops) so the default suite stays fast; exactly one test
exercises the ~30-stop demo dataset.

All travel data is synthetic (``DEMO_SYNTHETIC``) and is never presented as road routing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from datetime import time
from unittest import mock

from core.engine.first_stop import evaluation as evaluation_module
from core.engine.first_stop.evaluation import (
    evaluate_first_stop_candidates,
    ranking_key,
    score_of,
)
from core.engine.optimizer import route_problem
from core.model.cost_policy import (
    CostComponent,
    demo_provisional_policy,
    empty_cost_policy,
)
from core.model.first_stop import FirstStopState, RecommendationStatus
from core.model.ids import StopId
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.solution import ViolationKind
from core.time import timeline as timeline_engine
from core.time import tz
from core.validation.errors import InvalidCostPolicyError, StopNotGeocodedError
from demo.dataset import HEADLINE_STOP_IDS, build_demo_plan
from demo.synthetic_matrix import demo_matrix
from tests.support import WAREHOUSE, build_plan, degrees_for, stop

T = 3600
M = 60

#: A hard window that closes before a 04:00 departure could ever serve it (D29 hard window).
TIGHT = ServiceWindow.fixed(time(3, 0), time(3, 30))
OPEN = ServiceWindow.unrestricted()


def at(seconds: int, stop_id: str, **kwargs):
    """A stop exactly ``seconds`` of synthetic travel from the warehouse, straight north."""
    return stop(stop_id, WAREHOUSE[0] + degrees_for(seconds), WAREHOUSE[1], **kwargs)


def east(seconds: int, stop_id: str, **kwargs):
    """A stop exactly ``seconds`` of synthetic travel from the warehouse, due east."""
    return stop(stop_id, WAREHOUSE[0], WAREHOUSE[1] + degrees_for(seconds), **kwargs)


def evaluate(plan, *, policy=None, matrix=None):
    # The weighted-policy mechanics are exercised explicitly here; the product default is the
    # elapsed-duration objective (D35), which ``tests.support.build_plan`` applies by default.
    chosen = policy if policy is not None else demo_provisional_policy()
    return evaluate_first_stop_candidates(
        plan=plan, travel_matrix=matrix if matrix is not None else demo_matrix(), policy=chosen
    )


def feasible_plan():
    """All windows unrestricted: every candidate's complete route is feasible."""
    return build_plan(
        at(20 * M, "A", window=OPEN),
        at(40 * M, "B", window=OPEN),
        at(90 * M, "C", window=OPEN),
        at(15 * M, "OFF", window=OPEN, enabled=False),
        cost_policy=demo_provisional_policy(),
    )


def routing_plan():
    """The cheapest first leg loses: ``A`` is nearer, but only ``B`` heads a feasible route.

    START and FINISH both sit at the warehouse; ``A`` is 20 minutes away with hours 05:00-06:00
    and ``B`` is 40 minutes away with hours 06:00-06:15. Serving ``A`` first reaches ``B`` at
    06:20 - past its hard window - so the nearest first stop is rejected by the **complete**
    route, while ``B`` first serves ``A`` at 05:20 and finishes inside both windows (v2 section 14).
    """
    return build_plan(
        at(20 * M, "A", window=ServiceWindow.fixed(time(5, 0), time(6, 0))),
        at(40 * M, "B", window=ServiceWindow.fixed(time(6, 0), time(6, 15))),
        cost_policy=demo_provisional_policy(),
    )


def waiting_plan():
    """A feasible plan whose complete routes differ in waiting, so the objective has both parts.

    ``A`` opens at 04:15 (20 minutes away) and ``B`` at 05:00 (40 minutes away). Serving ``A``
    first means arriving 5 minutes before it opens; serving ``B`` first means waiting at ``B``.
    """
    return build_plan(
        at(20 * M, "A", window=ServiceWindow.fixed(time(4, 15), time(6, 30))),
        at(40 * M, "B", window=ServiceWindow.fixed(time(5, 0), time(6, 30))),
        cost_policy=demo_provisional_policy(),
    )


def nearer_first_stop_loses_plan():
    """Every candidate is feasible, and the **nearest** first leg still loses the ranking.

    ``A`` is 20 minutes from START and ``B`` is 40 minutes, both straight north, while ``D`` sits
    60 minutes due east. Serving ``A`` first looks cheapest on the first leg, but the complete
    route it produces has to make the detour east and then run out to FINISH the long way;
    starting at ``D`` finishes the whole route sooner. Nothing is rejected here, so complete-route
    quality - not feasibility and not the first leg - decides the ranking (v2 sections 12/13).
    """
    return build_plan(
        at(20 * M, "A", window=OPEN),
        at(40 * M, "B", window=OPEN),
        east(1 * T, "D", window=OPEN),
    )


def one_feasible_plan():
    """Exactly one candidate's complete route is feasible.

    ``A`` (20 minutes away) opens at 04:30: served first it is reached in time and ``B`` follows
    at 04:50. Served after ``B`` it is only reached at 05:00, past its window - so ``B`` first is
    rejected by the complete route while its own first leg is perfectly fine (v2 section 14).
    """
    return build_plan(
        at(20 * M, "A", window=ServiceWindow.fixed(time(4, 30), time(5, 0))),
        at(40 * M, "B", window=OPEN),
        cost_policy=demo_provisional_policy(),
    )


def all_infeasible_plan():
    """No candidate can serve its complete route inside the hard windows."""
    return build_plan(
        at(20 * M, "A", window=TIGHT),
        at(40 * M, "B", window=TIGHT),
        at(60 * M, "C", window=TIGHT),
        cost_policy=demo_provisional_policy(),
    )


class ExhaustiveCandidateSetTests(unittest.TestCase):
    """v2 section 20 / D34: every eligible candidate is evaluated, nothing is prefiltered."""

    def test_every_enabled_stop_is_evaluated(self) -> None:
        plan = feasible_plan()
        report = evaluate(plan)

        active = [some_stop.id for some_stop in plan.active_stops()]
        self.assertEqual(report.candidates_evaluated, len(active))
        self.assertEqual(report.optimizer_runs, len(active))
        self.assertEqual(
            sorted(report.ranked_ids() + report.rejected_ids()), sorted(active)
        )
        for stop_id in active:
            self.assertIsNotNone(report.find(stop_id))

    def test_a_single_feasible_candidate_still_evaluates_all_of_them(self) -> None:
        plan = one_feasible_plan()
        report = evaluate(plan)

        self.assertEqual(report.candidates_evaluated, len(plan.active_stops()))
        self.assertEqual(report.optimizer_runs, len(plan.active_stops()))
        self.assertEqual(report.ranked_ids(), (StopId("A"),))
        self.assertEqual(report.rejected_ids(), (StopId("B"),))
        self.assertEqual(report.recommended_stop_id, StopId("A"))

    def test_disabled_stops_are_never_candidates(self) -> None:
        plan = feasible_plan()
        report = evaluate(plan)

        self.assertEqual(report.disabled_stop_ids, (StopId("OFF"),))
        self.assertIsNone(report.find(StopId("OFF")))
        self.assertNotIn(StopId("OFF"), report.ranked_ids())
        self.assertNotIn(StopId("OFF"), report.rejected_ids())


class CompleteRouteRankingTests(unittest.TestCase):
    """v2 sections 12/13: ranking uses complete-route metrics, not the first leg."""

    def test_the_cheapest_first_leg_does_not_automatically_win(self) -> None:
        plan = routing_plan()
        report = evaluate(plan)

        recommended = report.recommended()
        assert recommended is not None
        cheapest = min(
            report.ranked + report.rejected,
            key=lambda candidate: (candidate.travel_time, candidate.stop_id),
        )

        # Both candidates have the same complete route duration, so the objective decides; the
        # rejected candidate is out of the ranking even though its first leg is shorter.
        self.assertEqual(cheapest.stop_id, recommended.stop_id)
        self.assertEqual(report.rejected_ids(), (StopId("B"),))
        self.assertLess(cheapest.travel_time, report.rejected[0].travel_time)
        self.assertFalse(report.rejected[0].feasible)
        self.assertNotIn(report.rejected[0].stop_id, report.ranked_ids())
        self.assertIsNone(report.rank_of(report.rejected[0].stop_id))

    def test_a_nearer_first_stop_can_lose_on_the_complete_route_alone(self) -> None:
        # Every candidate is feasible here, so nothing is hidden behind a rejection: the winner is
        # decided by the complete route, and the cheapest first leg is not enough to win
        # (v2 sections 12/13).
        plan = nearer_first_stop_loses_plan()
        policy = demo_provisional_policy()
        report = evaluate(plan, policy=policy)
        recommended = report.recommended()
        assert recommended is not None

        self.assertEqual(report.rejected_ids(), ())
        alternatives = tuple(
            candidate for candidate in report.ranked if candidate.stop_id != recommended.stop_id
        )
        self.assertTrue(alternatives)
        shorter_first_leg = min(
            alternatives, key=lambda candidate: (candidate.travel_time, candidate.stop_id)
        )
        self.assertLess(shorter_first_leg.travel_time, recommended.travel_time)
        # The cheaper first leg produces the LONGER complete route, so it loses on the complete
        # elapsed duration - the first component of the D35 ranking key.
        self.assertLess(
            recommended.estimated_complete_route_duration,
            shorter_first_leg.estimated_complete_route_duration,
        )
        self.assertEqual(report.rank_of(recommended.stop_id), 1)

    def test_the_ranking_key_is_the_owners_five_tuple_and_carries_no_score(self) -> None:
        # D35: the deterministic ranking key is exactly (complete elapsed duration, complete travel
        # time, complete waiting time, input_position, stop_id) - complete-route metrics, FINISH
        # leg included - and the weighted objective is deliberately not part of it.
        plan = nearer_first_stop_loses_plan()
        report = evaluate(plan, policy=demo_provisional_policy())

        for candidate in report.ranked:
            position = plan.stop_by_id(candidate.stop_id).input_position
            with self.subTest(stop=candidate.stop_id):
                self.assertEqual(
                    ranking_key(candidate, input_position=position),
                    (
                        candidate.estimated_complete_route_duration,
                        candidate.complete_travel_time,
                        candidate.complete_waiting_time,
                        position,
                        candidate.stop_id,
                    ),
                )
        expected = sorted(
            report.ranked,
            key=lambda candidate: ranking_key(
                candidate,
                input_position=plan.stop_by_id(candidate.stop_id).input_position,
            ),
        )
        self.assertEqual(
            [candidate.stop_id for candidate in expected], list(report.ranked_ids())
        )
        # No score can be smuggled in: the key does not accept one.
        with self.assertRaises(TypeError):
            ranking_key(report.ranked[0], score=report.ranked[0].score, input_position=0)  # type: ignore[call-arg]

    def test_candidates_that_tie_exactly_fall_back_to_the_documented_tie_break(self) -> None:
        # Unrestricted windows and two stops: both candidates have the same complete route, so the
        # objective cannot separate them and ``input_position`` decides, then the stop id (D33).
        plan = build_plan(
            at(20 * M, "Zulu", window=OPEN),
            at(40 * M, "Alpha", window=OPEN),
            cost_policy=demo_provisional_policy(),
        )
        report = evaluate(plan)

        self.assertEqual(report.ranked_ids(), (StopId("Zulu"), StopId("Alpha")))
        first, second = report.ranked
        self.assertEqual(first.score, second.score)
        self.assertEqual(
            first.estimated_complete_route_duration, second.estimated_complete_route_duration
        )

    def test_the_score_is_the_policy_over_the_complete_route_breakdown(self) -> None:
        policy = demo_provisional_policy()
        report = evaluate(waiting_plan(), policy=policy)

        self.assertTrue(report.ranked)
        for candidate in report.ranked:
            self.assertEqual(candidate.score, score_of(candidate.metrics, policy))
            self.assertEqual(
                candidate.metrics.travel_sec * 1.0 + candidate.metrics.waiting_sec * 2.0,
                candidate.score,
            )
            # The complete-route breakdown, not the first-leg costs, is what was scored.
            self.assertEqual(candidate.metrics.travel_sec, candidate.complete_travel_time)
            self.assertGreaterEqual(candidate.metrics.travel_sec, candidate.travel_time)
            self.assertGreaterEqual(candidate.metrics.waiting_sec, candidate.waiting_time)
        self.assertTrue(
            any(candidate.metrics.waiting_sec > 0 for candidate in report.ranked),
            "the plan must exercise the waiting component of the objective",
        )

    def test_service_time_is_constant_across_candidates_and_never_scored(self) -> None:
        plan = feasible_plan()
        report = evaluate(plan)

        service_times = {
            candidate.total_service_time for candidate in report.ranked + report.rejected
        }
        self.assertEqual(service_times, {len(plan.active_stops()) * 600})
        for candidate in report.ranked:
            # 600s of service per stop is reported, not part of the scored breakdown.
            self.assertEqual(
                set(dict(candidate.explanation)),
                {
                    CostComponent.TRAVEL_TIME.value,
                    CostComponent.WAITING_TIME.value,
                    CostComponent.DISTANCE.value,
                },
            )

    def test_top_k_returns_the_highest_ranked_candidates_and_keeps_alternatives(self) -> None:
        report = evaluate(feasible_plan())

        self.assertEqual([c.stop_id for c in report.top(2)], list(report.ranked_ids()[:2]))
        self.assertEqual(report.top(0), ())
        self.assertEqual(report.top(99), report.ranked)
        self.assertEqual(report.recommended_stop_id, report.ranked[0].stop_id)
        self.assertGreater(len(report.ranked), 1, "alternatives must stay visible (D32)")
        ranks = [report.rank_of(candidate.stop_id) for candidate in report.ranked]
        self.assertEqual(ranks, list(range(1, len(report.ranked) + 1)))


class CompleteRouteMetricTests(unittest.TestCase):
    """v2 section 12: the reported candidate metrics describe the whole route."""

    def test_every_required_metric_is_reported(self) -> None:
        plan = feasible_plan()
        policy = demo_provisional_policy()
        report = evaluate(plan, policy=policy)
        candidate = report.recommended()
        assert candidate is not None

        # First-leg metrics (v2 section 12) are the shared timeline's own numbers.
        expected = timeline_engine.compute_stop_timeline(
            plan=plan,
            stop=plan.stop_by_id(candidate.stop_id),
            departure_from_previous=plan.departure_time,
            previous_point=plan.departure_point,
            travel_provider=demo_matrix(),
            tzinfo=plan.load_timezone(),
        )
        self.assertEqual(candidate.travel_time, expected.travel_time)
        self.assertEqual(candidate.estimated_arrival, expected.estimated_arrival)
        self.assertEqual(candidate.waiting_time, expected.waiting_time)
        self.assertEqual(candidate.lateness, expected.lateness)
        self.assertEqual(candidate.estimated_service_start, expected.service_start)
        self.assertEqual(
            candidate.estimated_service_start,
            candidate.estimated_arrival + _seconds(candidate.waiting_time),
        )
        # Complete-route metrics, FINISH leg included.
        self.assertGreater(candidate.complete_travel_time, 0)
        self.assertGreaterEqual(candidate.complete_waiting_time, 0)
        self.assertGreater(candidate.total_service_time, 0)
        self.assertGreater(candidate.estimated_complete_route_duration, 0)
        self.assertIsNotNone(candidate.estimated_finish)
        self.assertEqual(candidate.violating_stop_ids, ())
        self.assertIsNotNone(candidate.metrics)
        self.assertEqual(candidate.score, score_of(candidate.metrics, policy))

    def test_complete_route_duration_and_finish_include_the_finish_leg(self) -> None:
        plan = feasible_plan()
        report = evaluate(plan)
        candidate = report.recommended()
        assert candidate is not None

        self.assertEqual(
            candidate.estimated_complete_route_duration,
            candidate.complete_travel_time
            + candidate.complete_waiting_time
            + candidate.total_service_time,
        )
        self.assertEqual(
            candidate.estimated_finish,
            plan.departure_time + _seconds(candidate.estimated_complete_route_duration),
        )
        # The metrics come from the authoritative complete-route evaluation of the same order.
        evaluation = timeline_engine_result(plan, candidate.stop_id)
        self.assertEqual(candidate.complete_travel_time, evaluation.metrics.travel_sec)
        self.assertEqual(candidate.estimated_finish, evaluation.metrics.finish_arrival)
        self.assertGreater(evaluation.metrics.travel_sec, candidate.travel_time)

    def test_first_leg_metrics_match_the_shared_timeline_arithmetic(self) -> None:
        plan = feasible_plan()
        policy = demo_provisional_policy()
        report = evaluate(plan, policy=policy)
        candidate = report.find(StopId("B"))
        assert candidate is not None

        expected = timeline_engine.compute_stop_timeline(
            plan=plan,
            stop=plan.stop_by_id(StopId("B")),
            departure_from_previous=plan.departure_time,
            previous_point=plan.departure_point,
            travel_provider=demo_matrix(),
            tzinfo=plan.load_timezone(),
        )
        self.assertEqual(candidate.travel_time, expected.travel_time)
        self.assertEqual(candidate.estimated_arrival, expected.estimated_arrival)
        self.assertEqual(candidate.waiting_time, expected.waiting_time)
        self.assertEqual(candidate.service_window_start, expected.service_window_start)
        self.assertEqual(candidate.estimated_service_start, expected.service_start)

    def test_a_first_stop_inside_its_own_window_can_still_be_rejected(self) -> None:
        # v2 section 14: the first stop may be perfectly servable on its own while the complete
        # route misses a LATER stop's hard window, and that candidate must not look feasible.
        report = evaluate(routing_plan())

        rejected = report.find(StopId("B"))
        assert rejected is not None
        self.assertEqual(rejected.lateness, 0)  # B itself is served inside its own window
        self.assertFalse(rejected.feasible)
        self.assertEqual(rejected.violating_stop_ids, (StopId("A"),))
        self.assertGreater(rejected.max_lateness, 0)
        self.assertNotIn(rejected.stop_id, report.ranked_ids())
        self.assertIsNone(report.rank_of(rejected.stop_id))


class InfeasibleCandidateTests(unittest.TestCase):
    """v2 section 14 / D9: infeasibility is explicit, reported and never ranked."""

    def test_complete_route_infeasible_candidates_are_rejected_with_their_violations(self) -> None:
        plan = all_infeasible_plan()
        report = evaluate(plan)

        self.assertEqual(report.ranked, ())
        self.assertEqual(len(report.rejected), len(plan.active_stops()))
        for candidate in report.rejected:
            self.assertFalse(candidate.feasible)
            self.assertTrue(candidate.violating_stop_ids)
            reasons = report.reasons_for(candidate.stop_id)
            self.assertTrue(reasons)
            # The reasons are this candidate's own, linked by the explicit candidate field. Here
            # the candidate's own first stop happens to be a violating stop too, so an assertion
            # that merely finds the candidate id inside the prose proves nothing; the link is a
            # field, and the reason names the VIOLATING stop (which may be a later stop).
            self.assertEqual(
                {diagnostic.candidate_stop_id for diagnostic in reasons},
                {candidate.stop_id},
            )
            self.assertEqual(
                tuple(sorted(diagnostic.stop_id for diagnostic in reasons)),
                tuple(sorted(candidate.violating_stop_ids)),
            )
            for diagnostic in reasons:
                self.assertIs(diagnostic.violation_kind, ViolationKind.TIME_WINDOW_INFEASIBLE)
                self.assertEqual(diagnostic.code, ViolationKind.TIME_WINDOW_INFEASIBLE.value)
                self.assertTrue(diagnostic.message)
                self.assertIn(str(diagnostic.stop_id), diagnostic.reason)
        # Every diagnostic belongs to exactly one candidate: the per-candidate lookup partitions
        # the report's diagnostics, so none is dropped and none is handed to two candidates.
        self.assertEqual(
            len(report.diagnostics),
            sum(len(report.reasons_for(candidate.stop_id)) for candidate in report.rejected),
        )

    def test_a_rejected_candidates_reasons_answer_for_its_own_id_not_the_violating_stop(
        self,
    ) -> None:
        # v2 section 14 / U4 contract item 4. In this plan ``B`` heads an infeasible complete route
        # because the LATER stop ``A`` misses its hard window, while ``B``'s own first stop is
        # served inside its own window. A caller asking ``B`` for ITS OWN reasons by ``B``'s id
        # must get them, and the answer must not come from the violating stop's id: ``A`` is a
        # perfectly feasible candidate with nothing recorded against it.
        report = evaluate(routing_plan())
        rejected = report.find(StopId("B"))
        assert rejected is not None
        self.assertEqual(rejected.lateness, 0)  # B's own first stop is servable
        self.assertEqual(rejected.violating_stop_ids, (StopId("A"),))
        self.assertNotEqual(rejected.stop_id, rejected.violating_stop_ids[0])

        reasons = report.reasons_for(StopId("B"))
        self.assertTrue(reasons, "a rejected candidate must answer for its own id")
        # The reasons list the violating stop ids of this candidate...
        self.assertEqual(
            tuple(sorted(diagnostic.stop_id for diagnostic in reasons)),
            tuple(sorted(rejected.violating_stop_ids)),
        )
        # ...and every one of them is linked to this candidate as data.
        for diagnostic in reasons:
            self.assertEqual(diagnostic.candidate_stop_id, StopId("B"))
            self.assertNotEqual(diagnostic.stop_id, diagnostic.candidate_stop_id)
            self.assertIn(str(diagnostic.stop_id), diagnostic.reason)

        # The violating stop's id is a different key, and that is what the earlier defect confused.
        self.assertEqual(report.reasons_for(StopId("A")), ())
        feasible = report.find(StopId("A"))
        assert feasible is not None
        self.assertTrue(feasible.feasible)
        self.assertIn(StopId("A"), report.ranked_ids())

    def test_all_infeasible_yields_no_fully_feasible_route_and_no_winner(self) -> None:
        report = evaluate(all_infeasible_plan())

        self.assertIs(report.status, RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE)
        self.assertEqual(report.status_name, "no_fully_feasible_route")
        self.assertIsNone(report.recommended())
        self.assertIsNone(report.recommended_stop_id)
        self.assertEqual(report.top(5), ())
        self.assertTrue(report.diagnostics)
        self.assertIn("no_fully_feasible_route", report.describe_status())

    def test_the_rejected_candidates_report_their_violating_stops(self) -> None:
        report = evaluate(routing_plan())

        self.assertEqual(report.ranked_ids(), (StopId("A"),))
        self.assertEqual(report.rejected_ids(), (StopId("B"),))
        for candidate in report.rejected:
            self.assertTrue(candidate.violating_stop_ids)
            self.assertEqual(candidate.violating_stop_ids, (StopId("A"),))
            self.assertGreater(candidate.max_lateness, 0)
        for candidate in report.ranked:
            self.assertEqual(candidate.violating_stop_ids, ())
            self.assertEqual(candidate.max_lateness, 0)

    def test_both_report_states_map_onto_the_domain_recommendation(self) -> None:
        feasible = evaluate(routing_plan()).to_recommendation()
        self.assertIs(feasible.status, RecommendationStatus.RECOMMENDED)
        self.assertEqual(feasible.recommended_stop_id, StopId("A"))

        none = evaluate(all_infeasible_plan()).to_recommendation()
        self.assertIs(none.status, RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE)
        self.assertIsNone(none.recommended_stop_id)
        self.assertEqual(none.ranked, ())
        self.assertTrue(none.diagnostics)


class AdvisoryOnlyTests(unittest.TestCase):
    """D4/D32/I5: a recommendation never implies a selection and never mutates the plan."""

    def test_evaluation_leaves_the_plan_awaiting_a_driver_choice(self) -> None:
        plan = feasible_plan()
        before = plan
        report = evaluate(plan)

        self.assertIsNotNone(report.recommended_stop_id)
        self.assertIs(plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertIsNone(plan.first_service_stop.selected_stop_id)
        self.assertIsNone(plan.first_service_stop.selection_source)
        self.assertFalse(plan.first_service_stop.pinned)
        self.assertEqual(plan, before)

    def test_the_domain_recommendation_carries_no_selection(self) -> None:
        plan = feasible_plan()
        recommendation = evaluate(plan).to_recommendation()

        self.assertTrue(recommendation.is_available)
        self.assertEqual(plan.first_service_stop.selected_stop_id, None)
        self.assertIs(plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertEqual(recommendation.resolved_at, plan.departure_time)
        self.assertEqual(recommendation.inputs_fingerprint, plan.inputs_fingerprint())


class CachingAndDeterminismTests(unittest.TestCase):
    """v2 section 20: the shared cache is real, and it changes no result."""

    def test_the_shared_leg_cache_is_reused_across_candidates(self) -> None:
        report = evaluate(feasible_plan())

        self.assertGreater(report.cache_stats.hits, 0)
        self.assertGreater(report.cache_stats.misses, 0)
        self.assertGreater(report.cache_stats.entries, 0)
        self.assertGreater(report.cache_stats.lookups, report.cache_stats.entries)

    def test_a_second_run_with_the_cache_warm_changes_no_result(self) -> None:
        plan = feasible_plan()
        policy = demo_provisional_policy()
        cached = evaluate(plan, policy=policy)
        uncached = evaluate_first_stop_candidates(
            plan=plan,
            travel_matrix=demo_matrix(),
            policy=policy,
        )

        self.assertEqual(cached.ranked_ids(), uncached.ranked_ids())
        self.assertEqual(cached.rejected_ids(), uncached.rejected_ids())
        self.assertEqual(
            [candidate.metrics for candidate in cached.ranked],
            [candidate.metrics for candidate in uncached.ranked],
        )
        self.assertEqual(
            [candidate.score for candidate in cached.ranked],
            [candidate.score for candidate in uncached.ranked],
        )

    def test_a_memoized_leg_carries_the_unmemoized_matrixs_answer(self) -> None:
        # The cache is transparent: every leg the memoized evaluation priced is re-priced through
        # the raw matrix on the committed order, and the authoritative evaluation of that order
        # must agree. A cache that changed a leg would show up here (v2 section 20).
        from core.engine.optimizer import evaluate_order

        plan = feasible_plan()
        report = evaluate(plan)

        for candidate in report.ranked:
            fresh = timeline_engine_result(plan, candidate.stop_id)
            self.assertEqual(fresh.order[0], candidate.stop_id)
            direct = evaluate_order(plan=plan, travel_matrix=demo_matrix(), order=fresh.order)
            self.assertEqual(direct.metrics, fresh.metrics)
            self.assertEqual(direct.timelines, fresh.timelines)
            self.assertEqual(direct.violations, fresh.violations)
            self.assertEqual(candidate.metrics.travel_sec, direct.metrics.travel_sec)
            self.assertEqual(candidate.metrics.distance_m, direct.metrics.distance_m)
            self.assertEqual(candidate.estimated_finish, direct.metrics.finish_arrival)

    def test_repeated_runs_are_identical(self) -> None:
        plan = feasible_plan()
        policy = demo_provisional_policy()
        first = evaluate(plan, policy=policy)
        second = evaluate(plan, policy=policy)

        self.assertEqual(first.ranked, second.ranked)
        self.assertEqual(first.rejected, second.rejected)
        self.assertEqual(first.diagnostics, second.diagnostics)
        self.assertEqual(first.cache_stats, second.cache_stats)
        self.assertEqual(first.inputs_fingerprint, second.inputs_fingerprint)

    def test_ranking_is_independent_of_pythonhashseed(self) -> None:
        # Dict/set iteration order is hash-seed dependent in principle; the whole evaluation -
        # ranking, rejection set, diagnostics, metrics and cache statistics - must not be.
        outputs = {
            seed: _evaluation_under_hash_seed(seed) for seed in ("0", "1", "12345")
        }
        self.assertEqual(len(set(outputs.values())), 1, outputs)
        for seed, output in outputs.items():
            with self.subTest(seed=seed):
                self.assertIn("recommended", output)
                self.assertIn("ranked D:", output)
                self.assertIn("CacheStats(hits=161", output)

    def test_fingerprint_reflects_the_evaluated_plan(self) -> None:
        plan = feasible_plan()
        report = evaluate(plan)

        self.assertEqual(report.inputs_fingerprint, plan.inputs_fingerprint())
        self.assertEqual(report.plan_id, plan.id)
        self.assertTrue(report.policy_is_provisional)
        self.assertEqual(report.policy_name, "demo_provisional_v1")
        self.assertIs(report.window_end_policy, WindowEndPolicy.SERVICE_FINISH_BEFORE_END)


class PlanStateTests(unittest.TestCase):
    """Plan states that carry nothing to recommend (D9)."""

    def test_a_plan_without_stops_reports_empty_plan(self) -> None:
        report = evaluate(build_plan(cost_policy=demo_provisional_policy()))

        self.assertIs(report.status, RecommendationStatus.EMPTY_PLAN)
        self.assertEqual(report.candidates_evaluated, 0)
        self.assertEqual(report.optimizer_runs, 0)
        self.assertEqual(report.ranked, ())
        self.assertEqual(report.rejected, ())

    def test_a_plan_with_only_disabled_stops_reports_no_active_stops(self) -> None:
        plan = build_plan(
            at(20 * M, "OFF", window=OPEN, enabled=False),
            cost_policy=demo_provisional_policy(),
        )
        report = evaluate(plan)

        self.assertIs(report.status, RecommendationStatus.NO_ACTIVE_STOPS)
        self.assertEqual(report.disabled_stop_ids, (StopId("OFF"),))
        self.assertEqual(report.candidates_evaluated, 0)


class PolicyAndErrorTests(unittest.TestCase):
    def test_a_policy_without_weights_is_refused(self) -> None:
        plan = build_plan(
            at(20 * M, "A", window=OPEN), cost_policy=empty_cost_policy()
        )
        with self.assertRaises(InvalidCostPolicyError):
            evaluate_first_stop_candidates(
                plan=plan, travel_matrix=demo_matrix(), policy=empty_cost_policy()
            )

    def test_an_unlocated_stop_blocks_evaluation(self) -> None:
        plan = build_plan(
            at(20 * M, "A", window=OPEN),
            stop("not-located"),
            cost_policy=demo_provisional_policy(),
        )
        with self.assertRaises(StopNotGeocodedError):
            evaluate(plan)


class DemoScaleTests(unittest.TestCase):
    """One demo-scale run: the engine's answer on the ~30-stop scenario (v2 section 33).

    The demo fixture is calibrated (Stage 2 unit U5) so that at least one fully feasible complete
    route exists and a real ranking is produced. This test pins the engine-level answer: exhaustive,
    honest, and a recommendation rather than a selection.
    """

    def test_the_demo_plan_is_evaluated_exhaustively_and_honestly(self) -> None:
        policy = demo_provisional_policy()
        plan = build_demo_plan(cost_policy=policy)
        report = evaluate(plan, policy=policy)

        self.assertEqual(report.candidates_evaluated, len(plan.active_stops()))
        self.assertEqual(report.optimizer_runs, len(plan.active_stops()))
        self.assertIn(HEADLINE_STOP_IDS["disabled"], report.disabled_stop_ids)
        self.assertGreater(report.cache_stats.hits, 0)
        # A ranked top-K exists, every ranked candidate is completely feasible, and the engine
        # never applies the recommendation: the driver decides (D4/D32).
        self.assertIs(report.status, RecommendationStatus.RECOMMENDED)
        self.assertGreaterEqual(len(report.ranked), 5)
        self.assertIsNotNone(report.recommended_stop_id)
        self.assertTrue(all(candidate.feasible for candidate in report.ranked))
        self.assertTrue(all(not candidate.violating_stop_ids for candidate in report.ranked))
        self.assertEqual(
            len(report.ranked) + len(report.rejected), len(plan.active_stops())
        )
        self.assertIsNone(plan.first_service_stop.selected_stop_id)
        self.assertIs(plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)


class MeasuredTimezoneResolutionTests(unittest.TestCase):
    """The measured numbers in ``evaluate_first_stop_candidates``'s docstring are re-counted (U5).

    The docstring states a measurement of the shipped fixture, so a fixture change must fail here
    instead of leaving a stale claim in the source (the U5 review found exactly that: 30/3462/270
    still written while the shipped fixture had 31 candidates and 3681 calls). The count is taken
    around a real evaluation and compared with the docstring text, so the two cannot disagree.

    ``resolve_local_datetime`` is counted through :mod:`core.time.tz` - the same module attribute
    ``_build_window_table``, ``_resolve_local_cached`` and ``resolve_service_window`` call - and the
    process-wide memo is cleared first, so the count is deterministic rather than dependent on which
    test ran before. Clearing a memo of pure conversions changes no answer.
    """

    def setUp(self) -> None:
        self.plan = build_demo_plan()

    def test_the_measured_timezone_resolution_count_matches_the_docstring(self) -> None:
        day_boundaries = 0
        memo_resolutions = 0
        route_resolutions = 0
        service_windows = 0
        resolving_service_windows = 0
        resolutions = 0
        real_resolve_local_datetime = tz.resolve_local_datetime
        real_resolve_service_window = tz.resolve_service_window
        real_cached = route_problem._resolve_local_cached
        docstring = evaluation_module.evaluate_first_stop_candidates.__doc__
        assert docstring is not None
        # Line wrapping must not decide this: compare against the unwrapped text.
        unwrapped_docstring = " ".join(docstring.split())
        # The document contract: the counts printed in the docstring.
        self.assertIn("31 candidates, 31 optimizer runs", unwrapped_docstring)
        self.assertIn("31 candidate problems x 9 prepared dates", unwrapped_docstring)

        def counting_resolution(*args, **kwargs):
            nonlocal resolutions, day_boundaries, memo_resolutions, route_resolutions
            resolutions += 1
            caller = sys._getframe(1).f_code.co_name
            if caller == "_build_window_table":
                day_boundaries += 1
            elif caller == "_resolve_local_cached":
                memo_resolutions += 1
            else:
                route_resolutions += 1
            return real_resolve_local_datetime(*args, **kwargs)

        def counting_service_window(*args, **kwargs):
            nonlocal service_windows, resolving_service_windows
            service_windows += 1
            resolved = real_resolve_service_window(*args, **kwargs)
            if resolved is not None:
                resolving_service_windows += 1
            return resolved

        real_cached.cache_clear()
        before = real_cached.cache_info()
        with (
            mock.patch.object(tz, "resolve_local_datetime", counting_resolution),
            mock.patch.object(tz, "resolve_service_window", counting_service_window),
        ):
            report = evaluate_first_stop_candidates(plan=self.plan, travel_matrix=demo_matrix())
        after = real_cached.cache_info()
        memo_hits = after.hits - before.hits
        memo_misses = after.misses - before.misses

        self.assertEqual(report.candidates_evaluated, len(self.plan.active_stops()))
        self.assertEqual(report.candidates_evaluated, 31)
        self.assertEqual(len(report.ranked), 26)
        self.assertEqual(len(report.ranked) + len(report.rejected), 31)
        # 31 candidate problems x 9 prepared dates: the local-midnight boundary is not memoized.
        self.assertEqual(day_boundaries, report.candidates_evaluated * 9)
        self.assertEqual(day_boundaries, 279)
        self.assertEqual(memo_misses, 54)
        self.assertEqual(memo_resolutions, memo_misses)
        self.assertEqual(memo_hits, 2457)
        self.assertEqual(resolutions, 3681)
        self.assertEqual(route_resolutions, 3348)
        self.assertEqual(day_boundaries + memo_resolutions + route_resolutions, resolutions)
        # The route pass calls ``resolve_service_window`` 1922 times; only the 1674 fixed windows
        # resolve, and each resolves exactly two wall-clock times (open and close).
        self.assertEqual(service_windows, 1922)
        self.assertEqual(resolving_service_windows, 1674)
        self.assertEqual(resolving_service_windows * 2, route_resolutions)
        for reported in (
            "**3681**",
            "279 local-midnight day",
            "54 distinct fixed-window",
            "and 3348 inside the authoritative per-candidate route evaluation",
            "the 1674 ``core.time.tz.resolve_service_window`` calls",
            "makes 1922 such calls in total",
        ):
            with self.subTest(reported=reported):
                self.assertIn(reported, unwrapped_docstring)
        self.assertIn("2457 lookups on that path", unwrapped_docstring)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _seconds(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def timeline_engine_result(plan, stop_id):
    """The authoritative complete-route evaluation of one candidate, recomputed independently."""
    from core.engine.optimizer import build_problem, optimize

    problem = build_problem(
        plan=plan, travel_matrix=demo_matrix(), first_stop_id=stop_id
    )
    return optimize(problem).evaluation


def _evaluation_under_hash_seed(seed: str) -> str:
    """The whole evaluation, computed in a fresh interpreter with ``PYTHONHASHSEED=seed``.

    One line per candidate and one per rejection reason, so the comparison covers the ranking,
    the rejected set, the diagnostics, every v2 section 12 metric and the cache statistics.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    script = (
        "import sys; sys.path.insert(0, r'%s');"
        "from tests.engine.test_first_stop_evaluation import nearer_first_stop_loses_plan, evaluate;"
        "report = evaluate(nearer_first_stop_loses_plan());"
        "print(report.status_name, report.cache_stats);\n"
        "print('ranked', ','.join(\n"
        "    f'{c.stop_id}:{c.score}:{c.estimated_complete_route_duration}:{c.travel_time}:'\n"
        "    f'{c.complete_travel_time}:{c.complete_waiting_time}:{c.total_service_time}:'\n"
        "    f'{c.violating_stop_ids}:{c.estimated_finish.isoformat()}'\n"
        "    for c in report.ranked));\n"
        "print('rejected', ','.join(c.stop_id for c in report.rejected));\n"
        "print('diagnostics', ','.join(\n"
        "    f'{d.candidate_stop_id}:{d.stop_id}:' f'{d.code}:{d.reason}'\n"
        "    for d in report.diagnostics))"
        % repo_root
    )
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = seed
    if not environment.get("PYTHONTZPATH"):
        # The offline development fallback the suite itself activates (tests/__init__.py), passed
        # down explicitly so the child interpreter resolves the plan's IANA zone the same way.
        from tests import TZDATA_FALLBACK_PATH

        if TZDATA_FALLBACK_PATH is not None:
            environment["PYTHONTZPATH"] = str(TZDATA_FALLBACK_PATH)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=environment,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"hash-seed run {seed} failed: {completed.returncode}\n{completed.stderr}"
        )
    return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
