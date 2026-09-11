#!/usr/bin/env python
"""Deterministic benchmark of exhaustive first-stop candidate evaluation (v2 sections 19, 20, D18).

What it measures
----------------

The Stage 2 reference requirement of v2 section 20: *evaluate the complete route for every
feasible first-stop candidate*, with no fixed-K prefilter. For a plan of ``n`` enabled stops that
means ``n`` optimizer runs - one per candidate, each optimizing ``START -> candidate -> the
remaining stops -> FINISH`` - and the time bound applies **"after the travel matrix already
exists"**.

It measures three fixtures, each labelled with its exact **enabled** stop count and its
DEMO/SYNTHETIC provenance:

* the **~30-stop demo plan** (:mod:`demo.dataset`) - the product demo scenario;
* the **~50-enabled-stop portfolio fixture** (:func:`demo.scale_dataset.build_portfolio_plan`) -
  the **primary MVP scale target** of the owner's Stage 2.1 scale decision (D36), 55 stops of which
  50 are enabled;
* the **~100-stop stress fixture** (:mod:`demo.scale_dataset`) - the engineering stress reference,
  which since D36 is **not performance-qualified**: 100 stops is no longer a hard MVP performance
  requirement, and the old <= ~5 s target at that scale is explicitly **not an MVP gate**.

So the run is deliberately in two passes over the *same* shared leg cache:

1. a **warm-up** pass that asks the travel matrix for every leg the exhaustive loop will need;
2. the **measured** pass, whose wall time is compared with the bound.

The warm-up is the same exhaustive loop with the counters ignored. That is the strongest form of
"the travel matrix already exists" this fixture can express: every leg is already memoized, so the
measured pass pays no matrix cost at all, and its numbers are the honest best case for the
current algorithm. The cold (first) pass is reported too - it is useful evidence, and it is
deliberately **not** the pass the bound is judged on.

What it prints
--------------

For every fixture: stop count, candidates evaluated, **candidates whose search hit the
deterministic evaluation ceiling**, optimizer runs, total and per-candidate wall time, total route
evaluations, accepted moves, leg-cache hits/misses/entries, and which performance target applies to
that fixture (D36: the portfolio fixture carries the preferred <= ~3 s / acceptable <= ~5 s targets
of the primary MVP scale plus the generous owner-accepted regression bound; the stress fixture
carries the reported-only v2 section 20 targets, the same regression bound, and the label that its
scale is not performance-qualified).

Two different claims are kept apart on purpose (U3 owner-decided fix 2):

* the **candidate set** is exhaustive - every eligible first-stop candidate gets its own
  complete-route optimization run, and no candidate is prefiltered, shortlisted or skipped
  (v2 section 20, no fixed-K prefilter);
* each candidate's **local search** is the full U2 neighbourhood with a deterministic evaluation
  ceiling (``max_evaluations`` / ``budget_exhausted``), which binds at ~100 stops and truncates
  pass 1. ``candidates_at_ceiling`` reports how many runs were truncated, so the report never
  claims exhaustive verification of a search that was truncated.

Exit status
-----------

**0** unless a fixture regresses past the bound asserted for its own profile, and **1** when one
does - that bound is the regression guard and the only timing this tool asserts. The **stress**
fixture (~100 stops) is asserted against the owner-accepted interim bound
(:data:`ACCEPTED_INTERIM_LOOP_LIMIT_SEC`, decision D34), whose latency the owner accepted while the
full neighbourhood and search quality stayed, and the **demo** fixture is guarded by the same
constant.

The **portfolio** fixture (~50 enabled stops, the primary MVP scale target of D36) is asserted
against the same **generous owner-accepted bound** (:data:`ACCEPTED_INTERIM_LOOP_LIMIT_SEC`, ~150 s,
roughly seven times the measured ~21-22 s at 50 enabled stops): the primary MVP scale therefore has a
real regression guard, and exceeding it fails the run. That bound is headroom, not the target: the
shipped exact implementation still measures well over the v2 section 20 acceptable target at that
scale, closing that gap needs a real algorithmic rewrite (the incremental / delta evaluator D34
defers, which is out of scope here), and the unit's rule is that the ~50-stop figure is
**reported - never faked, never asserted at a flaky second**. The v2 section 20 targets (preferred
<= 3 s, acceptable <= 5 s) are therefore *reported* engineering targets at the portfolio scale, and
the tool prints the measured number, that target and the honest verdict next to it - v2 section 20
itself calls them "engineering targets, not correctness rules".

Determinism
-----------

Everything except wall-clock time is deterministic: the plans, the candidate order, the produced
routes, the route-evaluation counts, the candidates-at-ceiling count, the accepted-move counts and
the cache statistics are identical on every run and on every machine. Only the seconds move.
``--json`` prints a machine readable record of every fixture.

Usage::

    python tools/benchmark_optimizer.py
    python tools/benchmark_optimizer.py --stop-count 100 --json
    python tools/benchmark_optimizer.py --no-scale --no-portfolio
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time as timer
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow `python tools/benchmark_optimizer.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from core.engine.optimizer import CacheStats, LegCache, build_problem, optimize  # noqa: E402
from core.model.first_stop import FirstStopIntent  # noqa: E402
from core.model.ids import StopId  # noqa: E402
from core.model.route_plan import RoutePlan  # noqa: E402
from core.time import tzdata  # noqa: E402
from core.validation.errors import TZDATA_INSTALL_COMMAND  # noqa: E402
from demo.dataset import build_demo_plan  # noqa: E402
from demo.scale_dataset import (  # noqa: E402
    PORTFOLIO_ENABLED_STOP_COUNT,
    PORTFOLIO_STOP_COUNT,
    SCALE_DEFAULT_STOP_COUNT,
    SCALE_WARNING,
    build_portfolio_plan,
    build_scale_plan,
    portfolio_warning_text,
)
from demo.synthetic_matrix import demo_matrix  # noqa: E402

__all__ = [
    "ACCEPTABLE_BUDGET_SEC",
    "ACCEPTED_INTERIM_LOOP_LIMIT_SEC",
    "OWNER_SCALE_STATEMENT",
    "PREFERRED_BUDGET_SEC",
    "PORTFOLIO_PROFILE",
    "STRESS_PROFILE",
    "BenchmarkDataset",
    "DatasetProfile",
    "DatasetMeasurement",
    "OptimizerLoopMeasurement",
    "candidate_first_stops",
    "check_timezone_data",
    "format_report",
    "main",
    "measure_dataset",
    "scale_profile_for",
]

#: v2 section 20: "preferred: <= approximately 3 seconds" for ~100 service stops. **Reported, never
#: asserted**: see :data:`ACCEPTED_INTERIM_LOOP_LIMIT_SEC` and decision D34.
PREFERRED_BUDGET_SEC = 3.0

#: v2 section 20: "acceptable for the early product: <= approximately 5 seconds". Under D36 this is
#: the **preferred / acceptable** pair of the primary MVP scale (~50 enabled stops), and it is
#: reported for the ~100-stop stress fixture, whose latency is no longer an MVP gate (D34/D36).
ACCEPTABLE_BUDGET_SEC = 5.0

#: The **owner-accepted** bound of the whole exhaustive first-stop loop, in seconds (decision D34).
#: The owner accepted the measured warm ~63-76 s at 97 enabled stops as an interim limitation; this
#: constant is that figure with headroom for a slower machine (about twice the worst accepted
#: measurement), so exceeding it is a real regression rather than machine noise. It is the bound the
#: **~100-stop stress fixture** and the **~50-enabled-stop portfolio fixture** are both asserted
#: against - the primary MVP scale carries the same generous guard, roughly seven times its measured
#: ~21-22 s - and it is deliberately **not** the v2 section 20 <= 5 s target at any scale: the owner
#: chose the full U2 neighbourhood and the restored search quality over that time target (D34), and
#: D36 states explicitly that the ~5 s-at-100-stops requirement is **no longer an MVP gate**. A
#: dedicated later unit will remove the latency with an incremental / delta complete-route evaluator.
ACCEPTED_INTERIM_LOOP_LIMIT_SEC = 150.0

#: The owner's Stage 2.1 scale decision, verbatim (D36), printed with every measurement so the
#: numbers are read under the target that actually applies to them.
OWNER_SCALE_STATEMENT = (
    "Portfolio MVP performance target: ~50 stops. 100-stop exhaustive optimization is supported as "
    "an engineering stress scenario but is not yet performance-optimized."
)


@dataclass(frozen=True)
class DatasetProfile:
    """Which scale target a measured fixture belongs to, and therefore what is asserted of it.

    ``kind`` is ``"portfolio"`` for the primary MVP target (~50 enabled stops, D36), ``"stress"``
    for the ~100-stop engineering reference that is not performance-qualified, and ``"demo"`` for
    the ~30-stop product demo plan. ``asserted_bound_sec`` is the only timing this tool asserts for
    that fixture - every fixture carries one, so no measured scale is left without a regression
    guard, and a ``None`` would mean no wall-clock bound is asserted at that scale at all.
    """

    kind: str
    description: str
    asserted_bound_sec: float | None
    asserted_bound_note: str

    @property
    def is_portfolio(self) -> bool:
        return self.kind == "portfolio"

    @property
    def is_performance_qualified(self) -> bool:
        """Whether the fixture's scale is a scale the MVP is performance-qualified at (D36)."""
        return self.is_portfolio


#: The **primary MVP scale target** fixture: ~50 enabled stops (D36). Its v2 section 20 targets
#: (preferred <= ~3 s, acceptable <= ~5 s) are **reported, never asserted**: the shipped exact
#: implementation measures well over the acceptable target here, and closing that gap needs the
#: deferred incremental / delta evaluator (a real algorithmic rewrite, out of scope). What *is*
#: asserted at this scale is the **generous owner-accepted bound** of D34
#: (:data:`ACCEPTED_INTERIM_LOOP_LIMIT_SEC`, ~150 s - about seven times the measured ~21-22 s at 50
#: enabled stops), so the primary MVP scale carries a real regression guard while the reported
#: <= 5 s target keeps its own honest verdict. That is the unit's rule: the ~50-stop figure is
#: reported honestly, guarded generously, and never asserted at a flaky exact second.
PORTFOLIO_PROFILE = DatasetProfile(
    kind="portfolio",
    description=(
        "PRIMARY MVP SCALE TARGET (D36): ~50 enabled stops - the performance-qualified scale of the "
        f"portfolio MVP. Targets: preferred <= ~{PREFERRED_BUDGET_SEC:.0f}s, acceptable "
        f"<= ~{ACCEPTABLE_BUDGET_SEC:.0f}s, REPORTED against the measured number (not asserted); the "
        f"asserted guard is the generous owner-accepted bound of "
        f"{ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.0f}s (D34)."
    ),
    asserted_bound_sec=ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
    asserted_bound_note=(
        f"generous owner-accepted bound <= {ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.0f}s (D34) with headroom "
        "over the measured ~21-22 s at 50 enabled stops: it is the regression guard at the primary "
        "MVP scale, not the "
        f"reported <= {ACCEPTABLE_BUDGET_SEC:.0f}s engineering target, which is printed with its "
        "honest verdict because the gap needs the deferred incremental/delta evaluator rather than a "
        "shortcut or a flaky exact-second assertion (v2 section 20, D34, D36)"
    ),
)

#: The **engineering stress reference**: ~100 stops, which D36 declares **not performance-qualified**
#: and no longer an MVP gate. Asserted only against the owner-accepted interim bound of D34.
STRESS_PROFILE = DatasetProfile(
    kind="stress",
    description=(
        "ENGINEERING STRESS REFERENCE, NOT PERFORMANCE-QUALIFIED (D36): the ~100-stop scale is "
        "future scale, and any other explicit --stop-count is an engineering scale. "
        f"The v2 section 20 preferred <= {PREFERRED_BUDGET_SEC:.0f}s / acceptable "
        f"<= {ACCEPTABLE_BUDGET_SEC:.0f}s targets are reported for it and are not an MVP gate; the "
        "owner accepted the measured latency as an interim limitation (D34)."
    ),
    asserted_bound_sec=ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
    asserted_bound_note=(
        f"owner-accepted interim bound <= {ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.0f}s (D34) - the "
        "asserted guard at the stress scale"
    ),
)

#: The ~30-stop product demo plan: reported against the same acceptable engineering target, guarded
#: by the same interim bound, because it is the plan the product actually shows.
DEMO_PROFILE = DatasetProfile(
    kind="demo",
    description=(
        "PRODUCT DEMO PLAN (~30 enabled stops): reported against the v2 section 20 acceptable target, "
        "guarded by the owner-accepted interim bound (D34)."
    ),
    asserted_bound_sec=ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
    asserted_bound_note=(
        f"owner-accepted interim bound <= {ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.0f}s (D34)"
    ),
)


def scale_profile_for(stop_count: int) -> DatasetProfile:
    """The profile of a ``--stop-count`` scale fixture run (D36).

    A run at the portfolio scale is the primary MVP target and is asserted against the generous
    owner-accepted bound (with its reported v2 section 20 target printed beside it); any other
    explicit ``--stop-count`` is an engineering scale and is labelled as a stress reference that is
    not performance-qualified.
    """
    return PORTFOLIO_PROFILE if stop_count == PORTFOLIO_STOP_COUNT else STRESS_PROFILE


def candidate_first_stops(plan: RoutePlan) -> tuple[StopId, ...]:
    """The complete first-stop candidate set of one plan, in one deterministic order.

    This is the candidate set of v2 section 20 and it is **exhaustive**: every enabled stop of the
    plan, in input order, exactly once. Nothing here ranks, filters or shortlists - a candidate is
    never dropped for looking unpromising, and disabled stops are absent because they are not part
    of the route at all (D20), not because they scored badly. The benchmark pass below and the test
    that pins the candidate set both read this one function, so the set the report claims to
    evaluate is the set the benchmark really optimizes.
    """
    return tuple(stop.id for stop in plan.active_stops())


@dataclass(frozen=True)
class OptimizerLoopMeasurement:
    """One exhaustive first-stop loop, warm and cold, with its deterministic work counters.

    ``candidates`` is the whole candidate set - every enabled stop, once - and
    ``candidates_at_ceiling`` is how many of those runs had their local search truncated by
    ``max_evaluations`` (:attr:`~core.engine.optimizer.local_search.LocalSearchResult.budget_exhausted`).
    The pair is what keeps the report honest: the candidate set stays exhaustive even when a
    per-candidate search could not verify its whole neighbourhood (v2 section 20, decision D34).
    """

    stop_count: int
    candidates: int
    optimizer_runs: int
    route_evaluations: int
    seed_candidates_priced: int
    screened_moves: int
    accepted_moves: int
    total_seconds: float
    warmup_seconds: float
    cache_hits: int
    cache_misses: int
    cache_entries: int
    candidates_at_ceiling: int = 0

    @property
    def seconds_per_candidate(self) -> float:
        return self.total_seconds / self.candidates if self.candidates else 0.0

    @property
    def spec_preferred_met(self) -> bool:
        """v2 section 20's preferred target - **reported**, never asserted (decision D34)."""
        return self.total_seconds <= PREFERRED_BUDGET_SEC

    @property
    def spec_target_met(self) -> bool:
        """v2 section 20's acceptable target - **reported**, never asserted (decision D34)."""
        return self.total_seconds <= ACCEPTABLE_BUDGET_SEC

    @property
    def accepted_bound_met(self) -> bool:
        """Whether the warm loop is inside the owner-accepted interim bound (decision D34).

        This is the asserted bound: ``False`` means the exhaustive loop regressed past the figure
        the owner accepted, which is a real failure, not a missed engineering target.
        """
        return self.total_seconds <= ACCEPTED_INTERIM_LOOP_LIMIT_SEC

    def as_json(self) -> dict[str, object]:
        return {
            "stop_count": self.stop_count,
            "candidates_evaluated": self.candidates,
            "candidates_at_ceiling": self.candidates_at_ceiling,
            "optimizer_runs": self.optimizer_runs,
            "total_seconds": round(self.total_seconds, 6),
            "seconds_per_candidate": round(self.seconds_per_candidate, 6),
            "warmup_seconds": round(self.warmup_seconds, 6),
            "route_evaluations": self.route_evaluations,
            "seed_candidates_priced": self.seed_candidates_priced,
            "screened_moves": self.screened_moves,
            "accepted_moves": self.accepted_moves,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_entries": self.cache_entries,
            "spec_preferred_sec": PREFERRED_BUDGET_SEC,
            "spec_acceptable_sec": ACCEPTABLE_BUDGET_SEC,
            "spec_preferred_met": self.spec_preferred_met,
            "spec_target_met": self.spec_target_met,
            "accepted_interim_limit_sec": ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
            "accepted_bound_met": self.accepted_bound_met,
        }


@dataclass(frozen=True)
class DatasetMeasurement:
    """One dataset's exhaustive loop plus the determinism evidence of running it twice."""

    label: str
    loop: OptimizerLoopMeasurement
    repeat: OptimizerLoopMeasurement
    evidence: str
    profile: DatasetProfile

    @property
    def deterministic(self) -> bool:
        """Everything except wall-clock time: same work, same routes, same cache statistics."""
        first, second = self.loop, self.repeat
        return (
            first.candidates == second.candidates
            and first.optimizer_runs == second.optimizer_runs
            and first.route_evaluations == second.route_evaluations
            and first.candidates_at_ceiling == second.candidates_at_ceiling
            and first.seed_candidates_priced == second.seed_candidates_priced
            and first.screened_moves == second.screened_moves
            and first.accepted_moves == second.accepted_moves
            and first.cache_hits == second.cache_hits
            and first.cache_misses == second.cache_misses
            and first.cache_entries == second.cache_entries
        )

    @property
    def accepted_bound_met(self) -> bool:
        """Whether the warm loop is inside the bound asserted for its own profile (D34/D36).

        Every profile carries an asserted bound, so no measured scale is unguarded: the **stress**
        fixture and the **demo** plan keep the owner-accepted interim bound of D34, and the
        **portfolio** fixture (the primary MVP scale) is asserted against that same generous
        owner-accepted bound with headroom over its measured ~21-22 s - exceeding it is a genuine
        regression, while its reported v2 section 20 <= 5 s target is printed with its own honest
        verdict and never gates the run (D36). This is the tool's exit-status guard, and it is
        deliberately a regression guard rather than an engineering target.
        """
        if self.profile.asserted_bound_sec is None:
            return True
        return self.loop.total_seconds <= self.profile.asserted_bound_sec

    def as_json(self) -> dict[str, object]:
        return {
            "label": self.label,
            "profile": {
                "kind": self.profile.kind,
                "performance_qualified": self.profile.is_performance_qualified,
                "description": self.profile.description,
                "asserted_bound_sec": self.profile.asserted_bound_sec,
                "asserted_bound_note": self.profile.asserted_bound_note,
                "accepted_bound_met": self.accepted_bound_met,
                "reported_acceptable_target_sec": ACCEPTABLE_BUDGET_SEC,
                "reported_target_met": self.loop.total_seconds <= ACCEPTABLE_BUDGET_SEC,
            },
            "loop": self.loop.as_json(),
            "repeat": self.repeat.as_json(),
            "deterministic_except_wall_clock": self.deterministic,
            "evidence": self.evidence,
        }


def _measure_pass(plan: RoutePlan, matrix, cache: LegCache) -> OptimizerLoopMeasurement:
    """One exhaustive pass over every eligible first-stop candidate, on a shared leg cache.

    What is shared and what is rebuilt, stated exactly (U4 review; corrected claim, not a silent
    one). **Shared across the candidates:** the one :class:`~core.engine.optimizer.cache.LegCache`
    passed in - every leg is priced once, so the measured pass asks the matrix nothing the warm-up
    did not already answer - and the process-wide timezone-resolution memo
    ``core.engine.optimizer.route_problem._resolve_local_cached``. **Rebuilt per candidate:**
    :func:`~core.engine.optimizer.route_problem.build_problem` builds a **fresh**
    :class:`~core.engine.optimizer.route_problem.RouteProblem` for every candidate (a different
    driver-selected first stop is a different problem), so its prepared tables - the frozen
    STOP/FINISH travel and distance snapshots, the per-stop input positions and the precomputed
    service-window table - are prepared again for each candidate. The earlier wording here claimed
    the candidates shared "the base problem's frozen travel table"; they do not, and nothing in the
    loop depends on it (U4 review). The measured cost of that rebuild on the 32-stop demo plan (31
    enabled stops, 1 disabled) is
    documented in :func:`core.engine.first_stop.evaluation.evaluate_first_stop_candidates`.

    That is what a real caller evaluating every candidate does (v2 section 20: the bound applies
    "after the travel matrix already exists").

    The candidate set comes from :func:`candidate_first_stops`, so this pass cannot rank, filter or
    shortlist anything; it only counts how many of those runs had their search truncated by the
    deterministic evaluation ceiling, and reports that number next to the candidate count.
    """
    candidates = candidate_first_stops(plan)
    route_evaluations = 0
    seed_priced = 0
    screened = 0
    accepted = 0
    at_ceiling = 0

    started = timer.perf_counter()
    for candidate in candidates:
        selected = dataclasses.replace(
            plan, first_service_stop=FirstStopIntent.manual_choice(candidate)
        )
        problem = build_problem(
            plan=selected, travel_matrix=matrix, first_stop_id=candidate, cache=cache
        )
        result = optimize(problem)
        route_evaluations += result.local_search.evaluations
        seed_priced += result.evidence.seed_evaluations
        screened += result.evidence.screened_moves
        accepted += len(result.local_search.accepted_moves)
        if result.local_search.budget_exhausted:
            at_ceiling += 1
    total_seconds = timer.perf_counter() - started

    stats: CacheStats = cache.stats
    return OptimizerLoopMeasurement(
        stop_count=len(candidates),
        candidates=len(candidates),
        optimizer_runs=len(candidates),
        route_evaluations=route_evaluations,
        seed_candidates_priced=seed_priced,
        screened_moves=screened,
        accepted_moves=accepted,
        total_seconds=total_seconds,
        warmup_seconds=0.0,
        cache_hits=stats.hits,
        cache_misses=stats.misses,
        cache_entries=stats.entries,
        candidates_at_ceiling=at_ceiling,
    )


def measure_dataset(
    label: str,
    plan: RoutePlan,
    matrix,
    *,
    evidence: str = "",
    repeats: int = 2,
    profile: DatasetProfile = DEMO_PROFILE,
) -> DatasetMeasurement:
    """Measure one dataset's exhaustive first-stop loop, warm, and prove it is repeatable.

    ``profile`` declares which performance target the fixture belongs to (D36), so the same
    measurement can be read under the target that actually applies to it. It defaults to the
    conservative demo profile: a caller that does not say which scale it measured never gets the
    primary MVP target's bound by accident.

    The cache is created once and kept:

    * the **cold** pass runs the whole loop against an empty cache - informative, and never the
      number the bound is judged on;
    * the counters are then reset (the entries stay) and the **measured** pass runs with every leg
      the loop needs already memoized, which is exactly the v2 section 20 condition "after the
      travel matrix already exists";
    * a third **repeat** pass proves that everything except wall-clock time is identical.

    Warming by running the real loop is deliberate: there is no synthetic warm-up that asks for
    legs the loop would not ask for, so the measured pass is genuinely the second time the same
    work is done.
    """
    cache = LegCache(matrix)
    cold = _measure_pass(plan, matrix, cache)
    cache.reset_stats()
    measured = _measure_pass(plan, matrix, cache)
    if repeats > 1:
        # The repeat describes exactly the same phase as the measured pass, not the two of them
        # together, so its deterministic counters can be compared with the measured ones directly.
        cache.reset_stats()
        repeat = _measure_pass(plan, matrix, cache)
    else:
        repeat = measured
    return DatasetMeasurement(
        label=label,
        loop=dataclasses.replace(measured, warmup_seconds=cold.total_seconds),
        repeat=repeat,
        evidence=evidence,
        profile=profile,
    )


def _format_loop(
    title: str, loop: OptimizerLoopMeasurement, profile: DatasetProfile
) -> list[str]:
    spec = "true" if loop.spec_target_met else "false"
    preferred = "true" if loop.spec_preferred_met else "false"
    if profile.asserted_bound_sec is None:
        bound_line = (
            f"  asserted bound                : NONE at this scale - the measured number and the "
            f"reported <= {ACCEPTABLE_BUDGET_SEC:.1f}s acceptable target are printed with their "
            f"honest verdict (REPORTED_TARGET_MET={spec}); {profile.asserted_bound_note}"
        )
    else:
        accepted = "true" if loop.total_seconds <= profile.asserted_bound_sec else "false"
        bound_line = (
            f"  asserted bound                : <= {profile.asserted_bound_sec:.1f}s "
            f"(BOUND_MET={accepted}) - {profile.asserted_bound_note}"
        )
    return [
        f"[{title}]",
        f"  profile                       : {profile.kind.upper()} - "
        f"performance-qualified={str(profile.is_performance_qualified).lower()}",
        f"  scale target                  : {profile.description}",
        f"  stops (enabled)               : {loop.stop_count}",
        f"  candidates evaluated          : {loop.candidates} (complete set: no prefilter, none "
        f"skipped)",
        f"  candidates at the ceiling     : {loop.candidates_at_ceiling} (search truncated by "
        f"max_evaluations, reported not hidden)",
        f"  optimizer runs                : {loop.optimizer_runs}",
        f"  total route evaluations       : {loop.route_evaluations}",
        f"  total wall time (warm)        : {loop.total_seconds:.3f}s",
        f"  per candidate                 : {loop.seconds_per_candidate * 1000:.2f}ms",
        f"  seed candidates priced        : {loop.seed_candidates_priced}",
        f"  screened moves                : {loop.screened_moves}",
        f"  accepted moves                : {loop.accepted_moves}",
        f"  leg cache                     : {loop.cache_hits} hits, {loop.cache_misses} misses, "
        f"{loop.cache_entries} entries",
        f"  spec target (v2 s.20, reported): preferred <= {PREFERRED_BUDGET_SEC:.1f}s "
        f"(met={preferred}), acceptable <= {ACCEPTABLE_BUDGET_SEC:.1f}s (met={spec}) - engineering "
        f"targets, reported; at the portfolio scale they are the PRIMARY MVP target and at the "
        f"stress scale they are not an MVP gate (D34/D36)",
        bound_line,
    ]


def format_report(measurements: Sequence[DatasetMeasurement]) -> str:
    """The whole human-readable report, as one deterministic string (except the seconds)."""
    lines = [
        "RoutePilot optimizer benchmark - exhaustive first-stop candidate evaluation",
        f"Owner scale decision (D36), verbatim: \"{OWNER_SCALE_STATEMENT}\"",
        "Scales measured: the ~30-stop demo plan (DEMO/SYNTHETIC), the ~50-enabled-stop portfolio "
        "fixture (the PRIMARY MVP TARGET, D36) and the ~100-stop stress fixture (ENGINEERING STRESS "
        "REFERENCE, NOT PERFORMANCE-QUALIFIED, D36). Every label states the exact enabled count.",
        "Spec: PRODUCT_SPEC_v2 section 20 (no prefilter; preferred <= 3s, acceptable <= 5s - "
        "engineering targets, reported not asserted); decision D18 (dozens -> ~100 -> 100+ stops); "
        "decision D34 (owner-accepted interim ~100-stop latency, no approximation); decision D36 "
        "(~50 enabled stops is the primary MVP target, ~100 stops is a stress reference only).",
        f"Provenance: scale/stress fixture - {SCALE_WARNING}",
        f"Provenance: portfolio fixture - {portfolio_warning_text()}",
        "Method: two exhaustive passes over one shared leg cache; the first warms every leg, "
        "the second is measured ('after the travel matrix already exists'). Each candidate still "
        "gets its own freshly prepared problem; only the leg cache and the process-wide timezone "
        "memo are shared (see _measure_pass).",
        "Claims kept apart: the candidate set is exhaustive (every enabled stop, no prefilter); "
        "each candidate's search is the full U2 neighbourhood with a deterministic evaluation "
        "ceiling, and the runs it truncated are counted above.",
        "",
    ]
    for measurement in measurements:
        lines.extend(_format_loop(measurement.label, measurement.loop, measurement.profile))
        lines.append(
            f"  repeat run (same work, wall time differs): {measurement.repeat.total_seconds:.3f}s"
        )
        lines.append(
            "  deterministic except wall-clock        : "
            f"{str(measurement.deterministic).lower()}"
        )
        lines.append(
            f"  cold first pass (informative, not the bound) : "
            f"{measurement.loop.warmup_seconds:.3f}s"
        )
        if measurement.evidence:
            lines.append(f"  evidence: {measurement.evidence}")
        lines.append("")
    exit_code = 0 if all(m.accepted_bound_met for m in measurements) else 1
    lines.append(
        f"RESULT: ACCEPTED_BOUND_MET={'true' if exit_code == 0 else 'false'} "
        f"(every fixture inside the generous owner-accepted bound asserted for its own profile, "
        f"the portfolio fixture included; exit {exit_code})"
    )
    lines.append(
        "NOTE: the ~50-stop portfolio fixture is the PRIMARY MVP scale target (D36) and its measured "
        f"number is REPORTED against the v2 section 20 acceptable <= {ACCEPTABLE_BUDGET_SEC:.1f}s "
        "target, which it is not expected to meet - closing that gap needs the deferred "
        "incremental/delta evaluator, not a prefilter, a shortlist or an approximate ranking. What "
        "that scale DOES assert is the same generous owner-accepted regression bound the other "
        f"scales use ({ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.1f}s, D34). The "
        f"~100-stop stress scale asserts that bound too, is labelled NOT performance-qualified, and "
        "is NOT an MVP gate (D36)."
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class BenchmarkDataset:
    """One benchmark fixture: its plans, its printed label, its provenance and its scale profile.

    It is iterable in the historical ``(label, plan, matrix, evidence)`` order so existing callers
    and tests can unpack it exactly as before, while new code can read ``profile`` directly. The
    label is rendered from the plan object's own enabled/total/disabled counts, so it cannot drift
    away from the plan it names.
    """

    label: str
    plan: RoutePlan
    matrix: object
    evidence: str
    profile: DatasetProfile

    def __iter__(self):
        return iter((self.label, self.plan, self.matrix, self.evidence))

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int):
        return (self.label, self.plan, self.matrix, self.evidence)[index]


def _counts_label(name: str, plan: RoutePlan) -> str:
    """``<name>, 50 enabled stops (55 stops, 5 disabled) (DEMO/SYNTHETIC)``.

    Read from the plan object itself, never from a remembered number, so a label can never claim a
    scale the plan does not have (the U5 label defect, and D36's rule that a fixture with materially
    fewer enabled stops is never called a "50-stop" fixture).
    """
    enabled = len(plan.active_stops())
    total = len(plan.stops)
    disabled = len(plan.disabled_stops())
    return (
        f"{name}, {enabled} enabled stops ({total} stops, {disabled} disabled) (DEMO/SYNTHETIC)"
    )


def _datasets(
    stop_count: int,
    *,
    include_scale: bool,
    include_demo: bool,
    include_portfolio: bool = True,
):
    """The benchmark's datasets: portfolio fixture, stress fixture and/or the ~30-stop demo plan.

    The portfolio fixture is the primary MVP scale target of D36 and is measured by default
    alongside the ~100-stop stress reference; the demo plan's counts are read from the demo plan
    object itself, in the same "31 enabled stops (32 stops, 1 disabled)" shape the demo report
    prints, so a printed label cannot drift away from the plan it names (see
    :mod:`tests.tools.test_benchmark_optimizer_labels`).
    """
    if include_portfolio:
        plan = build_portfolio_plan()
        yield BenchmarkDataset(
            label=_counts_label("portfolio fixture (PRIMARY MVP TARGET, D36)", plan),
            plan=plan,
            matrix=demo_matrix(),
            evidence=(
                "deterministic ~50-enabled-stop portfolio fixture of demo/scale_dataset.py "
                f"(build_portfolio_plan: {PORTFOLIO_STOP_COUNT} stops, "
                f"{PORTFOLIO_ENABLED_STOP_COUNT} enabled by the fixture's deterministic disabled "
                "policy) - the PRIMARY MVP scale target of D36: its measured number is reported "
                "honestly against the v2 section 20 targets and guarded by the same generous "
                f"owner-accepted regression bound ({ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.0f}s, D34) as "
                "the other scales"
            ),
            profile=PORTFOLIO_PROFILE,
        )
    if include_scale:
        plan = build_scale_plan(stop_count)
        yield BenchmarkDataset(
            label=_counts_label(
                f"stress fixture, {stop_count} stops (NOT performance-qualified, D36)", plan
            ),
            plan=plan,
            matrix=demo_matrix(),
            evidence=(
                "deterministic synthetic stress fixture of demo/scale_dataset.py - future scale / "
                "engineering stress reference, not performance-qualified under D36 (owner decision: "
                "failure to meet <= 5s at 100 stops does not block the portfolio MVP)"
            ),
            profile=scale_profile_for(stop_count),
        )
    if include_demo:
        demo_plan = build_demo_plan()
        # The label is rendered from the plan object's own counts (enabled / total / disabled) in
        # the same shape the demo report prints, so it cannot disagree with the plan it names. The
        # plan's scale is the approximate "~30 stops" the spec vocabulary uses, but the printed
        # count is exact.
        yield BenchmarkDataset(
            label=_counts_label("demo plan", demo_plan),
            plan=demo_plan,
            matrix=demo_matrix(),
            evidence="deterministic demo plan of demo/dataset.py",
            profile=DEMO_PROFILE,
        )


def check_timezone_data(zone: str = "Europe/Moscow") -> str | None:
    """Return a one-line reason the benchmark cannot run, or ``None`` when it can.

    Both benchmark plans declare an IANA time zone (D2), so the run needs a real database. This
    is a report, never a silent fix: production code does not fabricate a time zone source, and
    the benchmark is a development tool, not a place to start doing it (D12).
    """
    report = tzdata.probe_tzdata()
    if not report.is_available:
        return (
            "no IANA time zone database is available, so the benchmark cannot build its plans. "
            f"Install it with: {TZDATA_INSTALL_COMMAND} - or, on a development machine that has a "
            "TZif tree, set PYTHONTZPATH to it."
        )
    try:
        tzdata.load_timezone(zone)
    except Exception as error:  # noqa: BLE001 - the tool reports, it does not raise
        return f"cannot load the required zone {zone!r}: {type(error).__name__}: {error}"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stop-count",
        type=int,
        default=SCALE_DEFAULT_STOP_COUNT,
        help=(
            "service stops of the stress fixture (default: 100, the v2 section 20 scale, which D36 "
            "declares an engineering stress reference rather than an MVP gate)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="also print a JSON record")
    parser.add_argument("--no-scale", action="store_true", help="skip the stress fixture")
    parser.add_argument(
        "--no-portfolio",
        action="store_true",
        help="skip the ~50-enabled-stop portfolio fixture (the primary MVP target, D36)",
    )
    parser.add_argument("--no-demo", action="store_true", help="skip the ~30-stop demo plan")
    args = parser.parse_args(argv)

    blocked = check_timezone_data()
    if blocked is not None:
        print(f"benchmark cannot run: {blocked}", file=sys.stderr)
        return 2

    measurements = [
        measure_dataset(
            dataset.label,
            dataset.plan,
            dataset.matrix,
            evidence=dataset.evidence,
            profile=dataset.profile,
        )
        for dataset in _datasets(
            args.stop_count,
            include_scale=not args.no_scale,
            include_portfolio=not args.no_portfolio,
            include_demo=not args.no_demo,
        )
    ]
    if not measurements:
        parser.error("nothing to measure: --no-scale, --no-portfolio and --no-demo were all given")

    print(format_report(measurements))
    if args.json:
        print()
        print(
            json.dumps(
                {
                    "owner_scale_statement": OWNER_SCALE_STATEMENT,
                    "spec_targets": {
                        "preferred_sec": PREFERRED_BUDGET_SEC,
                        "acceptable_sec": ACCEPTABLE_BUDGET_SEC,
                        "asserted": False,
                        "note": (
                            "v2 section 20 engineering targets; reported, not asserted at the "
                            "~100-stop stress scale (D34/D36). They are the primary MVP target's "
                            "reported targets at the ~50-stop portfolio scale (D36)."
                        ),
                    },
                    "accepted_interim_bound": {
                        "sec": ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
                        "asserted": True,
                        "note": (
                            "owner-accepted latency with headroom; decision D34. It is the asserted "
                            "regression guard for every measured scale - the ~50-stop portfolio "
                            "fixture included - and it is not an MVP performance gate (D36)."
                        ),
                    },
                    "datasets": [measurement.as_json() for measurement in measurements],
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0 if all(measurement.accepted_bound_met for measurement in measurements) else 1


if __name__ == "__main__":
    raise SystemExit(main())
