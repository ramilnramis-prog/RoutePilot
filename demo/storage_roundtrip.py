"""End-to-end storage round-trip demo (Stage 3 U12; D38; Product Spec v2 sections 26, 36).

Run it with::

    python -m demo.storage_roundtrip
    python -m demo.storage_roundtrip --allow-system-tzdata   # offline machine, see below

What this demo is
-----------------

One deterministic pass over the **real** engine and the **real** storage adapters, on the shipped
DEMO/SYNTHETIC demo plan and an in-memory database. Nothing here is fabricated: the numbers are the
engine's own, and the round trips are the shipped repositories'. It proves, in this order:

1. the shipped demo plan is saved through
   :class:`storage.sqlite.route_plan_repository.SqliteRoutePlanRepository` into a fresh migrated
   database and reloaded as an **exactly equal** :class:`~core.model.route_plan.RoutePlan`;
2. the reloaded plan is the same route input as the in-memory original - the **strict DST after
   load** evidence of D3: the real exhaustive first-stop evaluation and the real optimizer run
   twice, on the original plan and on the reloaded plan, and every printed field (inputs
   fingerprint, route fingerprint, order, complete duration, travel, waiting, service and
   violations) is identical;
3. a real optimization run derived from that solution - the order, the three
   :class:`~core.model.solution.RouteMetrics` baselines, the violations and the recommendation
   payload the run showed - is appended to ``route_optimization_runs`` with a **fixed**
   ``created_at_utc`` and read back exactly through ``list_for_plan`` and ``latest``;
4. two ``app_settings`` entries are written and read back;
5. the D38 open-question-2 decision is stated and demonstrated: **complete derived timelines are
   never persisted**; only the authoritative reproducibility metadata (inputs fingerprint, route
   fingerprint, ``cost_policy_json``, the timezone/tzdata metadata and the stored route metrics) is,
   and the timelines/metrics recomputed from the reloaded plan match the original.

The database is ``:memory:`` and the demo **creates no database file**: a committed database file is
forbidden by Product Spec v2 section 37 and by D38, and there is deliberately no path option that
could produce one. The demo does not commit anything anywhere; it opens a fresh in-memory database
per run, so two runs print byte-identical output (no wall clock, no machine-specific path, and every
storage timestamp is pinned). It measures no latency and changes no engine record: the ~50-stop
scale limitation of D36/D37 is unchanged.

A recommendation is not a driver decision (D4/D11/D32): the stored plan keeps its shipped
``awaiting_first_stop_choice`` state, and the route this demo commits is computed on an **in-memory
copy** that carries the accepted recommendation. Nothing about the recommendation is ever written as
plan state.

A time zone database is required. On an offline machine where ``tzdata`` cannot be installed, the
standard ``PYTHONTZPATH`` mechanism works, or ``--allow-system-tzdata`` activates a discovered system
TZif tree explicitly and warns on stderr - never silently (D12).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.engine.first_stop.evaluation import (
    FirstStopEvaluationReport,
    evaluate_first_stop_candidates,
)
from core.engine.optimizer.route_fingerprint import route_fingerprint
from core.engine.optimizer.solve import solve_route
from core.engine.providers import TravelMatrix
from core.model.first_stop import FirstStopIntent
from core.model.ids import StopId
from core.model.optimization_run import (
    ALGORITHM_NAME,
    ALGORITHM_VERSION,
    OptimizationRun,
    OptimizationRunMetrics,
    OptimizationRunRecommendation,
    RunKind,
    RunStatus,
    run_metrics_from_solution,
)
from core.model.route_plan import RoutePlan
from core.model.solution import RouteMetrics, RouteSolution, StopTimeline, Violation
from core.model.value_objects import DataProvenance
from core.time import tzdata
from demo.dataset import DEMO_WARNING, build_demo_plan
from demo.synthetic_matrix import demo_matrix
from storage.sqlite.app_settings_repository import SqliteAppSettingsRepository
from storage.sqlite.database import connect, current_version, migrate
from storage.sqlite.optimization_run_repository import SqliteRouteOptimizationRunRepository
from storage.sqlite.route_plan_repository import SqliteRoutePlanRepository

__all__ = [
    "DEMO_CREATED_AT_UTC",
    "DEMO_TOP_K",
    "SETTINGS_ENTRIES",
    "RouteSnapshot",
    "RoundTripResult",
    "format_metrics",
    "main",
    "open_demo_database",
    "render_report",
    "run_round_trip",
]

#: The demo's own storage clock and the run's instant. Fixed so every stored timestamp and every
#: printed figure is reproducible: the demo never reads a wall clock.
DEMO_CREATED_AT_UTC = datetime(2026, 9, 11, 6, 0, 0, tzinfo=timezone.utc)

#: The run id of the appended run. A literal because run ids are uuids in production and a
#: deterministic demo needs a deterministic row.
DEMO_RUN_ID = "demo-run-0001"

#: How many ranked candidates the stored top-K payload keeps: the same head the demo report shows.
DEMO_TOP_K = 3

#: The two ``app_settings`` entries written and read back, in the order they are reported. Both are
#: documented intended keys of the approved schema (section 6); the repository owns their storage,
#: never their meaning.
SETTINGS_ENTRIES: tuple[tuple[str, Any], ...] = (
    ("default_timezone", "Europe/Moscow"),
    ("default_data_provenance", "DEMO_SYNTHETIC"),
)

_SEPARATOR = "=" * 100
_ORDER_WRAP = 96


def _fixed_clock() -> datetime:
    """The repositories' clock: one pinned instant, so no stored stamp reads a wall clock."""
    return DEMO_CREATED_AT_UTC


def format_metres(metres: float) -> str:
    """``660450`` -> ``660450 m (660.4 km)``; deterministic and label-free."""
    return f"{metres:.0f} m ({metres / 1000.0:.1f} km)"


def format_seconds(seconds: int) -> str:
    """``38940`` -> ``38940 s (10h49m)``."""
    hours, remainder = divmod(int(seconds), 3600)
    return f"{int(seconds)} s ({hours}h{remainder // 60:02d}m)"


def format_metrics(label: str, metrics: RouteMetrics) -> str:
    """One stored route of the run, with every component the schema's ``metrics_json`` carries."""
    return (
        f"{label}: {format_metres(metrics.distance_m)}, duration {format_seconds(metrics.duration_sec)}, "
        f"travel {metrics.travel_sec}s, waiting {metrics.waiting_sec}s, service {metrics.service_sec}s, "
        f"finish {metrics.finish_arrival.strftime('%Y-%m-%dT%H:%M:%SZ')}, "
        f"feasible {metrics.feasible}, baseline "
        f"{metrics.baseline_kind.value if metrics.baseline_kind else 'none'}"
    )


def format_order(order: Sequence[StopId], *, indent: str = "    ", wrap: int = _ORDER_WRAP) -> str:
    """The committed service order, wrapped, one deterministic text for any number of stops."""
    lines: list[str] = []
    current = indent
    for index, stop_id in enumerate(order):
        piece = str(stop_id) if index == 0 else f" -> {stop_id}"
        if current.strip() and len(current) + len(piece) > wrap:
            lines.append(current)
            current = indent + "   "
            piece = piece[4:]
        current += piece
    if current.strip():
        lines.append(current)
    return "\n".join(lines)


@dataclass(frozen=True)
class RouteSnapshot:
    """The complete-route result of one engine pass, as printed next to the other pass.

    ``label`` is presentation only and is excluded from equality, so two snapshots compare equal
    exactly when every engine-produced field is equal - including the whole order and every
    timeline, not just a digest of them.
    """

    label: str = field(compare=False)
    inputs_fingerprint: str
    route_fingerprint: str
    order: tuple[StopId, ...]
    metrics: RouteMetrics
    user_baseline: RouteMetrics
    algorithm_baseline: RouteMetrics
    violations: tuple[Violation, ...]
    timelines: tuple[StopTimeline, ...]


@dataclass(frozen=True)
class RoundTripResult:
    """Everything one demo run did, as data, so the text renderer and the tests share it."""

    plan_id: str
    schema_version: int
    tables: tuple[str, ...]
    plan_equal: bool
    stop_rows: int
    enabled_count: int
    disabled_count: int
    plan_first_stop_state: str
    start_label: str
    finish_label: str
    start_and_finish_are_stop_rows: bool
    recommendation_status: str
    recommended_stop_id: StopId | None
    ranked_ids: tuple[StopId, ...]
    rejected_ids: tuple[StopId, ...]
    original: RouteSnapshot
    reloaded: RouteSnapshot
    run: OptimizationRun
    loaded_runs: tuple[OptimizationRun, ...]
    latest_run: OptimizationRun | None
    settings_before: tuple[Any, ...]
    settings_after: tuple[Any, ...]
    tzdata_version: str | None

    @property
    def routes_identical(self) -> bool:
        """Whether the engine pass on the reloaded plan reproduced the original exactly."""
        return self.original == self.reloaded

    @property
    def run_round_trip_equal(self) -> bool:
        """Whether history read back is exactly the appended run, through both read methods."""
        return self.loaded_runs == (self.run,) and self.latest_run == self.run

    @property
    def history_count(self) -> int:
        return len(self.loaded_runs)

    @property
    def settings_round_trip_ok(self) -> bool:
        """Whether every setting was absent, then stored, then read back unchanged."""
        return all(
            before is None and after == expected
            for before, after, (_, expected) in zip(
                self.settings_before, self.settings_after, SETTINGS_ENTRIES
            )
        )


def open_demo_database() -> sqlite3.Connection:
    """A fresh in-memory database migrated to the approved schema; no file is ever created."""
    connection = connect(":memory:")
    migrate(connection)
    return connection


def run_round_trip(connection: sqlite3.Connection | None = None) -> RoundTripResult:
    """Run the whole demo round trip against the real engine and the real repositories.

    Args:
        connection: an optional **already migrated** connection the caller owns. Defaults to a
            fresh in-memory database opened and closed here, so the demo creates no database file.
    """
    if connection is not None:
        return _round_trip(connection)
    owned = open_demo_database()
    try:
        return _round_trip(owned)
    finally:
        owned.close()


def _round_trip(connection: sqlite3.Connection) -> RoundTripResult:
    plan = build_demo_plan()
    plans = SqliteRoutePlanRepository(
        connection,
        data_provenance=DataProvenance.DEMO_SYNTHETIC,
        clock=_fixed_clock,
    )
    plans.save(plan)
    reloaded = plans.get(plan.id)
    if reloaded is None:
        raise AssertionError(f"the stored plan {plan.id!r} could not be read back")

    stop_rows = connection.execute(
        "SELECT COUNT(*) FROM route_stops WHERE plan_id = ?", (str(plan.id),)
    ).fetchone()[0]
    stop_ids = {
        row[0]
        for row in connection.execute(
            "SELECT id FROM route_stops WHERE plan_id = ?", (str(plan.id),)
        )
    }
    labels = {plan.departure.label, plan.finish.label}

    matrix = demo_matrix()
    original_report = evaluate_first_stop_candidates(plan=plan, travel_matrix=matrix)
    reloaded_report = evaluate_first_stop_candidates(plan=reloaded, travel_matrix=matrix)
    _require_recommendation(original_report, "the in-memory demo plan")
    _require_recommendation(reloaded_report, "the reloaded demo plan")
    if original_report != reloaded_report:
        raise AssertionError("the reloaded plan produced a different recommendation")

    original, original_solution = _route_snapshot("original", plan, original_report, matrix)
    loaded, _ = _route_snapshot("reloaded", reloaded, reloaded_report, matrix)

    run = _optimization_run(
        plan=reloaded,
        solution=original_solution,
        report=original_report,
        route_fingerprint_value=original.route_fingerprint,
    )
    runs = SqliteRouteOptimizationRunRepository(connection)
    runs.append(run)
    loaded_runs = runs.list_for_plan(plan.id)
    latest_run = runs.latest(plan.id)

    settings = SqliteAppSettingsRepository(connection, clock=_fixed_clock)
    settings_before = tuple(settings.get(key) for key, _ in SETTINGS_ENTRIES)
    for key, value in SETTINGS_ENTRIES:
        settings.set(key, value)
    settings_after = tuple(settings.get(key) for key, _ in SETTINGS_ENTRIES)

    tables = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    )

    return RoundTripResult(
        plan_id=str(plan.id),
        schema_version=current_version(connection),
        tables=tables,
        plan_equal=reloaded == plan,
        stop_rows=int(stop_rows),
        enabled_count=len(reloaded.active_stops()),
        disabled_count=len(reloaded.disabled_stops()),
        plan_first_stop_state=reloaded.first_stop_state.value,
        start_label=reloaded.departure.label,
        finish_label=reloaded.finish.label,
        start_and_finish_are_stop_rows=bool(labels & stop_ids),
        recommendation_status=original_report.status.value,
        recommended_stop_id=original_report.recommended_stop_id,
        ranked_ids=original_report.ranked_ids(),
        rejected_ids=original_report.rejected_ids(),
        original=original,
        reloaded=loaded,
        run=run,
        loaded_runs=loaded_runs,
        latest_run=latest_run,
        settings_before=settings_before,
        settings_after=settings_after,
        tzdata_version=tzdata.tzdata_version(),
    )


def _require_recommendation(report: FirstStopEvaluationReport, what: str) -> None:
    """The demo is about the round trip, so a plan without a recommendation is a fixture failure."""
    if report.recommended_stop_id is None:
        raise AssertionError(
            f"{what} produced no first-stop recommendation (status {report.status.value!r}); the "
            "demo plan is expected to recommend one"
        )


def _route_snapshot(
    label: str,
    plan: RoutePlan,
    report: FirstStopEvaluationReport,
    matrix: TravelMatrix,
) -> tuple[RouteSnapshot, RouteSolution]:
    """One real engine pass: accept the recommendation on a copy, optimize, commit, snapshot.

    The accepted recommendation is applied to an in-memory ``dataclasses.replace`` copy only, so the
    stored plan keeps its ``awaiting_first_stop_choice`` state (D4/D11/D32).
    """
    selected = dataclasses.replace(
        plan, first_service_stop=FirstStopIntent.accepted_recommendation(report.recommended_stop_id)
    )
    solution = solve_route(plan=selected, travel_matrix=matrix)
    snapshot = RouteSnapshot(
        label=label,
        inputs_fingerprint=solution.inputs_fingerprint,
        route_fingerprint=route_fingerprint(selected, solution.order),
        order=solution.order,
        metrics=solution.metrics,
        user_baseline=_require_baseline(solution.user_baseline, "user_baseline"),
        algorithm_baseline=_require_baseline(solution.algorithm_baseline, "algorithm_baseline"),
        violations=solution.violations,
        timelines=solution.timelines,
    )
    return snapshot, solution


def _require_baseline(metrics: RouteMetrics | None, name: str) -> RouteMetrics:
    if metrics is None:
        raise AssertionError(f"the committed solution carries no {name}; the run needs both (D22)")
    return metrics


def _optimization_run(
    *,
    plan: RoutePlan,
    solution: RouteSolution,
    report: FirstStopEvaluationReport,
    route_fingerprint_value: str,
) -> OptimizationRun:
    """The real run of that solution, stored with a pinned instant so the output is reproducible."""
    return OptimizationRun(
        id=DEMO_RUN_ID,
        plan_id=plan.id,
        run_kind=RunKind.OPTIMIZE,
        algorithm=ALGORITHM_NAME,
        algorithm_version=ALGORITHM_VERSION,
        inputs_fingerprint=solution.inputs_fingerprint,
        route_fingerprint=route_fingerprint_value,
        cost_policy=plan.cost_policy,
        data_provenance=DataProvenance.DEMO_SYNTHETIC,
        status=RunStatus.OK if not solution.violations else RunStatus.HAS_INFEASIBLE_WINDOWS,
        order=solution.order,
        recommendation=OptimizationRunRecommendation.of(report.to_recommendation()),
        violations=solution.violations,
        metrics=run_metrics_from_solution(solution),
        created_at_utc=DEMO_CREATED_AT_UTC,
        tzdata_version=tzdata.tzdata_version(),
        top_k=report.top(DEMO_TOP_K),
    )


# --------------------------------------------------------------------------- #
# rendering: deterministic text, no wall clock and no machine-specific path
# --------------------------------------------------------------------------- #
def render_report(result: RoundTripResult) -> str:
    """The whole demo as one deterministic block of text."""
    lines: list[str] = []
    lines.append(_SEPARATOR)
    lines.append("RoutePilot storage round-trip demo (Stage 3 U12; D38; spec v2 sections 26 and 36)")
    lines.append(DEMO_WARNING)
    lines.append(_SEPARATOR)
    lines.append("")
    lines.extend(_plan_lines(result))
    lines.extend(_recommendation_lines(result))
    lines.extend(_comparison_lines(result))
    lines.extend(_run_lines(result))
    lines.extend(_settings_lines(result))
    lines.extend(_recompute_lines(result))
    lines.extend(_summary_lines(result))
    return "\n".join(lines)


def _plan_lines(result: RoundTripResult) -> list[str]:
    return [
        "[1] PLAN ROUND TRIP (route_plans + route_stops)",
        f"    database                : :memory: (in-memory SQLite; no database file is created)",
        f"    schema version          : {result.schema_version}",
        f"    tables                  : {', '.join(result.tables)}",
        f"    plan id                 : {result.plan_id}",
        f"    saved plan == reloaded  : {result.plan_equal}",
        f"    stops                   : {result.stop_rows} rows "
        f"({result.enabled_count} enabled + {result.disabled_count} disabled)",
        f"    START                   : {result.start_label!r} - a plan location (PlaceRef), "
        "never a stop row",
        f"    FINISH                  : {result.finish_label!r} - a plan location (PlaceRef), "
        "never a stop row",
        f"    START/FINISH as stops   : {result.start_and_finish_are_stop_rows}",
        f"    stored first-stop state : {result.plan_first_stop_state} "
        "(the recommendation is never plan state, D32)",
        "",
    ]


def _recommendation_lines(result: RoundTripResult) -> list[str]:
    top = ", ".join(str(stop_id) for stop_id in result.ranked_ids[:DEMO_TOP_K])
    rejected = ", ".join(str(stop_id) for stop_id in result.rejected_ids) or "-"
    return [
        "[2] RECOMMENDATION (the real engine, exhaustive complete-route evaluation)",
        f"    status                  : {result.recommendation_status}",
        f"    recommended stop        : {result.recommended_stop_id}",
        f"    ranked candidates       : {len(result.ranked_ids)} (top {DEMO_TOP_K}: {top})",
        f"    rejected candidates     : {len(result.rejected_ids)} ({rejected})",
        "",
    ]


def _comparison_lines(result: RoundTripResult) -> list[str]:
    original, loaded = result.original, result.reloaded
    rows = (
        ("inputs fingerprint", original.inputs_fingerprint, loaded.inputs_fingerprint),
        ("route fingerprint", original.route_fingerprint, loaded.route_fingerprint),
        (
            "complete duration",
            format_seconds(original.metrics.duration_sec),
            format_seconds(loaded.metrics.duration_sec),
        ),
        ("travel", f"{original.metrics.travel_sec} s", f"{loaded.metrics.travel_sec} s"),
        ("waiting", f"{original.metrics.waiting_sec} s", f"{loaded.metrics.waiting_sec} s"),
        ("service", f"{original.metrics.service_sec} s", f"{loaded.metrics.service_sec} s"),
        (
            "violations",
            _violations_text(original.violations),
            _violations_text(loaded.violations),
        ),
        ("order length", f"{len(original.order)} stops", f"{len(loaded.order)} stops"),
    )
    lines = [
        "[3] STRICT-DST-AFTER-LOAD EVIDENCE (D3): the engine run twice, in memory and after reload",
    ]
    for name, in_memory, after_load in rows:
        lines.append(f"    {name:<22} original : {in_memory}")
        lines.append(f"    {'':<22} reloaded : {after_load}")
    lines.append("")
    lines.append("    route order (original):")
    lines.append(format_order(original.order))
    lines.append("    route order (reloaded):")
    lines.append(format_order(loaded.order))
    lines.append("")
    lines.append(
        "    IDENTICAL: "
        f"{result.routes_identical} - both passes produced the same inputs fingerprint, route "
        "fingerprint, order and complete-route result"
    )
    lines.append("")
    return lines


def _violations_text(violations: Sequence[Violation]) -> str:
    if not violations:
        return "0"
    return f"{len(violations)}: " + ", ".join(
        f"{violation.stop_id}/{violation.kind.value}" for violation in violations
    )


def _run_lines(result: RoundTripResult) -> list[str]:
    run = result.run
    metrics: OptimizationRunMetrics = run.metrics
    return [
        "[4] RUN HISTORY (route_optimization_runs, append -> list_for_plan/latest)",
        f"    run id                  : {run.id} ({run.run_kind.value}, created_at_utc "
        f"{run.created_at_utc.strftime('%Y-%m-%dT%H:%M:%SZ')})",
        f"    algorithm               : {run.algorithm} {run.algorithm_version}",
        f"    tzdata metadata         : {run.tzdata_version}",
        f"    stored metrics          : {format_metrics('user_baseline', metrics.user_baseline)}",
        f"                              "
        f"{format_metrics('algorithm_baseline', metrics.algorithm_baseline)}",
        f"                              {format_metrics('after', metrics.after)}",
        f"    stored saving           : {metrics.saved_distance_m:.0f} m / "
        f"{metrics.saved_duration_sec} s",
        f"    stored recommendation   : {run.recommendation.describe()}",
        f"    stored top-K candidates : {len(run.top_k or ())}",
        f"    stored violations       : {_violations_text(run.violations)}",
        f"    history count           : {result.history_count}",
        f"    loaded run == appended  : {result.run_round_trip_equal} "
        "(list_for_plan and latest agree)",
        "",
    ]


def _settings_lines(result: RoundTripResult) -> list[str]:
    lines = ["[5] APP SETTINGS (app_settings, set -> get)"]
    for (key, _), before, after in zip(
        SETTINGS_ENTRIES, result.settings_before, result.settings_after
    ):
        lines.append(
            f"    {key:<24}: before {json.dumps(before)} -> "
            f"after {json.dumps(after, sort_keys=True)}"
        )
    lines.append(f"    settings round trip     : {result.settings_round_trip_ok}")
    lines.append("")
    return lines


def _recompute_lines(result: RoundTripResult) -> list[str]:
    original, loaded = result.original, result.reloaded
    first = loaded.timelines[0]
    last = loaded.timelines[-1]
    return [
        "[6] D38 OPEN QUESTION 2 = RECOMPUTE (no derived timeline is persisted)",
        "    persisted per run       : inputs fingerprint, route fingerprint, cost_policy_json, "
        "timezone/tzdata metadata, stored route metrics",
        "    never persisted         : timelines / ETA / waiting - recomputed from the stored "
        "authoritative inputs",
        f"    timelines recomputed    : {len(loaded.timelines)} (first service start "
        f"{first.service_start.strftime('%Y-%m-%dT%H:%M:%SZ')}, FINISH arrival "
        f"{last.estimated_departure.strftime('%Y-%m-%dT%H:%M:%SZ')})",
        f"    every timeline equal    : {original.timelines == loaded.timelines}",
        f"    after metrics equal     : {original.metrics == loaded.metrics}",
        "",
    ]


def _summary_lines(result: RoundTripResult) -> list[str]:
    return [
        "[7] SUMMARY",
        f"    plan round trip         : {'EXACT' if result.plan_equal else 'MISMATCH'} "
        "(saved RoutePlan == reloaded RoutePlan)",
        f"    run round trip          : {'EXACT' if result.run_round_trip_equal else 'MISMATCH'} "
        f"(append -> list_for_plan/latest, {result.history_count} run)",
        f"    settings round trip     : "
        f"{'EXACT' if result.settings_round_trip_ok else 'MISMATCH'} "
        f"({len(SETTINGS_ENTRIES)} entries written and read back)",
        f"    engine after reload     : {'IDENTICAL' if result.routes_identical else 'MISMATCH'} "
        "(strict DST re-validation, D3)",
        f"    storage layout          : SQLite :memory:, schema version {result.schema_version} "
        "(storage/sqlite/migrations/0001_init.sql), no ORM, no database file created",
        "    not claimed             : no retention policy (all runs kept), no UI/API/provider work, "
        "the D36/D37 scale record is unchanged",
        _SEPARATOR,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Print the round-trip demo. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="demo.storage_roundtrip",
        description=(
            "Save the shipped demo plan, reload it, re-run the real engine on both, store and read "
            "back a real optimization run and two settings - all in an in-memory database."
        ),
    )
    parser.add_argument(
        "--allow-system-tzdata",
        action="store_true",
        help=(
            "development only: use a discovered system TZif tree when the tzdata package is "
            "unavailable (prints a warning on stderr)"
        ),
    )
    arguments = parser.parse_args(argv)

    if arguments.allow_system_tzdata:
        activated = tzdata.activate_system_tzif_fallback()
        if activated is not None:
            print(
                "[storage_roundtrip] WARNING: the tzdata package is unavailable; using the system "
                f"TZif tree at {activated}. This is a development fallback - install the real "
                "dependency with: python -m pip install tzdata",
                file=sys.stderr,
            )

    print(render_report(run_round_trip()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
