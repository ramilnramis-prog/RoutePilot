"""First-stop candidate evaluation: the Stage 1 acceptance criteria.

A. nearest stop is not always the best first stop
B. farthest stop is not always the best first stop
C. changing departure_time can change the preferred candidate
D. waiting time affects route cost
E. an impossible hard service window is reported explicitly, never hidden by a large score
F. the service_window_end policy is respected
G. all outputs are deterministic

Terminology: what the engine produces is a **recommendation**. It never selects, pins or applies a
first stop - the driver decides (D4/D32). The A/B criteria are therefore properties of the
recommendation.

All travel data here is synthetic and labelled DEMO_SYNTHETIC.
"""

from __future__ import annotations

import unittest
from datetime import time

from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.model.cost_policy import (
    CostComponent,
    demo_provisional_policy,
    empty_cost_policy,
)
from core.model.first_stop import FirstStopState
from core.model.ids import StopId
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.solution import ViolationKind
from core.validation.errors import InvalidCostPolicyError, StopNotGeocodedError
from demo.dataset import HEADLINE_STOP_IDS, build_demo_plan, demo_departure_time_at
from demo.synthetic_matrix import demo_matrix
from tests.support import WAREHOUSE, build_plan, degrees_for, stop, utc

T = 3600
M = 60


def stop_after(seconds: int, stop_id: str, **kwargs):
    """A stop whose synthetic travel time from the warehouse is exactly ``seconds``."""
    return stop(stop_id, WAREHOUSE[0] + degrees_for(seconds), WAREHOUSE[1], **kwargs)


def demo_report(*, hour: int = 4, policy=None, window_end_policy=None):
    plan = build_demo_plan(
        departure_time=demo_departure_time_at(hour),
        cost_policy=policy if policy is not None else demo_provisional_policy(),
        window_end_policy=window_end_policy
        if window_end_policy is not None
        else build_demo_plan().window_end_policy,
    )
    return evaluate_first_stop_candidates(
        plan=plan, travel_matrix=demo_matrix(), policy=policy if policy is not None else demo_provisional_policy()
    )


class AcceptanceCriteriaTests(unittest.TestCase):
    """The demo must prove A-G."""

    def test_a_nearest_is_not_the_best_first_stop(self) -> None:
        report = demo_report(hour=4)
        best = report.recommended()
        nearest = report.nearest()
        assert best is not None and nearest is not None
        self.assertNotEqual(best.stop_id, nearest.stop_id)
        self.assertGreater(best.travel_time, nearest.travel_time)
        # The nearest stop is expensive precisely because of waiting, not because of driving.
        self.assertGreater(nearest.waiting_time, best.waiting_time)
        self.assertGreater(nearest.score, best.score)
        self.assertGreater(report.rank_of(nearest.stop_id) or 0, 1)

    def test_b_farthest_is_not_the_best_first_stop(self) -> None:
        report = demo_report(hour=4)
        best = report.recommended()
        farthest = report.farthest()
        assert best is not None and farthest is not None
        self.assertNotEqual(best.stop_id, farthest.stop_id)
        self.assertGreater(farthest.travel_time, best.travel_time)
        self.assertGreater(farthest.score, best.score)

    def test_c_departure_time_changes_the_preferred_candidate(self) -> None:
        winners = {
            hour: demo_report(hour=hour).recommended_stop_id
            for hour in (4, 5, 6, 7, 8)
        }
        self.assertEqual(len(set(winners.values())), 5, winners)
        self.assertNotEqual(winners[4], winners[7])

        travels = [
            demo_report(hour=hour).recommended().travel_time for hour in (4, 6, 7, 8)  # type: ignore[union-attr]
        ]
        # The later the departure, the closer the best first stop: waiting is what makes far better.
        self.assertEqual(travels, sorted(travels, reverse=True))

    def test_c_the_latest_departure_favours_the_nearest_stop(self) -> None:
        report = demo_report(hour=8)
        best = report.recommended()
        nearest = report.nearest()
        assert best is not None and nearest is not None
        self.assertEqual(best.stop_id, nearest.stop_id)

    def test_d_waiting_time_affects_route_cost(self) -> None:
        # Two stops at the same distance; only the opening time (and therefore waiting) differs.
        policy = demo_provisional_policy()
        plan = build_plan(
            stop_after(45 * M, "early", window=ServiceWindow.fixed(time(8, 0), time(18, 0))),
            stop_after(45 * M, "later", window=ServiceWindow.fixed(time(9, 0), time(19, 0))),
            cost_policy=policy,
        )
        report = evaluate_first_stop_candidates(
            plan=plan, travel_matrix=demo_matrix(), policy=policy
        )
        early = report.find(StopId("early"))
        later = report.find(StopId("later"))
        assert early is not None and later is not None

        self.assertEqual(early.travel_time, later.travel_time)
        self.assertEqual(later.waiting_time - early.waiting_time, T)
        self.assertGreater(later.score, early.score)
        self.assertEqual(
            later.score - early.score,
            2 * (later.waiting_time - early.waiting_time),
            "the score difference must be exactly the waiting difference at the waiting weight",
        )
        self.assertEqual(report.recommended_stop_id, StopId("early"))

    def test_e_impossible_hard_window_is_reported_explicitly(self) -> None:
        report = demo_report(hour=4)
        tight = report.find(HEADLINE_STOP_IDS["tight_window"])
        assert tight is not None
        self.assertFalse(tight.feasible)
        self.assertIsNotNone(tight.violation)
        assert tight.violation is not None
        self.assertIs(tight.violation.kind, ViolationKind.TIME_WINDOW_INFEASIBLE)
        self.assertIn("cannot be served within the permitted window", tight.violation.message)
        # Excluded from the ranking, and never the preferred stop.
        self.assertIsNone(report.rank_of(tight.stop_id))
        self.assertNotEqual(report.recommended_stop_id, tight.stop_id)

    def test_e_a_lowest_scoring_infeasible_stop_still_loses(self) -> None:
        # The cheapest-looking candidate is infeasible (its window closes before the driver can
        # arrive), so it must not win even though its score is the lowest of all candidates.
        policy = demo_provisional_policy()
        plan = build_plan(
            stop_after(
                10 * M,
                "impossible",
                window=ServiceWindow.fixed(time(4, 0), time(4, 5)),
            ),
            stop_after(20 * M, "possible", window=ServiceWindow.fixed(time(8, 0), time(18, 0))),
            cost_policy=policy,
        )
        report = evaluate_first_stop_candidates(
            plan=plan, travel_matrix=demo_matrix(), policy=policy
        )
        impossible = report.find(StopId("impossible"))
        possible = report.find(StopId("possible"))
        assert impossible is not None and possible is not None

        self.assertFalse(impossible.feasible)
        self.assertLess(impossible.score, possible.score)
        self.assertEqual(report.recommended_stop_id, StopId("possible"))
        self.assertEqual(report.ranked_ids(), (StopId("possible"),))

    def test_f_window_end_policy_is_respected(self) -> None:
        finish_policy = demo_report(
            hour=4, window_end_policy=WindowEndPolicy.SERVICE_FINISH_BEFORE_END
        )
        start_policy = demo_report(
            hour=4, window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END
        )
        edge_id = HEADLINE_STOP_IDS["edge_window"]

        in_finish = finish_policy.find(edge_id)
        in_start = start_policy.find(edge_id)
        assert in_finish is not None and in_start is not None

        # Same stop, same window, same arrival: only the meaning of "closes at" differs.
        self.assertEqual(in_finish.travel_time, in_start.travel_time)
        self.assertEqual(in_finish.estimated_arrival, in_start.estimated_arrival)
        self.assertFalse(in_finish.feasible)
        self.assertTrue(in_start.feasible)
        self.assertIs(
            in_finish.window_end_policy, WindowEndPolicy.SERVICE_FINISH_BEFORE_END
        )
        self.assertIs(in_start.window_end_policy, WindowEndPolicy.SERVICE_START_BEFORE_END)
        self.assertGreater(in_finish.lateness, 0)
        self.assertEqual(in_start.lateness, 0)
        self.assertEqual(in_finish.finish_overtime, in_start.finish_overtime)

    def test_g_all_outputs_are_deterministic(self) -> None:
        first = demo_report(hour=4)
        second = demo_report(hour=4)
        self.assertEqual(first.ranked_ids(), second.ranked_ids())
        self.assertEqual(
            [evaluation.score for evaluation in first.ranked],
            [evaluation.score for evaluation in second.ranked],
        )
        self.assertEqual(first.inputs_fingerprint, second.inputs_fingerprint)
        self.assertEqual(
            [evaluation.travel_time for evaluation in first.infeasible],
            [evaluation.travel_time for evaluation in second.infeasible],
        )

    def test_evaluation_only_recommends_it_never_selects(self) -> None:
        # D4/D32/I5: a recommendation is available, and nothing is selected. These are two
        # separate facts about two separate fields.
        report = demo_report(hour=4)
        plan = build_demo_plan()
        self.assertIsNotNone(report.recommended_stop_id)
        self.assertIs(plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertIsNone(plan.first_service_stop.selected_stop_id)
        self.assertFalse(plan.first_service_stop.pinned)


class EvaluationRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = demo_provisional_policy()
        self.plan = build_demo_plan(cost_policy=self.policy)

    def test_infeasible_candidates_are_separated_and_never_ranked(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        self.assertTrue(report.infeasible)
        for evaluation in report.ranked:
            self.assertTrue(evaluation.feasible)
            self.assertIsNone(evaluation.violation)
        for evaluation in report.infeasible:
            self.assertFalse(evaluation.feasible)
            self.assertIsNotNone(evaluation.violation)

    def test_disabled_stops_are_not_candidates(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        disabled = HEADLINE_STOP_IDS["disabled"]
        self.assertIn(disabled, report.disabled_stop_ids)
        self.assertIsNone(report.find(StopId(disabled)))

    def test_score_equals_weighted_breakdown(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        for evaluation in report.ranked[:5]:
            expected = (
                evaluation.component(CostComponent.TRAVEL_TIME) * 1.0
                + evaluation.component(CostComponent.WAITING_TIME) * 2.0
            )
            self.assertAlmostEqual(evaluation.score, expected)

    def test_ranking_is_ordered_by_score(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        scores = [evaluation.score for evaluation in report.ranked]
        self.assertEqual(scores, sorted(scores))

    def test_top_five_is_available_for_the_report(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        top = report.top(5)
        self.assertEqual(len(top), 5)
        self.assertEqual(top[0].stop_id, report.recommended_stop_id)
        for evaluation in top:
            self.assertTrue(evaluation.feasible)

    def test_nearest_and_farthest_helpers_match_travel_times(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        nearest = report.nearest()
        farthest = report.farthest()
        assert nearest is not None and farthest is not None
        self.assertEqual(
            nearest.travel_time, min(e.travel_time for e in report.ranked)
        )
        self.assertEqual(
            farthest.travel_time, max(e.travel_time for e in report.ranked)
        )

    def test_remaining_route_estimate_is_not_implemented_yet(self) -> None:
        # Spec section 8 is explicit that candidate quality includes the route after the
        # candidate; that term arrives with the optimizer, and is not faked here.
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        self.assertTrue(all(e.remaining_route_estimate is None for e in report.ranked))
        declaration = self.policy.declaration(
            CostComponent.FIRST_STOP_REMAINING_ROUTE_WEIGHT
        )
        self.assertEqual(declaration.status.value, "planned")

    def test_a_policy_without_weights_is_refused(self) -> None:
        plan = build_demo_plan(cost_policy=empty_cost_policy())
        with self.assertRaises(InvalidCostPolicyError):
            evaluate_first_stop_candidates(
                plan=plan, travel_matrix=demo_matrix(), policy=empty_cost_policy()
            )

    def test_an_unlocated_stop_blocks_evaluation(self) -> None:
        plan = build_plan(
            stop("located", 55.80, 37.70),
            stop("not-located"),
            cost_policy=self.policy,
        )
        with self.assertRaises(StopNotGeocodedError):
            evaluate_first_stop_candidates(
                plan=plan, travel_matrix=demo_matrix(), policy=self.policy
            )

    def test_fingerprint_reflects_the_evaluated_plan(self) -> None:
        report = evaluate_first_stop_candidates(
            plan=self.plan, travel_matrix=demo_matrix(), policy=self.policy
        )
        self.assertEqual(report.inputs_fingerprint, self.plan.inputs_fingerprint())
        self.assertTrue(report.policy_is_provisional)
        self.assertEqual(report.plan_id, self.plan.id)

    def test_tie_break_is_shortest_first_leg(self) -> None:
        # Two identical stops: same travel, same window, same score. The deterministic tie-break
        # picks the shortest first leg, then the stop id.
        policy = demo_provisional_policy()
        plan = build_plan(
            stop_after(45 * M, "b-second", window=ServiceWindow.fixed(time(8, 0), time(18, 0))),
            stop_after(45 * M, "a-first", window=ServiceWindow.fixed(time(8, 0), time(18, 0))),
            cost_policy=policy,
        )
        report = evaluate_first_stop_candidates(
            plan=plan, travel_matrix=demo_matrix(), policy=policy
        )
        self.assertEqual(report.recommended_stop_id, StopId("a-first"))


if __name__ == "__main__":
    unittest.main()
