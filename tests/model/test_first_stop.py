"""First service stop: the driver's decision and the engine's recommendation (D4-D11, D32).

Two things these tests protect:

* the optimizer **recommends** and the driver **decides** - nothing automatic produces a selection
  or a pinned first stop (D4/D5), and a recommendation never implies one (I5/D32);
* provenance describes the driver's *choice* (D6) and is separate from pinning, while
  ``recommended_stop_id`` and ``selected_stop_id`` are separate fields with separate lifecycles.
"""

from __future__ import annotations

import unittest

from core.model.first_stop import (
    CandidateDiagnostic,
    FirstStopCandidate,
    FirstStopIntent,
    FirstStopMode,
    FirstStopRecommendation,
    FirstStopState,
    RecommendationStatus,
    SelectionSource,
)
from core.validation.errors import InvalidRoutePlanError
from tests.support import utc

NOW = utc(2026, 9, 11, 1, 0)
FINGERPRINT = "fingerprint-abc"


def candidate(
    stop_id: str,
    *,
    travel: int = 14100,
    wait: int = 300,
    lateness: int = 0,
    complete: int = 22860,
    feasible: bool | None = None,
    explanation: tuple[tuple[str, float], ...] = (),
) -> FirstStopCandidate:
    if feasible is None:
        feasible = lateness == 0
    return FirstStopCandidate(
        stop_id=stop_id,
        travel_time=travel,
        estimated_arrival=utc(2026, 9, 11, 4, 55),
        waiting_time=wait,
        lateness=lateness,
        estimated_complete_route_duration=complete,
        feasible=feasible,
        explanation=explanation,
    )


class FirstStopIntentTests(unittest.TestCase):
    """The driver's decision, and the state before it exists."""

    def test_recommend_mode_starts_with_no_choice(self) -> None:
        intent = FirstStopIntent.recommend()
        self.assertIs(intent.mode, FirstStopMode.RECOMMEND)
        self.assertFalse(intent.has_selection)
        self.assertIsNone(intent.selected_stop_id)
        self.assertIsNone(intent.selection_source)
        self.assertFalse(intent.pinned)
        self.assertIs(intent.state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)

    def test_manual_mode_starts_with_no_choice(self) -> None:
        intent = FirstStopIntent.manual_mode()
        self.assertIs(intent.mode, FirstStopMode.MANUAL)
        self.assertFalse(intent.has_selection)
        self.assertIs(intent.state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)

    def test_accepting_the_recommendation_records_provenance_and_pins(self) -> None:
        intent = FirstStopIntent.accepted_recommendation("S73")
        self.assertIs(intent.mode, FirstStopMode.RECOMMEND)
        self.assertEqual(intent.selected_stop_id, "S73")
        self.assertIs(intent.selection_source, SelectionSource.ACCEPTED_RECOMMENDATION)
        self.assertTrue(intent.pinned)
        self.assertIs(intent.state, FirstStopState.FIRST_STOP_SELECTED)

    def test_choosing_another_stop_is_a_manual_choice(self) -> None:
        intent = FirstStopIntent.manual_choice("S51")
        self.assertIs(intent.mode, FirstStopMode.RECOMMEND)
        self.assertEqual(intent.selected_stop_id, "S51")
        self.assertIs(intent.selection_source, SelectionSource.MANUAL_CHOICE)
        self.assertTrue(intent.pinned)

    def test_manual_mode_choice_is_a_manual_choice(self) -> None:
        intent = FirstStopIntent.manual_choice("S88", mode=FirstStopMode.MANUAL)
        self.assertIs(intent.mode, FirstStopMode.MANUAL)
        self.assertIs(intent.selection_source, SelectionSource.MANUAL_CHOICE)

    def test_manual_mode_rejects_accepted_recommendation_provenance(self) -> None:
        # In MANUAL mode the driver selects directly, so "accepted a recommendation" cannot apply.
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(
                FirstStopMode.MANUAL, "S73", SelectionSource.ACCEPTED_RECOMMENDATION, True
            )

    def test_selection_without_provenance_is_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(FirstStopMode.RECOMMEND, "S73", None, True)

    def test_provenance_without_selection_is_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(
                FirstStopMode.RECOMMEND, None, SelectionSource.MANUAL_CHOICE, False
            )

    def test_pinned_without_selection_is_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(FirstStopMode.RECOMMEND, None, None, True)

    def test_a_selection_is_always_pinned(self) -> None:
        # v2 section 5: selected_stop_id != None with pinned = false is INVALID.
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(
                FirstStopMode.RECOMMEND, "S73", SelectionSource.MANUAL_CHOICE, False
            )
        self.assertTrue(FirstStopIntent.manual_choice("S73").pinned)
        self.assertTrue(FirstStopIntent.accepted_recommendation("S73").pinned)

    def test_clearing_returns_to_awaiting_choice_and_keeps_the_mode(self) -> None:
        cleared = FirstStopIntent.manual_choice("S73", mode=FirstStopMode.MANUAL).cleared()
        self.assertIs(cleared.mode, FirstStopMode.MANUAL)
        self.assertFalse(cleared.has_selection)
        self.assertIsNone(cleared.selected_stop_id)
        self.assertIsNone(cleared.selection_source)
        self.assertIs(cleared.state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)

    def test_clearing_never_substitutes_a_stop(self) -> None:
        # D8: the domain does not choose on the driver's behalf, in either mode.
        for mode in (FirstStopMode.RECOMMEND, FirstStopMode.MANUAL):
            with self.subTest(mode=mode):
                cleared = FirstStopIntent.manual_choice("S73", mode=mode).cleared()
                self.assertIsNone(cleared.selected_stop_id)

    def test_describe_reports_the_state_honestly(self) -> None:
        self.assertIn("awaiting", FirstStopIntent.recommend().describe())
        self.assertIn("pinned", FirstStopIntent.manual_choice("S73").describe())


class RecommendationTests(unittest.TestCase):
    """The recommendation is derived, advisory, and never a selection."""

    def test_recommendation_does_not_imply_selection(self) -> None:
        # The exact state from the product description: recommended S73, nothing selected.
        recommendation = FirstStopRecommendation.recommended(
            "S73",
            ranked=(candidate("S73"), candidate("S51", complete=23280)),
            resolved_at=NOW,
            inputs_fingerprint=FINGERPRINT,
        )
        intent = FirstStopIntent.recommend()

        self.assertEqual(recommendation.recommended_stop_id, "S73")
        self.assertTrue(recommendation.is_available)
        self.assertIsNone(intent.selected_stop_id)
        self.assertFalse(intent.has_selection)
        self.assertIs(intent.state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)

    def test_recommended_requires_stop_ranked_candidates_time_and_fingerprint(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation(RecommendationStatus.RECOMMENDED)
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation.recommended(
                "S73", ranked=(), resolved_at=NOW, inputs_fingerprint=FINGERPRINT
            )
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation(
                RecommendationStatus.RECOMMENDED,
                "S73",
                (candidate("S73"),),
                None,
                FINGERPRINT,
            )
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation(
                RecommendationStatus.RECOMMENDED,
                "S73",
                (candidate("S73"),),
                NOW,
                "",
            )

    def test_recommended_must_be_one_of_the_ranked_candidates(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation.recommended(
                "S99",
                ranked=(candidate("S73"),),
                resolved_at=NOW,
                inputs_fingerprint=FINGERPRINT,
            )

    def test_no_recommendation_states_never_carry_a_placeholder_stop(self) -> None:
        for status in (
            RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE,
            RecommendationStatus.NO_ACTIVE_STOPS,
            RecommendationStatus.EMPTY_PLAN,
        ):
            with self.subTest(status=status):
                recommendation = FirstStopRecommendation.none(status)
                self.assertFalse(recommendation.is_available)
                self.assertIsNone(recommendation.recommended_stop_id)
                self.assertEqual(recommendation.ranked, ())
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation(
                RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE, "S73"
            )
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopRecommendation(
                RecommendationStatus.EMPTY_PLAN, None, (candidate("S73"),)
            )

    def test_no_feasible_candidate_carries_diagnostics(self) -> None:
        recommendation = FirstStopRecommendation.none(
            RecommendationStatus.NO_FULLY_FEASIBLE_ROUTE,
            diagnostics=(
                CandidateDiagnostic(
                    stop_id="S06",
                    code="time_window_infeasible",
                    message="window closes before the earliest possible arrival",
                ),
            ),
        )
        self.assertEqual(len(recommendation.diagnostics), 1)
        self.assertEqual(recommendation.diagnostics[0].code, "time_window_infeasible")

    def test_ranked_candidates_are_accessible(self) -> None:
        ranked = (candidate("S73"), candidate("S51", complete=23280))
        recommendation = FirstStopRecommendation.recommended(
            "S73", ranked=ranked, resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        self.assertEqual([c.stop_id for c in recommendation.top(1)], ["S73"])
        self.assertEqual(recommendation.rank_of("S51"), 2)
        self.assertIsNone(recommendation.rank_of("S99"))
        self.assertIsNotNone(recommendation.find("S51"))

    def test_explanation_returns_the_recommended_candidates_breakdown(self) -> None:
        ranked = (
            candidate("S73", explanation=(("travel_time", 14100.0), ("waiting_time", 300.0))),
            candidate("S51", complete=23280, explanation=(("travel_time", 12600.0),)),
        )
        recommendation = FirstStopRecommendation.recommended(
            "S73", ranked=ranked, resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        self.assertEqual(
            recommendation.explanation(),
            {"travel_time": 14100.0, "waiting_time": 300.0},
        )

    def test_empty_recommendation_has_no_explanation(self) -> None:
        self.assertEqual(
            FirstStopRecommendation.none(RecommendationStatus.EMPTY_PLAN).explanation(), {}
        )


class FirstStopCandidateTests(unittest.TestCase):
    def test_feasibility_matches_lateness(self) -> None:
        feasible = candidate("S73")
        self.assertEqual(feasible.lateness, 0)
        self.assertTrue(feasible.feasible)

        infeasible = candidate("S06", lateness=600, wait=0)
        self.assertFalse(infeasible.feasible)

    def test_inconsistent_feasibility_is_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            candidate("S06", lateness=600, feasible=True)
        with self.assertRaises(InvalidRoutePlanError):
            candidate("S06", lateness=0, feasible=False)

    def test_complete_route_duration_cannot_be_shorter_than_the_first_leg(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            candidate("S73", travel=14100, complete=1000)


if __name__ == "__main__":
    unittest.main()
