"""The benchmark's printed demo-plan label, pinned to the demo plan's own counts (v2 section 24, D20).

Independent review finding (U5, final label-fix pass): the benchmark printed
``demo plan, 30 enabled stops (DEMO/SYNTHETIC)`` for a plan that holds **32 stops / 31 enabled /
1 disabled**, so a printed label disagreed with the plan it named. The label is now rendered from
the plan object's own counts in the same "31 enabled stops (32 stops, 1 disabled)" shape the demo
report prints, and this module pins that so the label cannot silently drift again.

These tests measure nothing: they read the label from the dataset builder and compare its numbers
with the same plan object's counts. The exhaustive benchmark loop is never run here - it belongs to
``tests/engine/test_optimizer_performance.py`` behind the slow-test gate.
"""

from __future__ import annotations

import unittest
from typing import Any

from core.time import tzdata
from tests import TZDATA_FALLBACK_PATH
from tools.benchmark_optimizer import _datasets


def setUpModule() -> None:
    """The demo plan declares an IANA zone (D2), so an offline machine needs the TZif fallback.

    Same offline bootstrap ``tests.engine.test_optimizer_performance`` uses: the fallback is
    activated only when the suite had to fall back, and it is an environment workaround, never a
    product behaviour (D12).
    """
    if TZDATA_FALLBACK_PATH is not None:
        tzdata.activate_system_tzif_fallback()


def demo_dataset() -> tuple[str, Any, Any, str]:
    """The benchmark's single demo dataset: (label, plan, matrix, evidence), built for the test."""
    datasets = list(_datasets(100, include_scale=False, include_demo=True))
    if len(datasets) != 1:
        raise AssertionError(f"expected exactly one demo dataset, got {len(datasets)}")
    return datasets[0]


class DemoPlanLabelTests(unittest.TestCase):
    """The printed demo-plan label states the demo plan's real enabled/total/disabled counts."""

    def test_the_demo_label_matches_the_demo_plans_own_counts(self) -> None:
        # The comparison is against the plan returned by the same dataset, not a remembered number,
        # so a recalibrated fixture moves the label and the assertion together - and a hard-coded
        # label would fail here.
        label, plan, _matrix, _evidence = demo_dataset()
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
        label, plan, _matrix, _evidence = demo_dataset()
        self.assertNotIn("~", label)
        self.assertEqual(len(plan.active_stops()) + len(plan.disabled_stops()), len(plan.stops))
        self.assertEqual(len(plan.stops), 32)

    def test_only_the_demo_dataset_is_yielded_when_the_scale_fixture_is_off(self) -> None:
        # Guards the tests above: with --no-scale the benchmark yields exactly this one dataset, so
        # the demo label pinned here is the label that run prints.
        label, plan, _matrix, evidence = demo_dataset()
        self.assertEqual(evidence, "deterministic demo plan of demo/dataset.py")
        self.assertIn("demo plan", label)
        self.assertNotIn("scale fixture", label)


if __name__ == "__main__":
    unittest.main()
