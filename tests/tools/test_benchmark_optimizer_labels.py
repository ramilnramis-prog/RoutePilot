"""The benchmark's printed fixture labels, pinned to each plan's own counts (v2 section 24, D20, D36).

Two label contracts are pinned here:

* independent review finding (U5, final label-fix pass): the benchmark printed
  ``demo plan, 30 enabled stops (DEMO/SYNTHETIC)`` for a plan that holds **32 stops / 31 enabled /
  1 disabled**, so a printed label disagreed with the plan it named;
* the owner's scale decision (D36, Stage 2.1 sections A/E/F): the benchmark measures a
  **~50-enabled-stop portfolio fixture** - the primary MVP target - alongside the ~30-stop demo plan
  and the ~100-stop **stress reference**, and the stress entry must be labelled as a stress /
  engineering reference that is **not performance-qualified**. A fixture with materially fewer
  enabled stops must never be labelled a "50-stop" fixture without stating its enabled count.

Every label is rendered from the plan object's own enabled/total/disabled counts in the same
"31 enabled stops (32 stops, 1 disabled)" shape the demo report prints, and these tests compare the
label with that same plan object, so a recalibrated fixture moves both together.

These tests measure nothing: they read the label from the dataset builder and compare its numbers
with the same plan's counts. The exhaustive benchmark loop is never run here - it belongs to
``tests/engine/test_optimizer_performance.py`` behind the slow-test gate.
"""

from __future__ import annotations

import unittest
from typing import Any

from core.time import tzdata
from demo.scale_dataset import (
    PORTFOLIO_DISABLED_STOP_COUNT,
    PORTFOLIO_ENABLED_STOP_COUNT,
    PORTFOLIO_STOP_COUNT,
)
from tests import TZDATA_FALLBACK_PATH
from tools.benchmark_optimizer import (
    ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
    PORTFOLIO_PROFILE,
    STRESS_PROFILE,
    _datasets,
)


def setUpModule() -> None:
    """The demo plan declares an IANA zone (D2), so an offline machine needs the TZif fallback.

    Same offline bootstrap ``tests.engine.test_optimizer_performance`` uses: the fallback is
    activated only when the suite had to fall back, and it is an environment workaround, never a
    product behaviour (D12).
    """
    if TZDATA_FALLBACK_PATH is not None:
        tzdata.activate_system_tzif_fallback()


def demo_dataset() -> Any:
    """The benchmark's single demo dataset, built for the test."""
    datasets = list(_datasets(100, include_scale=False, include_demo=True, include_portfolio=False))
    if len(datasets) != 1:
        raise AssertionError(f"expected exactly one demo dataset, got {len(datasets)}")
    return datasets[0]


def portfolio_dataset() -> Any:
    """The benchmark's ~50-enabled-stop portfolio dataset, built for the test."""
    datasets = list(
        _datasets(100, include_scale=False, include_demo=False, include_portfolio=True)
    )
    if len(datasets) != 1:
        raise AssertionError(f"expected exactly one portfolio dataset, got {len(datasets)}")
    return datasets[0]


def stress_dataset() -> Any:
    """The benchmark's ~100-stop stress dataset, built for the test."""
    datasets = list(
        _datasets(100, include_scale=True, include_demo=False, include_portfolio=False)
    )
    if len(datasets) != 1:
        raise AssertionError(f"expected exactly one stress dataset, got {len(datasets)}")
    return datasets[0]


class DemoPlanLabelTests(unittest.TestCase):
    """The printed demo-plan label states the demo plan's real enabled/total/disabled counts."""

    def test_the_demo_label_matches_the_demo_plans_own_counts(self) -> None:
        # The comparison is against the plan returned by the same dataset, not a remembered number,
        # so a recalibrated fixture moves the label and the assertion together - and a hard-coded
        # label would fail here.
        dataset = demo_dataset()
        label, plan = dataset.label, dataset.plan
        enabled = len(plan.active_stops())
        total = len(plan.stops)
        disabled = len(plan.disabled_stops())

        # The fixture's current shape, stated once, so drift is visible in the failure message.
        self.assertEqual((total, enabled, disabled), (32, 31, 1))

        expected_counts = f"{enabled} enabled stops ({total} stops, {disabled} disabled)"
        self.assertIn(expected_counts, label, "the label must carry the plan's own counts")
        self.assertEqual(
            label,
            f"demo plan, {expected_counts} (DEMO/SYNTHETIC)",
        )
        # The marker that keeps synthetic demo data from being read as real routing must survive.
        self.assertIn("DEMO/SYNTHETIC", label)
        # The defect this test exists for: a label claiming the plan's enabled count is its total
        # count, or naming the approximate "~30" as an exact number.
        self.assertNotIn("30 enabled stops", label)

    def test_the_demo_label_names_the_exact_counts_and_not_the_approximate_scale(self) -> None:
        # "~30 stops" is spec vocabulary for the plan's approximate scale and must stay that way in
        # prose; the printed dataset label is exact, so the tilde must not appear in it.
        dataset = demo_dataset()
        label, plan = dataset.label, dataset.plan
        self.assertNotIn("~", label)
        self.assertEqual(len(plan.active_stops()) + len(plan.disabled_stops()), len(plan.stops))
        self.assertEqual(len(plan.stops), 32)

    def test_only_the_demo_dataset_is_yielded_when_the_scale_fixtures_are_off(self) -> None:
        # Guards the tests above: with --no-scale --no-portfolio the benchmark yields exactly this
        # one dataset, so the demo label pinned here is the label that run prints.
        dataset = demo_dataset()
        self.assertEqual(dataset.evidence, "deterministic demo plan of demo/dataset.py")
        self.assertIn("demo plan", dataset.label)
        self.assertNotIn("scale fixture", dataset.label)
        self.assertNotIn("portfolio fixture", dataset.label)
        self.assertEqual(dataset.profile.kind, "demo")


class PortfolioFixtureLabelTests(unittest.TestCase):
    """D36: the ~50-enabled-stop portfolio fixture is the primary MVP target, labelled as such."""

    def test_the_portfolio_fixture_holds_exactly_the_declared_enabled_count(self) -> None:
        # The owner's target is "approximately 50 ENABLED service stops", so the fixture is pinned
        # on its enabled count - never on its total. 55 stops, 5 disabled by the fixture's own
        # deterministic policy, 50 enabled.
        dataset = portfolio_dataset()
        plan = dataset.plan
        enabled = len(plan.active_stops())
        total = len(plan.stops)
        disabled = len(plan.disabled_stops())

        self.assertEqual((enabled, total, disabled), (50, 55, 5))
        self.assertEqual(enabled, PORTFOLIO_ENABLED_STOP_COUNT)
        self.assertEqual(total, PORTFOLIO_STOP_COUNT)
        self.assertEqual(disabled, PORTFOLIO_DISABLED_STOP_COUNT)
        self.assertEqual(enabled + disabled, total)
        # The disabled stops are the deterministic every-10th ones, never the first stop.
        self.assertEqual(
            [str(stop.id) for stop in plan.disabled_stops()],
            ["K010", "K020", "K030", "K040", "K050"],
        )
        self.assertTrue(plan.active_stops()[0].id == "K000")

    def test_the_portfolio_label_states_the_exact_enabled_count_and_the_primary_target(self) -> None:
        dataset = portfolio_dataset()
        plan = dataset.plan
        label = dataset.label
        enabled = len(plan.active_stops())
        total = len(plan.stops)
        disabled = len(plan.disabled_stops())

        self.assertEqual(
            label,
            f"portfolio fixture (PRIMARY MVP TARGET, D36), {enabled} enabled stops "
            f"({total} stops, {disabled} disabled) (DEMO/SYNTHETIC)",
        )
        self.assertIn("50 enabled stops (55 stops, 5 disabled)", label)
        self.assertIn("PRIMARY MVP TARGET", label)
        self.assertIn("DEMO/SYNTHETIC", label)
        # A fixture with materially fewer enabled stops must never be called a "50-stop" fixture
        # without stating the enabled count: the label always carries both numbers.
        self.assertNotIn("50 stops", label)
        self.assertEqual(dataset.profile, PORTFOLIO_PROFILE)
        self.assertTrue(dataset.profile.is_portfolio)
        self.assertTrue(dataset.profile.is_performance_qualified)
        # The primary target's v2 section 20 numbers are REPORTED, not asserted: the shipped exact
        # implementation measures far outside the acceptable target at this scale, and closing that
        # gap needs the deferred incremental/delta evaluator. What the primary MVP scale DOES carry
        # is the generous owner-accepted regression bound (D34) - the same ~150 s guard the other
        # scales use and roughly seven times the measured ~21-22 s at 50 enabled stops - so the
        # primary MVP scale is not left without a bound while the reported <= 5 s target keeps its
        # own honest verdict (the U6b review fix).
        self.assertEqual(dataset.profile.asserted_bound_sec, ACCEPTED_INTERIM_LOOP_LIMIT_SEC)
        self.assertEqual(dataset.profile.asserted_bound_sec, 150.0)
        self.assertIn("owner-accepted bound", dataset.profile.asserted_bound_note)
        self.assertIn("MVP SCALE TARGET", dataset.profile.description)
        self.assertIn("REPORTED against the measured number", dataset.profile.description)
        self.assertIn("asserted guard", dataset.profile.description)

    def test_the_portfolio_fixture_is_deterministic_across_builds(self) -> None:
        # Determinism is the whole point of the fixture: identical plans, identical input order and
        # one identical recommendation fingerprint, in this process and across calls.
        first = portfolio_dataset().plan
        second = portfolio_dataset().plan

        self.assertEqual([stop.id for stop in first.stops], [stop.id for stop in second.stops])
        self.assertEqual(
            [(stop.latitude, stop.longitude) for stop in first.stops],
            [(stop.latitude, stop.longitude) for stop in second.stops],
        )
        self.assertEqual(
            [stop.input_position for stop in first.stops],
            list(range(PORTFOLIO_STOP_COUNT)),
        )
        self.assertEqual(first.inputs_fingerprint(), second.inputs_fingerprint())
        self.assertEqual(
            [bool(stop.enabled) for stop in first.stops],
            [bool(stop.enabled) for stop in second.stops],
        )
        # Windows, durations and priorities are the shared deterministic rotation, so the portfolio
        # fixture is a scale subset of the same generator as the ~100-stop stress fixture.
        self.assertEqual(len({stop.service_window.window_kind.value for stop in first.stops}), 3)
        self.assertIn(None, {stop.service_duration for stop in first.stops})


class StressFixtureLabelTests(unittest.TestCase):
    """D36: the ~100-stop entry is an engineering stress reference, NOT performance-qualified."""

    def test_the_stress_entry_keeps_its_own_counts_and_is_labelled_not_qualified(self) -> None:
        dataset = stress_dataset()
        plan = dataset.plan
        enabled = len(plan.active_stops())
        total = len(plan.stops)
        disabled = len(plan.disabled_stops())

        # The ~100-stop default is untouched: the same plan, the same 97 enabled stops as the
        # recorded measurement (D34) records.
        self.assertEqual(total, 100)
        self.assertEqual(enabled, 97)
        self.assertEqual(disabled, 3)
        self.assertEqual(
            dataset.label,
            f"stress fixture, 100 stops (NOT performance-qualified, D36), {enabled} enabled stops "
            f"({total} stops, {disabled} disabled) (DEMO/SYNTHETIC)",
        )
        self.assertIn("NOT performance-qualified", dataset.label)
        self.assertIn("97 enabled stops", dataset.label)
        self.assertEqual(dataset.profile, STRESS_PROFILE)
        self.assertFalse(dataset.profile.is_portfolio)
        self.assertFalse(dataset.profile.is_performance_qualified)
        self.assertIn("STRESS REFERENCE", dataset.profile.description)
        self.assertIn("not an MVP gate", dataset.profile.description)
        # The fixture and its honest measured evidence stay.
        self.assertIn("stress fixture of demo/scale_dataset.py", dataset.evidence)
        self.assertIn("not performance-qualified", dataset.evidence)

    def test_an_explicit_non_portfolio_stop_count_is_labelled_a_stress_reference(self) -> None:
        datasets = list(
            _datasets(40, include_scale=True, include_demo=False, include_portfolio=False)
        )
        self.assertEqual(len(datasets), 1)
        self.assertIn("40 stops (NOT performance-qualified, D36)", datasets[0].label)
        self.assertFalse(datasets[0].profile.is_performance_qualified)


class DefaultDatasetSelectionTests(unittest.TestCase):
    """The default run measures all three fixtures, portfolio first, each with its own profile."""

    def test_the_default_selection_is_portfolio_then_stress_then_demo(self) -> None:
        datasets = list(_datasets(100, include_scale=True, include_demo=True))
        self.assertEqual(len(datasets), 3)
        self.assertEqual([dataset.profile.kind for dataset in datasets], ["portfolio", "stress", "demo"])
        self.assertIn("portfolio fixture", datasets[0].label)
        self.assertIn("stress fixture", datasets[1].label)
        self.assertIn("demo plan", datasets[2].label)
        # Every fixture carries its enabled count and the synthetic provenance marker.
        for dataset in datasets:
            with self.subTest(label=dataset.label):
                self.assertIn(f"{len(dataset.plan.active_stops())} enabled stops", dataset.label)
                self.assertIn("DEMO/SYNTHETIC", dataset.label)

    def test_a_dataset_still_unpacks_as_the_historical_four_tuple(self) -> None:
        # The tool's callers and older tests unpack (label, plan, matrix, evidence); the dataclass
        # keeps that order while exposing ``profile`` for the scale decision.
        dataset = portfolio_dataset()
        label, plan, matrix, evidence = dataset
        self.assertEqual(label, dataset.label)
        self.assertIs(plan, dataset.plan)
        self.assertIs(matrix, dataset.matrix)
        self.assertEqual(evidence, dataset.evidence)
        self.assertEqual(dataset[0], dataset.label)
        self.assertEqual(len(dataset), 4)


if __name__ == "__main__":
    unittest.main()
