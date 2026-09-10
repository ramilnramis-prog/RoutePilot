"""First-stop intent and resolution semantics (D4-D9, D11; spec sections 3-6).

The tests below pin down the two things that were explicitly corrected during design:

* provenance and pinning are separate concepts (D6) - Locking an auto recommendation keeps
  ``selection_source='auto_recommendation'``;
* ``selected_stop_id`` may legitimately be ``None`` for several *distinct* reasons (D9).
"""

from __future__ import annotations

import unittest

from core.model.first_stop import (
    CandidateDiagnostic,
    FirstStopCandidate,
    FirstStopIntent,
    FirstStopMode,
    FirstStopResolution,
    FirstStopStatus,
    PinnedVia,
    SelectionSource,
    UNRESOLVED_FIRST_STOP_STATUSES,
)
from core.validation.errors import InvalidRoutePlanError
from tests.support import utc

NOW = utc(2026, 9, 11, 1, 0)
FINGERPRINT = "fingerprint-abc"


class FirstStopIntentTests(unittest.TestCase):
    def test_auto_intent_is_dynamic(self) -> None:
        intent = FirstStopIntent.auto()
        self.assertIs(intent.mode, FirstStopMode.AUTO)
        self.assertFalse(intent.pinned)
        self.assertIsNone(intent.pinned_stop_id)
        self.assertTrue(intent.is_dynamic)

    def test_pinned_without_a_stop_is_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(FirstStopMode.AUTO, True, None)

    def test_stop_without_pin_is_rejected(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopIntent(FirstStopMode.AUTO, False, "s1")

    def test_locked_and_overridden_auto_are_both_pinned_but_differ_in_provenance(self) -> None:
        locked = FirstStopIntent.auto_locked("s1")
        overridden = FirstStopIntent.auto_overridden("s2")
        manual = FirstStopIntent.manual("s3")
        for intent in (locked, overridden, manual):
            self.assertTrue(intent.pinned)
            self.assertFalse(intent.is_dynamic)
        self.assertIs(locked.mode, FirstStopMode.AUTO)
        self.assertIs(overridden.mode, FirstStopMode.AUTO)
        self.assertIs(manual.mode, FirstStopMode.MANUAL)

    def test_manual_requires_an_explicit_stop(self) -> None:
        manual = FirstStopIntent.manual("s1")
        self.assertEqual(manual.pinned_stop_id, "s1")
        self.assertIn("manual", manual.describe())


class FirstStopResolutionTests(unittest.TestCase):
    def test_auto_recommendation_is_not_pinned(self) -> None:
        resolution = FirstStopResolution.auto_recommendation(
            "s1", resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        self.assertIs(resolution.selection_source, SelectionSource.AUTO_RECOMMENDATION)
        self.assertIsNone(resolution.pinned_via)
        self.assertFalse(resolution.is_pinned)
        self.assertTrue(resolution.is_resolved)

    def test_locked_auto_recommendation_keeps_auto_provenance(self) -> None:
        # D6: Lock changes pinned_via, never selection_source.
        resolution = FirstStopResolution.locked_auto_recommendation(
            "s1", resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        self.assertIs(resolution.selection_source, SelectionSource.AUTO_RECOMMENDATION)
        self.assertIs(resolution.pinned_via, PinnedVia.LOCK)
        self.assertTrue(resolution.is_pinned)
        self.assertIsNotNone(resolution.selected_stop_id)

    def test_driver_override_and_manual_are_driver_provenance(self) -> None:
        overridden = FirstStopResolution.driver_override(
            "s1", resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        manual = FirstStopResolution.manual_selection(
            "s2", resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        self.assertIs(overridden.selection_source, SelectionSource.DRIVER)
        self.assertIs(overridden.pinned_via, PinnedVia.OVERRIDE)
        self.assertIs(manual.selection_source, SelectionSource.DRIVER)
        self.assertIs(manual.pinned_via, PinnedVia.MANUAL_MODE)

    def test_provenance_and_pinning_are_independent(self) -> None:
        # The rejected invariant was: auto_recommendation iff not pinned. Both combinations of
        # (auto_recommendation, pinned) must be constructible.
        unpinned = FirstStopResolution.auto_recommendation(
            "s1", resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        pinned = FirstStopResolution.locked_auto_recommendation(
            "s1", resolved_at=NOW, inputs_fingerprint=FINGERPRINT
        )
        self.assertFalse(unpinned.is_pinned)
        self.assertTrue(pinned.is_pinned)
        self.assertEqual(unpinned.selection_source, pinned.selection_source)

    def test_lock_cannot_claim_driver_provenance(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopResolution(
                status=FirstStopStatus.RESOLVED,
                selected_stop_id="s1",
                selection_source=SelectionSource.DRIVER,
                pinned_via=PinnedVia.LOCK,
                resolved_at=NOW,
                inputs_fingerprint=FINGERPRINT,
            )

    def test_override_cannot_claim_auto_provenance(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopResolution(
                status=FirstStopStatus.RESOLVED,
                selected_stop_id="s1",
                selection_source=SelectionSource.AUTO_RECOMMENDATION,
                pinned_via=PinnedVia.OVERRIDE,
                resolved_at=NOW,
                inputs_fingerprint=FINGERPRINT,
            )

    def test_resolved_requires_stop_source_timestamp_and_fingerprint(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopResolution(status=FirstStopStatus.RESOLVED, selected_stop_id="s1")
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopResolution(
                status=FirstStopStatus.RESOLVED,
                selected_stop_id="s1",
                selection_source=SelectionSource.AUTO_RECOMMENDATION,
                resolved_at=NOW,
                inputs_fingerprint="",
            )

    def test_unresolved_states_never_carry_a_placeholder_stop(self) -> None:
        for status in UNRESOLVED_FIRST_STOP_STATUSES:
            resolution = FirstStopResolution.unresolved(status)
            self.assertFalse(resolution.is_resolved)
            self.assertIsNone(resolution.selected_stop_id)
            self.assertIsNone(resolution.selection_source)
            self.assertIsNone(resolution.pinned_via)
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopResolution(
                status=FirstStopStatus.UNRESOLVED_MANUAL_AWAITING_CHOICE,
                selected_stop_id="s1",
            )

    def test_unresolved_factory_refuses_the_resolved_status(self) -> None:
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopResolution.unresolved(FirstStopStatus.RESOLVED)

    def test_no_feasible_candidate_carries_diagnostics(self) -> None:
        resolution = FirstStopResolution.unresolved(
            FirstStopStatus.NO_FEASIBLE_FIRST_STOP,
            diagnostics=(
                CandidateDiagnostic(
                    stop_id="s1",
                    code="time_window_infeasible",
                    message="window closes before the earliest possible arrival",
                ),
            ),
        )
        self.assertEqual(len(resolution.diagnostics), 1)
        self.assertEqual(resolution.diagnostics[0].code, "time_window_infeasible")

    def test_candidate_feasibility_matches_lateness(self) -> None:
        candidate = FirstStopCandidate(
            stop_id="s1",
            travel_time=14100,
            estimated_arrival=utc(2026, 9, 11, 4, 55),
            waiting_time=300,
            lateness=0,
            estimated_complete_route_duration=22860,
            feasible=True,
        )
        self.assertEqual(candidate.lateness, 0)
        with self.assertRaises(InvalidRoutePlanError):
            FirstStopCandidate(
                stop_id="s1",
                travel_time=14100,
                estimated_arrival=utc(2026, 9, 11, 4, 55),
                waiting_time=0,
                lateness=600,
                estimated_complete_route_duration=22860,
                feasible=True,
            )

    def test_explanation_returns_the_selected_candidates_breakdown(self) -> None:
        candidate = FirstStopCandidate(
            stop_id="s1",
            travel_time=14100,
            estimated_arrival=utc(2026, 9, 11, 4, 55),
            waiting_time=300,
            lateness=0,
            estimated_complete_route_duration=22860,
            feasible=True,
            explanation=(("travel_time", 14100.0), ("waiting_time", 300.0)),
        )
        resolution = FirstStopResolution.auto_recommendation(
            "s1", resolved_at=NOW, inputs_fingerprint=FINGERPRINT, top_k=(candidate,)
        )
        self.assertEqual(
            resolution.explanation(),
            {"travel_time": 14100.0, "waiting_time": 300.0},
        )


if __name__ == "__main__":
    unittest.main()
