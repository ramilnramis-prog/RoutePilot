"""Demo report and scenario analysis (spec sections 1, 24, 25).

These tests pin the numeric story the demo tells, so a change in behaviour cannot quietly change
the narrative the report shows.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from core.model.service_window import WindowEndPolicy
from demo.dataset import HEADLINE_STOP_IDS
from demo.report import (
    DEPARTURE_SWEEP_HOURS,
    WEIGHT_SENSITIVITY_RATIOS,
    build_report,
    departure_sweep,
    format_duration,
    main,
    stops_without_fixed_window,
    unknown_hours_distortion,
    weight_sensitivity,
    window_end_policy_comparison,
)

T = 3600
M = 60


class FormattingTests(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(format_duration(0), "0m")
        self.assertEqual(format_duration(45 * M), "45m")
        self.assertEqual(format_duration(3 * T + 55 * M), "3h55m")
        self.assertEqual(format_duration(4 * T), "4h00m")


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
            "INPUT ROUTE",
            "FIRST-LEG TIMELINE",
            "ALL CANDIDATE FIRST STOPS",
            "TOP 5 CANDIDATES",
            "WHY THE RECOMMENDATION IS NEITHER THE NEAREST NOR THE FARTHEST",
            "EFFECT OF CHANGING DEPARTURE TIME",
            "SENSITIVITY TO THE PROVISIONAL WAITING WEIGHT",
            "WINDOW END POLICY (D29)",
            "DATA-QUALITY CAVEAT",
            "DETERMINISM",
        ):
            with self.subTest(section=section):
                self.assertIn(section, report)

    def test_report_states_the_window_end_policy_in_force(self) -> None:
        self.assertIn(
            f"window end policy : {WindowEndPolicy.SERVICE_FINISH_BEFORE_END.value}",
            build_report(),
        )

    def test_report_states_that_nothing_is_applied(self) -> None:
        # D4/D32: the report is a recommendation; the driver decides.
        report = build_report()
        self.assertIn("awaiting_first_stop_choice", report)
        self.assertIn("the driver decides", report)
        self.assertIn("Nothing is applied and no working route is committed", report)

    def test_cli_prints_the_report(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("RoutePilot demo scenario", buffer.getvalue())

    def test_cli_accepts_the_offline_tzdata_flag(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(["--allow-system-tzdata"])
        self.assertEqual(exit_code, 0)


class DepartureSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outcomes = departure_sweep()

    def test_sweep_covers_every_configured_hour(self) -> None:
        self.assertEqual(
            tuple(outcome.local_hour for outcome in self.outcomes), DEPARTURE_SWEEP_HOURS
        )

    def test_every_departure_has_a_feasible_winner(self) -> None:
        for outcome in self.outcomes:
            with self.subTest(hour=outcome.local_hour):
                self.assertIsNotNone(outcome.recommended_id)
                self.assertGreater(outcome.recommended_score, 0)

    def test_changing_departure_time_changes_the_winner(self) -> None:
        winners = [outcome.recommended_id for outcome in self.outcomes]
        self.assertEqual(len(set(winners)), len(winners))

    def test_the_winner_gets_closer_as_departure_approaches_opening(self) -> None:
        travels = [outcome.recommended_travel for outcome in self.outcomes]
        self.assertEqual(travels, sorted(travels, reverse=True))

    def test_at_0800_the_nearest_stop_wins(self) -> None:
        final = self.outcomes[-1]
        self.assertEqual(final.local_hour, 8)
        self.assertEqual(final.recommended_id, HEADLINE_STOP_IDS["nearest"])
        self.assertEqual(final.recommended_wait, 0)


class WeightSensitivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = weight_sensitivity()

    def test_all_configured_ratios_are_shown(self) -> None:
        self.assertEqual(
            tuple(row.waiting_weight for row in self.rows), WEIGHT_SENSITIVITY_RATIOS
        )

    def test_equal_weights_are_degenerate_and_favour_the_nearest_stop(self) -> None:
        row = self.rows[0]
        self.assertEqual(row.waiting_weight, 1.0)
        self.assertEqual(row.recommended_id, HEADLINE_STOP_IDS["nearest"])
        self.assertIn("degenerate", row.note)
        self.assertIn("tie", row.note)

    def test_penalising_waiting_favours_arriving_at_opening(self) -> None:
        for row in self.rows[1:]:
            with self.subTest(waiting_weight=row.waiting_weight):
                self.assertEqual(row.recommended_id, HEADLINE_STOP_IDS["on_opening"])

    def test_waiting_is_never_cheaper_than_driving_in_the_demo_policy(self) -> None:
        self.assertGreaterEqual(WEIGHT_SENSITIVITY_RATIOS[-1], 2.0)


class WindowEndPolicyReportTests(unittest.TestCase):
    def test_policy_comparison_flips_feasibility_for_the_same_stop(self) -> None:
        rows = window_end_policy_comparison()
        self.assertEqual(len(rows), 2)
        finish_row, start_row = rows
        self.assertIs(finish_row[0], WindowEndPolicy.SERVICE_FINISH_BEFORE_END)
        self.assertIs(start_row[0], WindowEndPolicy.SERVICE_START_BEFORE_END)
        self.assertFalse(finish_row[2])
        self.assertTrue(start_row[2])
        self.assertIn("cannot be served within the permitted window", finish_row[3])
        self.assertEqual(start_row[3], "")
        # Both policies see the same arrival and the same overtime; only feasibility differs.
        self.assertIn("finish_overtime 10m", finish_row[1])
        self.assertIn("finish_overtime 10m", start_row[1])


class UnknownHoursCaveatTests(unittest.TestCase):
    def test_stops_without_a_fixed_window_are_listed(self) -> None:
        rows = stops_without_fixed_window()
        ids = {row.stop_id for row in rows}
        self.assertIn(HEADLINE_STOP_IDS["unknown_hours"], ids)
        self.assertIn(HEADLINE_STOP_IDS["always_open"], ids)
        for row in rows:
            self.assertGreater(row.travel, 0)
            self.assertGreater(row.score, 0)

    def test_unknown_hours_make_a_stop_look_cheaper(self) -> None:
        distortion = unknown_hours_distortion()
        self.assertEqual(distortion.stop_id, HEADLINE_STOP_IDS["nearest"])
        self.assertLess(distortion.score_if_unknown, distortion.score_known)
        self.assertEqual(distortion.rank_if_unknown, 1)
        self.assertIsNotNone(distortion.rank_known)
        self.assertGreater(distortion.rank_known or 0, 1)
        # With no opening to wait for, the score collapses to the driving time alone.
        self.assertEqual(distortion.score_if_unknown, float(distortion.travel))

    def test_the_real_dataset_keeps_such_stops_out_of_first_place(self) -> None:
        # The distortion is real, but the dataset is built so a far unknown-hours stop does not win.
        rows = stops_without_fixed_window()
        self.assertTrue(all(row.score > 14400 for row in rows))


if __name__ == "__main__":
    unittest.main()
