"""The complete-route demo report and what v2 section 33 requires the demo to prove.

These tests pin the numeric story the demo tells, so a change in behaviour cannot quietly change
the narrative the report shows - and they pin the four claims of v2 section 33:

* the nearest candidate is **not** the recommendation;
* the farthest candidate is **not** the recommendation;
* changing the departure time **can** change the recommendation;
* **complete-route** quality decides, not the first leg alone.

They also pin the two claims the U5 review found unpinned and therefore false-or-fragile: the
printed "least complete driving" comparison is derived from the ranking it claims to be about, and
the rejected-candidate diagnostics the report prints (v2 section 14, D9) come from the real fixture.

The exhaustive demo-scale evaluation costs several seconds, so it is evaluated through the report's
memoized helpers: every test in this module reads the same evaluation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import time as timer
import unittest
from datetime import time

from core.engine.first_stop.evaluation import FirstStopEvaluationReport, score_of
from core.model.first_stop import FirstStopState, RecommendationStatus
from core.model.service_window import ServiceWindow
from demo.dataset import HEADLINE_STOP_IDS, build_demo_plan
from demo.report import (
    BENCHMARK_COMMAND,
    DEMO_EVALUATION_RUNTIME_BUDGET_SEC,
    DEPARTURE_SWEEP_HOURS,
    RECORDED_BENCHMARK,
    WEIGHT_SENSITIVITY_RATIOS,
    build_report,
    complete_travel_rank,
    demo_evaluation,
    departure_sweep,
    fewest_driving_ids,
    format_duration,
    main,
    objective_winner_sentence,
    recommendation_preview,
    rejected_candidate_lines,
    weight_sensitivity,
)
from demo.synthetic_matrix import demo_matrix

T = 3600
M = 60

#: The recommended first stop of the demo plan at 04:00 under the shipped provisional policy.
RECOMMENDED = "S25-ON-OPENING"


def _demo_report() -> FirstStopEvaluationReport:
    return demo_evaluation(build_demo_plan())


class FormattingTests(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(format_duration(0), "0m")
        self.assertEqual(format_duration(45 * M), "45m")
        self.assertEqual(format_duration(3 * T + 55 * M), "3h55m")
        self.assertEqual(format_duration(4 * T), "4h00m")


class DemoScenarioTests(unittest.TestCase):
    """The engine's answer on the demo plan: a real, ranked recommendation (v2 sections 12-14)."""

    def setUp(self) -> None:
        self.plan = build_demo_plan()
        self.report = _demo_report()

    def test_the_demo_plan_yields_a_fully_feasible_recommendation(self) -> None:
        # The confirmed problem this unit fixes: the fixture must not answer
        # no_fully_feasible_route any more.
        self.assertIs(self.report.status, RecommendationStatus.RECOMMENDED)
        self.assertIsNotNone(self.report.recommended_stop_id)
        self.assertEqual(self.report.recommended_stop_id, RECOMMENDED)
        self.assertEqual(
            len(self.report.ranked) + len(self.report.rejected),
            len(self.plan.active_stops()),
        )

    def test_the_ranking_is_exhaustive_over_every_enabled_stop(self) -> None:
        self.assertEqual(self.report.candidates_evaluated, len(self.plan.active_stops()))
        self.assertEqual(self.report.optimizer_runs, len(self.plan.active_stops()))
        self.assertNotIn(
            HEADLINE_STOP_IDS["disabled"],
            [candidate.stop_id for candidate in self.report.ranked],
        )
        self.assertNotIn(
            HEADLINE_STOP_IDS["disabled"],
            [candidate.stop_id for candidate in self.report.rejected],
        )

    def test_every_ranked_candidate_has_a_fully_feasible_complete_route(self) -> None:
        self.assertGreaterEqual(len(self.report.ranked), 5)
        for candidate in self.report.ranked:
            with self.subTest(stop=candidate.stop_id):
                self.assertTrue(candidate.feasible)
                self.assertEqual(candidate.violating_stop_ids, ())
                self.assertIsNotNone(candidate.estimated_finish)

    def test_some_candidates_are_rejected_with_their_violating_stops(self) -> None:
        # v2 section 14 / D9: the fixture must exercise the rejection path, and a rejected
        # candidate is never ranked or labelled a valid route.
        self.assertGreaterEqual(len(self.report.rejected), 1)
        self.assertGreaterEqual(len(self.report.ranked), 5)
        for candidate in self.report.rejected:
            with self.subTest(stop=candidate.stop_id):
                self.assertFalse(candidate.feasible)
                self.assertTrue(candidate.violating_stop_ids)
                self.assertEqual(candidate.violating_stop_ids, ("S32-EARLY-CLOSE",))
                self.assertIsNone(self.report.rank_of(candidate.stop_id))
                self.assertTrue(self.report.reasons_for(candidate.stop_id))

    def test_the_ranking_is_ordered_by_the_complete_route_objective(self) -> None:
        scores = [candidate.score for candidate in self.report.ranked]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(self.report.recommended_stop_id, self.report.ranked[0].stop_id)

    def test_complete_duration_includes_travel_waiting_and_service(self) -> None:
        for candidate in list(self.report.ranked) + list(self.report.rejected):
            with self.subTest(stop=candidate.stop_id):
                self.assertEqual(
                    candidate.estimated_complete_route_duration,
                    candidate.complete_travel_time
                    + candidate.complete_waiting_time
                    + candidate.total_service_time,
                )

    def test_total_service_time_is_identical_for_every_candidate(self) -> None:
        service_times = {
            candidate.total_service_time
            for candidate in list(self.report.ranked) + list(self.report.rejected)
        }
        self.assertEqual(len(service_times), 1)

    def test_the_recommendation_is_not_applied(self) -> None:
        # D4/D32: a recommendation is not a selection.
        self.assertIs(self.plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        self.assertIsNone(self.plan.first_service_stop.selected_stop_id)


class CompleteDrivingComparisonTests(unittest.TestCase):
    """The printed "least complete driving" comparison is computed, not remembered.

    U5 review: the report used to claim the nearest candidate's complete route "drives the least"
    while two ranked candidates drove less. These tests derive the same comparison from the ranking
    the sentence is about, so the claim cannot go stale again.
    """

    def setUp(self) -> None:
        self.report = _demo_report()

    def test_the_nearest_candidate_really_has_the_least_complete_driving(self) -> None:
        driving = complete_travel_rank(self.report, HEADLINE_STOP_IDS["nearest"])
        assert driving is not None
        nearest = self.report.find(HEADLINE_STOP_IDS["nearest"])
        assert nearest is not None
        self.assertEqual(driving.rank, 1)
        self.assertEqual(driving.of, len(self.report.ranked))
        self.assertEqual(driving.minimum, nearest.complete_travel_time)
        self.assertEqual(
            set(fewest_driving_ids(self.report)),
            {HEADLINE_STOP_IDS["nearest"], HEADLINE_STOP_IDS["near_second"]},
        )
        self.assertEqual(
            driving.tied_ids,
            (HEADLINE_STOP_IDS["near_second"], HEADLINE_STOP_IDS["nearest"]),
        )

    def test_the_comparison_is_derived_from_the_ranking_and_catches_less_driving(self) -> None:
        # The same helper reports a higher rank when a candidate drives more, and the extreme
        # reported is the real minimum of the compared set - which is what the sentence claims.
        reported_minimum = min(
            candidate.complete_travel_time for candidate in self.report.ranked
        )
        reported_maximum = max(
            candidate.complete_travel_time for candidate in self.report.ranked
        )
        for candidate in self.report.ranked:
            with self.subTest(stop=candidate.stop_id):
                driving = complete_travel_rank(self.report, candidate.stop_id)
                assert driving is not None
                self.assertEqual(driving.minimum, reported_minimum)
                self.assertEqual(driving.maximum, reported_maximum)
                self.assertEqual(
                    driving.rank,
                    sum(
                        other.complete_travel_time < candidate.complete_travel_time
                        for other in self.report.ranked
                    )
                    + 1,
                )

    def test_the_nearest_really_drives_least_and_still_ranks_last(self) -> None:
        nearest = self.report.find(HEADLINE_STOP_IDS["nearest"])
        assert nearest is not None
        self.assertLessEqual(
            nearest.complete_travel_time,
            min(candidate.complete_travel_time for candidate in self.report.ranked),
        )
        self.assertEqual(self.report.rank_of(nearest.stop_id), len(self.report.ranked))

    def test_the_report_prints_the_computed_comparison_and_not_a_stale_superlative(self) -> None:
        text = build_report()
        nearest = self.report.find(HEADLINE_STOP_IDS["nearest"])
        assert nearest is not None
        self.assertIn(
            f"the 1st-least complete driving of the {len(self.report.ranked)} ranked candidates "
            f"(least {format_duration(nearest.complete_travel_time)}, "
            f"most {format_duration(max(c.complete_travel_time for c in self.report.ranked))})",
            text,
        )

    def test_a_rejected_role_has_no_composite_driving_rank(self) -> None:
        # The farthest candidate is rejected, so the report must not claim a driving rank for it.
        self.assertIsNone(self.report.rank_of(HEADLINE_STOP_IDS["farthest"]))
        text = build_report()
        self.assertIn("why the farthest loses", text)
        self.assertIn("it is REJECTED, never ranked", text)
        self.assertIn("farthest REJECTED of", text)


class DemoProvesSection33Tests(unittest.TestCase):
    """The four claims v2 section 33 requires the demo to prove."""

    def setUp(self) -> None:
        self.plan = build_demo_plan()
        self.report = _demo_report()
        self.nearest = self.report.find(HEADLINE_STOP_IDS["nearest"])
        self.farthest = self.report.find(HEADLINE_STOP_IDS["farthest"])
        self.recommended = self.report.recommended()
        assert self.nearest is not None and self.farthest is not None
        assert self.recommended is not None

    def test_nearest_is_not_the_recommendation(self) -> None:
        self.assertNotEqual(self.nearest.stop_id, self.report.recommended_stop_id)
        self.assertGreaterEqual(self.report.rank_of(self.nearest.stop_id), len(self.report.ranked) // 2)
        # ... and it is a feasible candidate, not a rejected one: complete-route quality, not
        # feasibility, is what pushes it down.
        self.assertTrue(self.nearest.feasible)

    def test_farthest_is_not_the_recommendation(self) -> None:
        self.assertNotEqual(self.farthest.stop_id, self.report.recommended_stop_id)
        # In this calibration the farthest candidate is rejected outright - even stronger than
        # merely ranking below the recommendation - and it is never ranked.
        self.assertIsNone(self.report.rank_of(self.farthest.stop_id))
        self.assertFalse(self.farthest.feasible)
        self.assertIn(self.farthest.stop_id, self.report.rejected_ids())

    def test_complete_route_quality_decides_and_not_the_first_leg(self) -> None:
        # The recommendation DRIVES MORE than the nearest candidate and still wins, because the
        # complete route's waiting is what separates them: the first leg alone would pick the
        # nearest.
        self.assertGreater(
            self.recommended.complete_travel_time, self.nearest.complete_travel_time
        )
        self.assertLess(
            self.recommended.complete_waiting_time, self.nearest.complete_waiting_time
        )
        self.assertLess(self.recommended.score, self.nearest.score)
        self.assertLess(
            self.recommended.estimated_complete_route_duration,
            self.nearest.estimated_complete_route_duration,
        )
        # The nearest is the closest candidate by first-leg travel AND its complete route is the
        # cheapest to drive, so "nearest" really means what it says: it loses on the complete
        # route, not on the first leg.
        first_legs = {candidate.stop_id: candidate.travel_time for candidate in self.report.ranked}
        self.assertEqual(self.nearest.travel_time, min(first_legs.values()))
        self.assertEqual(
            self.nearest.complete_travel_time,
            min(candidate.complete_travel_time for candidate in self.report.ranked),
        )

    def test_the_farthest_candidate_is_the_farthest_by_first_leg(self) -> None:
        first_legs = {
            candidate.stop_id: candidate.travel_time
            for candidate in list(self.report.ranked) + list(self.report.rejected)
        }
        self.assertEqual(self.farthest.travel_time, max(first_legs.values()))

    def test_changing_the_departure_time_changes_the_recommendation(self) -> None:
        outcomes = departure_sweep()
        self.assertEqual(tuple(outcome.local_hour for outcome in outcomes), DEPARTURE_SWEEP_HOURS)
        recommendations = [outcome.recommended_id for outcome in outcomes]
        self.assertIsNone(
            next((item for item in recommendations if item is None), None),
            "every departure hour must still produce a recommendation",
        )
        self.assertGreater(len(set(recommendations)), 1)
        # Leaving at 08:00 makes the nearest customer the strongest complete route; leaving at
        # 04:00 does not.
        self.assertEqual(outcomes[-1].recommended_id, HEADLINE_STOP_IDS["nearest"])
        self.assertNotEqual(outcomes[0].recommended_id, HEADLINE_STOP_IDS["nearest"])
        # The recommendation actually changes three times across the sweep, so the demo does not
        # rely on a single flip.
        self.assertEqual(
            [outcome.recommended_id for outcome in outcomes],
            [RECOMMENDED, RECOMMENDED, "S08-UNKNOWN-HOURS", "S14-PRIORITY-2", HEADLINE_STOP_IDS["nearest"]],
        )
        self.assertTrue(all(outcome.note for outcome in outcomes[2:]))
        self.assertEqual(outcomes[2].note, f"changed from {RECOMMENDED} at 05:00")

    def test_the_sweep_reports_the_complete_route_of_each_recommendation(self) -> None:
        for outcome in departure_sweep():
            with self.subTest(hour=outcome.local_hour):
                self.assertEqual(outcome.status, RecommendationStatus.RECOMMENDED.value)
                self.assertIsNotNone(outcome.complete_duration)
                self.assertIsNotNone(outcome.complete_waiting)
                self.assertIsNotNone(outcome.first_leg)
                self.assertGreater(outcome.ranked, 0)
                # The bottleneck stop stays binding at every departure hour, so the sweep reports
                # both a ranking and explicit rejections rather than hiding either.
                self.assertGreater(outcome.rejected, 0)

    def test_the_first_leg_view_would_have_picked_a_different_stop(self) -> None:
        # The Stage 1 view scored the first leg only (travel + 2 x waiting at the first stop).
        first_leg_scores = {
            candidate.stop_id: candidate.travel_time + 2 * candidate.waiting_time
            for candidate in self.report.ranked
        }
        first_leg_best = min(first_leg_scores, key=lambda stop_id: (first_leg_scores[stop_id], stop_id))
        self.assertNotEqual(first_leg_best, self.report.recommended_stop_id)


class WeightSensitivityTests(unittest.TestCase):
    """D31: the demo report shows the provisional weights' sensitivity over the complete routes."""

    def setUp(self) -> None:
        self.plan = build_demo_plan()
        self.rows = weight_sensitivity()

    def test_the_ratios_are_evaluated_over_the_complete_route_objective(self) -> None:
        self.assertEqual(
            tuple(row.waiting_weight for row in self.rows), WEIGHT_SENSITIVITY_RATIOS
        )
        self.assertEqual(set(WEIGHT_SENSITIVITY_RATIOS), {1.0, 1.5, 2.0, 3.0})
        for row in self.rows:
            with self.subTest(weight=row.waiting_weight):
                self.assertEqual(row.travel_weight, 1.0)
                self.assertIsNotNone(row.recommended_id)
                self.assertGreater(row.complete_duration, 0)

    def test_the_degenerate_one_to_one_case_is_reported_as_a_tie(self) -> None:
        # The 1:1 case is the one D31 calls out: the objective cannot separate the candidates, so
        # the documented tie-break decides. The report says so instead of claiming a clear winner.
        one_to_one = self.rows[0]
        self.assertEqual(one_to_one.waiting_weight, 1.0)
        self.assertGreater(one_to_one.tied_with_recommendation, 1)
        self.assertIn("DEGENERATE 1:1 case", one_to_one.note)
        self.assertIn("input_position, stop_id", one_to_one.note)

    def test_the_shipped_and_heavier_weights_recommend_the_same_stop(self) -> None:
        winners = {row.waiting_weight: row.recommended_id for row in self.rows}
        self.assertEqual(winners[1.0], "S09-ALWAYS-OPEN")
        self.assertEqual(winners[1.5], RECOMMENDED)
        self.assertEqual(winners[2.0], RECOMMENDED)
        self.assertEqual(winners[3.0], RECOMMENDED)

    def test_the_shipped_row_is_the_policy_the_plan_actually_ships(self) -> None:
        # The 2.0 row must be the configured provisional policy (D31), not a different policy that
        # happens to look similar.
        from core.model.cost_policy import CostComponent

        shipped = next(row for row in self.rows if row.waiting_weight == 2.0)
        self.assertEqual(
            shipped.waiting_weight,
            float(self.plan.cost_policy.weights[CostComponent.WAITING_TIME]),
        )
        self.assertEqual(shipped.recommended_id, _demo_report().recommended_stop_id)

    def test_an_unweighted_waiting_time_would_change_the_recommendation(self) -> None:
        # The sensitivity section is not decorative: the answer really does depend on the
        # provisional weight, which is exactly why the weights are marked provisional (D31).
        self.assertNotEqual(self.rows[0].recommended_id, self.rows[-1].recommended_id)
        shipped = demo_evaluation(self.plan)
        self.assertEqual(shipped.recommended_stop_id, RECOMMENDED)

    def test_the_report_prints_every_sensitivity_row(self) -> None:
        text = build_report()
        self.assertIn(
            "SENSITIVITY TO THE PROVISIONAL WAITING WEIGHT - COMPLETE-ROUTE OUTCOMES (D31)", text
        )
        self.assertIn("PROVISIONAL WEIGHTS, NOT PRODUCT TRUTH (D31)", text)
        for row in self.rows:
            with self.subTest(weight=row.waiting_weight):
                self.assertIn(f"{row.waiting_weight:>6.1f}  {row.recommended_id}", text)
        self.assertIn("DEGENERATE 1:1 case", text)


class RecommendationPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = build_demo_plan()
        self.report = _demo_report()
        recommended = self.report.recommended()
        assert recommended is not None
        self.preview = recommendation_preview(self.plan, recommended.stop_id)

    def test_the_preview_starts_at_the_candidate_and_serves_every_enabled_stop(self) -> None:
        self.assertEqual(self.preview.order[0], self.preview.stop_id)
        self.assertEqual(
            sorted(self.preview.order),
            sorted(stop.id for stop in self.plan.active_stops()),
        )
        self.assertEqual(len(self.preview.evaluation.timelines), len(self.plan.active_stops()))

    def test_the_preview_uses_the_committed_route_machinery(self) -> None:
        # I6: the preview and the committed route are produced by the same optimizer, so they
        # cannot diverge.
        candidate = self.report.recommended()
        assert candidate is not None
        self.assertEqual(
            self.preview.evaluation.metrics.duration_sec,
            candidate.estimated_complete_route_duration,
        )
        self.assertEqual(
            self.preview.evaluation.metrics.waiting_sec, candidate.complete_waiting_time
        )
        self.assertEqual(
            self.preview.evaluation.metrics.finish_arrival, candidate.estimated_finish
        )

    def test_local_search_never_worsens_the_greedy_seed(self) -> None:
        self.assertLessEqual(self.preview.final_objective, self.preview.seed_objective)
        self.assertGreaterEqual(self.preview.accepted_moves, 0)

    def test_the_user_baseline_is_the_input_order_and_is_reported(self) -> None:
        # v2 section 30: the BEFORE route is START -> stops in input_position order -> FINISH. The
        # demo's nearest-first work list visits the early-closing customer 17th and therefore misses
        # its 10:00 window: the BEFORE route is infeasible and the optimizer turns it into a
        # feasible AFTER route, which is exactly what a BEFORE/AFTER comparison is for.
        user = self.preview.user_evaluation
        self.assertEqual(user.order, self.plan.user_baseline_order())
        self.assertFalse(user.metrics.feasible)
        self.assertGreater(
            len(user.violations), 0
        )
        self.assertTrue(self.preview.evaluation.metrics.feasible)
        self.assertGreater(
            user.metrics.duration_sec, self.preview.evaluation.metrics.duration_sec
        )
        self.assertGreater(
            user.metrics.distance_m, self.preview.evaluation.metrics.distance_m
        )

    def test_the_user_baseline_violation_is_the_early_closing_customer(self) -> None:
        # The violating stop is named, not merely counted (v2 section 14 / D9).
        violating = {
            violation.stop_id for violation in self.preview.user_evaluation.violations
        }
        self.assertEqual(violating, {HEADLINE_STOP_IDS["early_close"]})

    def test_the_algorithm_baseline_starts_at_the_selected_stop(self) -> None:
        self.assertEqual(
            self.preview.algorithm_evaluation.order[0], self.preview.stop_id
        )

    def test_the_plan_is_never_mutated_by_a_preview(self) -> None:
        self.assertIsNone(self.plan.first_service_stop.selected_stop_id)
        self.assertIs(self.plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)


class ReportTextTests(unittest.TestCase):
    def test_report_is_deterministic(self) -> None:
        self.assertEqual(build_report(), build_report())

    def test_report_labels_synthetic_data(self) -> None:
        report = build_report()
        self.assertIn("DEMO / SYNTHETIC DATA", report)
        self.assertIn("NOT road routing", report)

    def test_report_marks_the_weights_as_provisional(self) -> None:
        report = build_report()
        self.assertIn("demo_provisional_v1", report)
        self.assertIn("PROVISIONAL DEMO WEIGHTS, not product truth", report)

    def test_report_contains_every_required_section(self) -> None:
        report = build_report()
        for section in (
            "PLAN",
            "STATUS AND WORK",
            "RECOMMENDED FIRST STOP",
            "TOP 5 CANDIDATES BY COMPLETE ROUTE OUTCOME",
            "COMPLETE ROUTE THE RECOMMENDATION WOULD PRODUCE",
            "NEAREST vs FARTHEST vs THE RECOMMENDATION",
            "BASELINES",
            "DEPARTURE-TIME SWEEP",
            "FINGERPRINTS",
            "REJECTED / INFEASIBLE CANDIDATES",
            "SENSITIVITY TO THE PROVISIONAL WAITING WEIGHT",
            "PERFORMANCE",
            "DETERMINISM",
        ):
            with self.subTest(section=section):
                self.assertIn(section, report)

    def test_report_states_the_status_and_the_recommendation(self) -> None:
        report = build_report()
        self.assertIn(f"recommended {RECOMMENDED} of 31 candidates evaluated", report)
        self.assertIn("31 evaluated = 26 fully feasible (ranked) + 5 rejected", report)
        self.assertIn("31 candidates", report)

    def test_report_prints_the_plan_shape(self) -> None:
        report = build_report()
        self.assertIn("32 total, 31 enabled, 1 disabled, 17 opening at 08:00", report)
        self.assertIn("default service   : 10m per stop", report)
        self.assertIn("S10-DISABLED", report)

    def test_report_prints_the_complete_route_of_the_recommendation(self) -> None:
        report = build_report()
        self.assertIn("PREVIEW ONLY: nothing is committed", report)
        self.assertIn("complete duration", report)
        self.assertIn("FINISH", report)
        for column in ("arrive", "opens", "wait", "start", "svc", "depart", "window", "late"):
            with self.subTest(column=column):
                self.assertIn(column, report)

    def test_report_prints_the_nearest_and_farthest_with_their_rank(self) -> None:
        report = build_report()
        self.assertIn("nearest       S01-NEAR", report)
        self.assertIn("farthest      S05-FARTHEST", report)
        self.assertIn("ranks                       : recommended #1", report)
        # The nearest is ranked last and the farthest is rejected: neither is the recommendation.
        self.assertIn(f"nearest #{len(_demo_report().ranked)}", report)
        self.assertIn("farthest REJECTED of", report)

    def test_report_prints_both_baselines_and_what_was_saved(self) -> None:
        report = build_report()
        self.assertIn("USER (input_position order)", report)
        self.assertIn(f"OPTIMIZED (around {RECOMMENDED})", report)
        self.assertIn("ALGORITHM (greedy seed, internal)", report)
        self.assertIn("saved by optimizing :", report)
        self.assertIn("never shown as the driver's BEFORE route", report)
        # An infeasible BEFORE route is printed as infeasible rather than hidden.
        self.assertIn("20:24  NO", report)

    def test_report_prints_the_complete_route_metrics_and_the_objective_breakdown(self) -> None:
        report = build_report()
        for column in ("1st leg", "c.trav", "c.wait", "c.svc", "c.dur", "travel*w", "wait*w"):
            with self.subTest(column=column):
                self.assertIn(column, report)
        self.assertIn("travel_time 24900 x 1 + waiting_time 6720 x 2 = 38340", report)

    def test_report_prints_the_fingerprints_and_how_they_differ(self) -> None:
        report = build_report()
        self.assertIn("recommendation fingerprint (plan.inputs_fingerprint)", report)
        self.assertIn("same fingerprint after selecting", report)
        self.assertIn(f"route fingerprint, first stop {RECOMMENDED}", report)
        self.assertIn("route fingerprint, first stop S08-UNKNOWN-HOURS", report)
        self.assertIn("deliberately does not", report)

    def test_report_prints_the_work_counters(self) -> None:
        report = build_report()
        self.assertIn("optimizer runs    : 31", report)
        self.assertIn("leg cache         : 71270 hits, 2014 misses, 1024 entries", report)

    def test_the_performance_stop_count_label_matches_the_plan_shape(self) -> None:
        # GAP 2 (U5): the counters measure the **enabled** stops, so the scale label must not call a
        # 32-stop plan a "31-stop" plan. The label is printed from the plan's own counts, so it is
        # compared with the plan the report was built from - not with a remembered number.
        plan = build_demo_plan()
        rendered = build_report(plan=plan)
        expected_label = (
            f"  {len(plan.active_stops())} enabled stops ({len(plan.stops)} stops, "
            f"{len(plan.disabled_stops())} disabled) : "
            f"{len(plan.active_stops())} candidates"
        )
        self.assertEqual(
            expected_label, "  31 enabled stops (32 stops, 1 disabled) : 31 candidates"
        )
        # The recommended path prints it once, and the no-feasible-route path prints the same label.
        self.assertEqual(rendered.count(expected_label), 1)
        infeasible = dataclasses.replace(
            plan,
            stops=tuple(
                dataclasses.replace(
                    stop,
                    service_window=ServiceWindow.fixed(time(3, 0), time(3, 30)),
                )
                if stop.id == HEADLINE_STOP_IDS["farthest"]
                else stop
                for stop in plan.stops
            ),
        )
        self.assertIn(expected_label, build_report(plan=infeasible))
        # The label's numbers are the plan's own enabled/total/disabled counts, so it cannot drift.
        self.assertIn(
            f"{len(plan.stops)} total, {len(plan.active_stops())} enabled, "
            f"{len(plan.disabled_stops())} disabled",
            rendered,
        )
        self.assertEqual(len(plan.active_stops()), 31)
        self.assertEqual(len(plan.stops), 32)
        self.assertEqual(len(plan.disabled_stops()), 1)
        self.assertNotIn("31-stop demo plan", rendered)
        self.assertNotIn("31-stop demo plan", build_report(plan=infeasible))

    def test_report_prints_the_rejected_candidates_with_their_violating_stops(self) -> None:
        # U5 review: the delivered narrative must actually print the v2 section 14 diagnostics,
        # grouped by candidate, naming the violating stop ids.
        report = build_report()
        self.assertIn("REJECTED / INFEASIBLE CANDIDATES", report)
        self.assertIn("5 of 31 candidates have an infeasible complete route", report)
        self.assertIn("REJECTED S05-FARTHEST", report)
        self.assertIn("violating stops: S32-EARLY-CLOSE", report)
        self.assertIn("cannot be served within its permitted window", report)
        self.assertNotIn(
            f"none: all 31 enabled stops produced a fully feasible complete route", report
        )

    def test_report_references_the_100_stop_benchmark_and_its_command(self) -> None:
        report = build_report()
        self.assertIn(BENCHMARK_COMMAND, report)
        self.assertIn(f"stops {RECORDED_BENCHMARK.stop_count} enabled", report)
        self.assertIn("RECORDED measurement", report)
        self.assertIn("interim limitation (D34)", report)
        self.assertIn(f"<= {DEMO_EVALUATION_RUNTIME_BUDGET_SEC:.0f}s", report)

    def test_report_without_timings_says_how_to_measure_them(self) -> None:
        report = build_report()
        self.assertIn("not measured in this call", report)
        self.assertIn("python -m demo.report", report)

    def test_report_with_timings_marks_them_as_measured_wall_clock(self) -> None:
        from demo.report import EvaluationTimings

        report = build_report(timings=EvaluationTimings(4.5, 21.0))
        self.assertIn("exhaustive evaluation 4.50s", report)
        self.assertIn("departure sweep 21.00s", report)
        self.assertIn("MEASURED WALL CLOCK, machine-dependent", report)

    def test_report_states_the_ranking_key(self) -> None:
        self.assertIn(
            "(score, complete duration, input_position, stop_id)", build_report()
        )

    def test_the_sweep_footer_maps_every_hour_to_its_recommendation(self) -> None:
        report = build_report()
        for outcome in departure_sweep():
            with self.subTest(hour=outcome.local_hour):
                self.assertIn(
                    f"{outcome.local_hour:02d}:00 -> {outcome.recommended_id}", report
                )


class ObjectiveSuperlativeTests(unittest.TestCase):
    """The "why the recommendation wins" superlative names the ranked set it is true of (U5 review).

    The objective only ranks the **fully feasible** candidates: the five rejected candidates are
    never ranked and their infeasible scores are not comparable with a feasible one, as the same
    report says in its rejection section (v2 section 14, D9). The old sentence claimed "the lowest
    objective of all 31 candidates evaluated", which can be read as ranking an infeasible route -
    and nothing in the report proves it, because the objective ranks 26 candidates, not 31.
    """

    def setUp(self) -> None:
        self.plan = build_demo_plan()
        self.report = _demo_report()
        self.recommended = self.report.recommended()
        assert self.recommended is not None
        self.text = build_report()

    def test_the_report_prints_the_ranked_set_superlative(self) -> None:
        self.assertEqual(len(self.report.ranked), 26)
        self.assertEqual(len(self.report.rejected), 5)
        self.assertEqual(self.report.candidates_evaluated, 31)
        self.assertIn(
            "the lowest objective of the 26 ranked fully feasible candidates is 38340 "
            "(5 of the 31 evaluated are REJECTED, never ranked and carry no comparable score)",
            self.text,
        )
        self.assertNotIn("the lowest objective of all", self.text)

    def test_the_printed_minimum_is_the_lowest_ranked_score(self) -> None:
        # The sentence claims a lowest objective, so it must print the value it claims: the minimum
        # of ``report.ranked``, derived from the ranking the sentence names (U5: the claim was
        # unproven while the number itself was nowhere in the sentence).
        scores = [candidate.score for candidate in self.report.ranked]
        self.assertEqual(scores, sorted(scores))
        lowest_ranked_score = min(candidate.score for candidate in self.report.ranked)
        sentence = objective_winner_sentence(self.report)
        self.assertIn(f"candidates is {lowest_ranked_score:.0f} ", sentence)
        self.assertIn(f"candidates is {lowest_ranked_score:.0f} ", self.text)
        # The printed minimum IS the recommended candidate's score: the ranking is ordered by that
        # objective and the recommendation is its head (v2 sections 12-14).
        self.assertEqual(lowest_ranked_score, self.recommended.score)
        self.assertEqual(lowest_ranked_score, self.report.ranked[0].score)
        self.assertEqual(self.report.ranked[0].stop_id, self.recommended.stop_id)
        self.assertEqual(f"{lowest_ranked_score:.0f}", "38340")

    def test_the_superlative_is_derived_from_the_ranking_not_remembered(self) -> None:
        # The sentence is produced by one helper from the report's own ranked/rejected sets, so it
        # cannot describe a larger set than the one the objective actually ranks.
        self.assertEqual(
            objective_winner_sentence(self.report),
            "- the lowest objective of the 26 ranked fully feasible candidates is 38340 "
            "(5 of the 31 evaluated are REJECTED, never ranked and carry no comparable score).",
        )
        self.assertIn(objective_winner_sentence(self.report), self.text)

    def test_the_recommended_score_is_the_minimum_of_the_ranked_scores_only(self) -> None:
        ranked_scores = [
            score_of(candidate.metrics, self.plan.cost_policy) for candidate in self.report.ranked
        ]
        self.assertEqual(len(ranked_scores), len(self.report.ranked))
        self.assertEqual(
            min(ranked_scores), score_of(self.recommended.metrics, self.plan.cost_policy)
        )
        self.assertEqual(min(ranked_scores), self.recommended.score)
        # No rejected candidate participates in that minimum: each one carries an infeasible score
        # and no rank at all (v2 section 14, D9).
        rejected_scores: list[float] = []
        for candidate in self.report.rejected:
            with self.subTest(stop=candidate.stop_id):
                self.assertFalse(candidate.feasible)
                self.assertTrue(candidate.violating_stop_ids)
                self.assertIsNone(self.report.rank_of(candidate.stop_id))
                self.assertNotIn(candidate.stop_id, self.report.ranked_ids())
                rejected_scores.append(candidate.score)
        self.assertEqual(len(rejected_scores), 5)
        # The rejected candidates answer a different question: they are never ranked, and none of
        # their infeasible scores participates in the ranking the sentence names. In this fixture
        # they all score above the recommendation, so an "of all 31 evaluated" superlative was not
        # merely unproven - it ranked a set the objective never ranked.
        infeasible_scores = [
            score_of(candidate.metrics, self.plan.cost_policy)
            for candidate in self.report.rejected
        ]
        self.assertEqual(len(infeasible_scores), len(self.report.rejected))
        self.assertGreater(min(infeasible_scores), min(ranked_scores))
        self.assertNotIn(min(ranked_scores), infeasible_scores)
        self.assertIsNone(
            self.report.rank_of(
                self.report.rejected[infeasible_scores.index(min(infeasible_scores))].stop_id
            )
        )


class RejectedCandidateRenderingTests(unittest.TestCase):
    """The rejection diagnostics are rendered grouped by candidate, with violating stop ids.

    The demo fixture now rejects five candidates on its own, so the shipped report prints these
    lines. These tests additionally build two deliberately tightened variants of the same plan to
    prove that the renderer keeps naming the violating stops for *any* rejected candidate - and that
    a fully infeasible plan never promotes a candidate.
    """

    @staticmethod
    def _plan_with_window(
        stop_id: str,
        open_hour: int,
        open_minute: int,
        close_hour: int,
        close_minute: int,
    ):
        plan = build_demo_plan()
        window = ServiceWindow.fixed(
            time(open_hour, open_minute), time(close_hour, close_minute)
        )
        stops = tuple(
            dataclasses.replace(stop, service_window=window) if stop.id == stop_id else stop
            for stop in plan.stops
        )
        return dataclasses.replace(plan, stops=stops)

    def test_the_shipped_report_renders_the_fixture_rejections(self) -> None:
        report = _demo_report()
        lines = rejected_candidate_lines(report)
        self.assertEqual(len(lines), len(report.rejected))
        self.assertGreaterEqual(len(lines), 1)
        for candidate, line in zip(report.rejected, lines):
            with self.subTest(stop=candidate.stop_id):
                self.assertIn(f"REJECTED {candidate.stop_id}", line)
                self.assertIn("violating stops: S32-EARLY-CLOSE", line)
                self.assertIn("cannot be served within its permitted window", line)

    def test_a_tight_window_rejects_some_candidates_and_names_the_violating_stop(self) -> None:
        from core.engine.first_stop.evaluation import evaluate_first_stop_candidates

        plan = self._plan_with_window(HEADLINE_STOP_IDS["farthest"], 8, 0, 8, 20)
        report = evaluate_first_stop_candidates(plan=plan, travel_matrix=demo_matrix())

        self.assertIs(report.status, RecommendationStatus.RECOMMENDED)
        self.assertGreater(len(report.rejected), 0)
        self.assertGreater(len(report.ranked), 0)
        self.assertEqual(
            len(report.ranked) + len(report.rejected), len(plan.active_stops())
        )

        lines = rejected_candidate_lines(report)
        self.assertEqual(len(lines), len(report.rejected))
        self.assertIn(f"violating stops: {HEADLINE_STOP_IDS['farthest']}", lines[0])
        for candidate in report.rejected:
            self.assertNotIn(candidate.stop_id, report.ranked_ids())
            self.assertIn(HEADLINE_STOP_IDS["farthest"], candidate.violating_stop_ids)
            reasons = report.reasons_for(candidate.stop_id)
            self.assertTrue(reasons)
            self.assertTrue(all(reason.candidate_stop_id == candidate.stop_id for reason in reasons))

    def test_an_impossible_window_leaves_no_fully_feasible_route_and_no_winner(self) -> None:
        from core.engine.first_stop.evaluation import evaluate_first_stop_candidates

        plan = self._plan_with_window(HEADLINE_STOP_IDS["farthest"], 3, 0, 3, 30)
        report = evaluate_first_stop_candidates(plan=plan, travel_matrix=demo_matrix())

        self.assertIs(report.status, RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE)
        self.assertEqual(report.ranked, ())
        self.assertIsNone(report.recommended_stop_id)
        self.assertGreater(len(report.diagnostics), 0)
        self.assertEqual(len(rejected_candidate_lines(report)), len(report.rejected))

    def test_the_report_of_an_infeasible_plan_says_so_and_lists_the_rejections(self) -> None:
        plan = self._plan_with_window(HEADLINE_STOP_IDS["farthest"], 3, 0, 3, 30)
        text = build_report(plan=plan)

        self.assertIn("no_fully_feasible_route", text)
        self.assertIn("none: no candidate produced a fully feasible complete route", text)
        self.assertIn("REJECTED / INFEASIBLE CANDIDATES", text)
        self.assertIn(f"violating stops: {HEADLINE_STOP_IDS['farthest']}", text)
        self.assertNotIn("TOP 5 CANDIDATES", text)


class CommandLineTests(unittest.TestCase):
    def test_cli_prints_the_report_and_measures_the_runtime(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("RoutePilot demo - COMPLETE-ROUTE first-stop recommendation", output)
        self.assertIn("MEASURED WALL CLOCK, machine-dependent", output)

    def test_cli_accepts_the_offline_tzdata_flag(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["--allow-system-tzdata"])
        self.assertEqual(exit_code, 0)

    def test_the_demo_evaluation_is_memoized_so_repeated_reads_are_cheap(self) -> None:
        # The exhaustive loop costs seconds; the report and its tests share one evaluation.
        started = timer.perf_counter()
        build_report()
        elapsed = timer.perf_counter() - started
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
