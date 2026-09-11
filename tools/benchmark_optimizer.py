#!/usr/bin/env python
"""Deterministic benchmark of exhaustive first-stop candidate evaluation (v2 sections 19, 20, D18).

What it measures
----------------

The Stage 2 reference requirement of v2 section 20: *evaluate the complete route for every
feasible first-stop candidate*, with no fixed-K prefilter. For a plan of ``n`` enabled stops that
means ``n`` optimizer runs - one per candidate, each optimizing ``START -> candidate -> the
remaining stops -> FINISH`` - and the time bound applies **"after the travel matrix already
exists"**.

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

For the ~100-stop synthetic fixture (:mod:`demo.scale_dataset`) and for the ~30-stop demo plan
(:mod:`demo.dataset`): stop count, candidates evaluated, **candidates whose search hit the
deterministic evaluation ceiling**, optimizer runs, total and per-candidate wall time, total route
evaluations, accepted moves, leg-cache hits/misses/entries, the v2 section 20 spec target
(**reported, never asserted**) and the owner-accepted interim bound.

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

**0** when the measured (warm) exhaustive loop is inside the **owner-accepted interim bound**
(:data:`ACCEPTED_INTERIM_LOOP_LIMIT_SEC`, decision D34), and **1** when it exceeds it: that bound is
the regression guard, and it is the only timing this tool asserts. The v2 section 20 targets
(preferred <= 3 s, acceptable <= 5 s) are printed as *reported* engineering targets and are **not**
asserted - v2 section 20 calls them "engineering targets, not correctness rules", and the owner
accepted the ~100-stop latency as an explicit interim limitation (D34) while the full neighbourhood
and the search quality stay.

Determinism
-----------

Everything except wall-clock time is deterministic: the plans, the candidate order, the produced
routes, the route-evaluation counts, the candidates-at-ceiling count, the accepted-move counts and
the cache statistics are identical on every run and on every machine. Only the seconds move.
``--json`` prints a machine readable record of both datasets.

Usage::

    python tools/benchmark_optimizer.py
    python tools/benchmark_optimizer.py --stop-count 100 --json
    python tools/benchmark_optimizer.py --stop-count 60 --no-scale
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
from demo.scale_dataset import SCALE_WARNING, build_scale_plan  # noqa: E402
from demo.synthetic_matrix import demo_matrix  # noqa: E402

__all__ = [
    "ACCEPTABLE_BUDGET_SEC",
    "ACCEPTED_INTERIM_LOOP_LIMIT_SEC",
    "PREFERRED_BUDGET_SEC",
    "DatasetMeasurement",
    "OptimizerLoopMeasurement",
    "candidate_first_stops",
    "check_timezone_data",
    "format_report",
    "main",
    "measure_dataset",
]

#: v2 section 20: "preferred: <= approximately 3 seconds" for ~100 service stops. **Reported, never
#: asserted**: see :data:`ACCEPTED_INTERIM_LOOP_LIMIT_SEC` and decision D34.
PREFERRED_BUDGET_SEC = 3.0

#: v2 section 20: "acceptable for the early product: <= approximately 5 seconds". **Reported, never
#: asserted**: the owner accepted the measured ~100-stop latency as an explicit interim limitation
#: (D34), and v2 section 20 calls these numbers engineering targets, not correctness rules.
ACCEPTABLE_BUDGET_SEC = 5.0

#: The **owner-accepted** bound of the whole exhaustive first-stop loop, in seconds (decision D34).
#: The owner accepted the measured warm ~63-76 s at 97 enabled stops as an interim limitation; this
#: constant is that figure with headroom for a slower machine (about twice the worst accepted
#: measurement), so exceeding it is a real regression rather than machine noise. It is the single
#: timing this tool asserts, and it is what the exit status reports. It is deliberately **not** the
#: v2 section 20 <= 5 s target: the owner chose the full U2 neighbourhood and the restored search
#: quality over that time target, and a dedicated later Stage 2 unit will remove the latency with an
#: incremental / delta complete-route evaluator.
ACCEPTED_INTERIM_LOOP_LIMIT_SEC = 150.0


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

    def as_json(self) -> dict[str, object]:
        return {
            "label": self.label,
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
) -> DatasetMeasurement:
    """Measure one dataset's exhaustive first-stop loop, warm, and prove it is repeatable.

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
    )


def _format_loop(title: str, loop: OptimizerLoopMeasurement) -> list[str]:
    accepted = "true" if loop.accepted_bound_met else "false"
    spec = "true" if loop.spec_target_met else "false"
    preferred = "true" if loop.spec_preferred_met else "false"
    return [
        f"[{title}]",
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
        f"(met={preferred}), acceptable <= {ACCEPTABLE_BUDGET_SEC:.1f}s (met={spec}) - not asserted, "
        f"owner decision D34",
        f"  owner-accepted interim bound  : <= {ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.1f}s "
        f"(ACCEPTED_BOUND_MET={accepted}) - the asserted guard",
    ]


def format_report(measurements: Sequence[DatasetMeasurement]) -> str:
    """The whole human-readable report, as one deterministic string (except the seconds)."""
    lines = [
        "RoutePilot optimizer benchmark - exhaustive first-stop candidate evaluation",
        "Spec: PRODUCT_SPEC_v2 section 20 (no prefilter; ~100 stops; preferred <= 3s, acceptable "
        "<= 5s - engineering targets, reported not asserted); decision D18 (dozens -> ~100 -> 100+ "
        "stops); decision D34 (owner-accepted interim ~100-stop latency, no approximation).",
        f"Provenance: {SCALE_WARNING}",
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
        lines.extend(_format_loop(measurement.label, measurement.loop))
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
    exit_code = 0 if all(m.loop.accepted_bound_met for m in measurements) else 1
    lines.append(
        f"RESULT: ACCEPTED_BOUND_MET={'true' if exit_code == 0 else 'false'} "
        f"(owner-accepted interim bound {ACCEPTED_INTERIM_LOOP_LIMIT_SEC:.1f}s; exit {exit_code})"
    )
    return "\n".join(lines)


def _datasets(stop_count: int, *, include_scale: bool, include_demo: bool):
    """The benchmark's datasets: the scale fixture and/or the ~30-stop demo plan.

    The demo label's counts are read from the demo plan object itself, in the same
    "31 enabled stops (32 stops, 1 disabled)" shape the demo report prints, so the printed label
    cannot drift away from the plan it names (see
    :mod:`tests.tools.test_benchmark_optimizer_labels`).
    """
    if include_scale:
        yield (
            f"scale fixture, {stop_count} stops (DEMO/SYNTHETIC)",
            build_scale_plan(stop_count),
            demo_matrix(),
            "deterministic synthetic benchmark fixture of demo/scale_dataset.py",
        )
    if include_demo:
        demo_plan = build_demo_plan()
        # The label is rendered from the plan object's own counts (enabled / total / disabled) in
        # the same shape the demo report prints, so it cannot disagree with the plan it names. The
        # plan's scale is the approximate "~30 stops" the spec vocabulary uses, but the printed
        # count is exact.
        enabled = len(demo_plan.active_stops())
        total = len(demo_plan.stops)
        disabled = len(demo_plan.disabled_stops())
        yield (
            f"demo plan, {enabled} enabled stops ({total} stops, {disabled} disabled) "
            "(DEMO/SYNTHETIC)",
            demo_plan,
            demo_matrix(),
            "deterministic demo plan of demo/dataset.py",
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
        default=100,
        help="service stops of the scale fixture (default: 100, the v2 section 20 target)",
    )
    parser.add_argument("--json", action="store_true", help="also print a JSON record")
    parser.add_argument("--no-scale", action="store_true", help="skip the scale fixture")
    parser.add_argument("--no-demo", action="store_true", help="skip the ~30-stop demo plan")
    args = parser.parse_args(argv)

    blocked = check_timezone_data()
    if blocked is not None:
        print(f"benchmark cannot run: {blocked}", file=sys.stderr)
        return 2

    measurements = [
        measure_dataset(label, plan, matrix, evidence=evidence)
        for label, plan, matrix, evidence in _datasets(
            args.stop_count,
            include_scale=not args.no_scale,
            include_demo=not args.no_demo,
        )
    ]
    if not measurements:
        parser.error("nothing to measure: --no-scale and --no-demo were both given")

    print(format_report(measurements))
    if args.json:
        print()
        print(
            json.dumps(
                {
                    "spec_targets": {
                        "preferred_sec": PREFERRED_BUDGET_SEC,
                        "acceptable_sec": ACCEPTABLE_BUDGET_SEC,
                        "asserted": False,
                        "note": "v2 section 20 engineering targets; reported, not asserted (D34)",
                    },
                    "accepted_interim_bound": {
                        "sec": ACCEPTED_INTERIM_LOOP_LIMIT_SEC,
                        "asserted": True,
                        "note": "owner-accepted ~100-stop latency with headroom; decision D34",
                    },
                    "datasets": [measurement.as_json() for measurement in measurements],
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0 if all(measurement.loop.accepted_bound_met for measurement in measurements) else 1


if __name__ == "__main__":
    raise SystemExit(main())
