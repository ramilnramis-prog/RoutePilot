"""Framework-agnostic application layer for the RoutePilot API (Stage 4 U13/U14).

This module holds the application logic that the HTTP transport calls. It is deliberately **not**
an HTTP module:

* no HTTP types, no status codes, no JSON and no request/response objects appear here - the
  transport (today stdlib ``http.server``, later possibly FastAPI) owns all of that;
* failures are raised as the framework-neutral :class:`ServiceError` hierarchy below, whose names
  describe the *application* outcome (``NotFound``, ``InvalidInput``, ``CapabilityNotImplemented``,
  ``NoFirstStopSelected``, ``PlanBusy``, ...). ``api/http_server.py`` maps those names onto HTTP
  status codes in one documented table, so replacing the transport never touches this file;
* results are Python data and domain objects (:class:`PlanRecord`, :class:`AppSetting`,
  :class:`RecommendationResult`, :class:`RouteResult`, :class:`RunResult`, ...).
  ``api/serialization.py`` turns domain objects into JSON-ready payloads.

Two architectural rules from the architecture document are enforced by construction:

* ``api/`` may import ``core/``, ``storage/`` and the deterministic ``demo/`` fixture, and nothing
  imports ``api/`` from the inside - ``core/`` stays pure (D1, ``tests/test_core_isolation.py``);
* **no business formula lives in the transport or in this layer.** Every number the API reports is
  read from the domain objects, the storage repositories or ``core``'s own capability tables. A
  service method may validate a request and apply the approved MVP controls (``enabled``,
  ``priority`` and the driver's first-stop decision) through the domain, and may not compute a
  route metric of its own. The engine's own report, evaluation and metrics objects are carried
  through unchanged, so HTTP can never show a figure the engine did not produce.

Engine-facing surface (Stage 4 U14, D39)
========================================
:class:`RecommendationService`, :class:`SelectionService` and :class:`RouteService` add the
engine-facing application logic; the HTTP endpoints are documented in ``api/http_server.py``.

* **recommendation** - load the plan from SQLite and run the REAL engine
  (:func:`core.engine.first_stop.evaluation.evaluate_first_stop_candidates`): exhaustive, one
  candidate per enabled stop, no prefilter, no shortlist and no approximation (v2 section 20, D34).
  The report is returned as plain Python data through the engine's own report object.
  ``no_fully_feasible_route`` is a VALID answer with its diagnostics, never an error and never a
  fabricated winner (v2 section 14);
* **selection** - the driver's decision applied THROUGH THE DOMAIN and persisted via the plan
  repository (D4-D11/D32). Accepting the recommendation is mode ``recommend`` with
  ``selection_source = accepted_recommendation``; choosing another stop is mode ``manual`` with
  ``selection_source = manual_choice``; either way the selection is pinned (D5). Clearing returns
  the plan to ``awaiting_first_stop_choice`` with a null selected stop and a null source (D8/D9).
  An unknown stop, a disabled stop, an illegal transition and a mode/source mismatch are refused
  **loudly** - never repaired silently. A recommendation is never stored as the plan's selection;
* **committed route** - for the **selected** first stop only, computed by the real optimizer through
  the existing solve boundary (:func:`core.engine.optimizer.solve.solve_route`), returning the
  order, the per-stop timeline rows, the metrics with both baselines and the explicit violations.
  With nothing selected the service reports the documented :class:`NoFirstStopSelected` state
  instead of inventing a route;
* **run history** - one immutable row appended per recalculation, recording the fingerprints, the
  tzdata version actually in use, the cost policy actually used, the order, the recommendation
  payload that was shown (as history, never plan state), the top-K, the violations and the metrics
  with both baselines. The recorded recommendation is the engine's **own** ranked list and the
  top-K is its head, so a row can never claim a ``recommended_stop_id`` that is not ``top_k[0]``;
  because the driver decides, the run's committed ``order`` may legitimately start at a different
  stop than the one recorded as recommended, and both facts belong in the row (D32).
  ``created_at_utc`` is **when the row was created**, read from this layer's injectable UTC clock
  seam (never the plan's departure time, which is the plan's own fact). Run ids are API-assigned and
  deterministic (see :func:`run_id_for`): the API contains no business formula, but it must name the
  row it appends.

GET endpoints never write anything (owner decision 5)
-----------------------------------------------------
Computing a recommendation or a route appends **no** history, and no read path changes the plan.
Only ``optimize_and_record`` (``POST /api/plans/{id}/optimize``) appends a run; the selection change
is persisted because it *is* the request, and it does not recompute or append. All three
computations (recommendation, route, optimize) are guarded by a **per-plan single-flight lock**:
two concurrent requests for the same plan cannot run the exhaustive loop twice. Acquisition uses a
documented bounded wait (:data:`PLAN_LOCK_TIMEOUT_SECONDS`); when the bound expires the caller gets
:class:`PlanBusy` (``409 plan_busy``) with an honest message rather than a partial or fabricated
result. There is no background job queue and no async job/status subsystem (owner decision, D39(e)).
Each response carries the measured ``computation_seconds`` of its engine work; the accepted MVP
latency is stated honestly in ``api/http_server.py`` and in :func:`health_payload` (the ~50-stop
portfolio worst case is about 8 seconds, D36/D37).

Concurrency: a ``sqlite3`` connection is not safe to share between threads, so every request gets
its **own** connection, opened from the configured database identifier through
:func:`storage.sqlite.database.connect` (which enables ``PRAGMA foreign_keys`` and verifies it).
An in-process SQLite database opened on a ``file:...?mode=memory&cache=shared`` URI would be
destroyed as soon as its last connection closes, so in that one case a keeper connection is held
open for the lifetime of the :class:`DatabaseState` (see :meth:`DatabaseState.close`). A file-backed
database holds no keeper connection at all.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.engine.first_stop.evaluation import (
    FirstStopEvaluationReport,
    evaluate_first_stop_candidates,
)
from core.engine.optimizer.route_fingerprint import route_fingerprint
from core.engine.optimizer.solve import solve_route
from core.engine.providers import ProviderCapabilities, TravelMatrix
from core.model.cost_policy import CostComponent, default_component_declarations
from core.model.first_stop import (
    FirstStopCandidate,
    FirstStopIntent,
    FirstStopMode,
    FirstStopState,
    SelectionSource,
)
from core.model.ids import PlanId, RunId
from core.model.optimization_run import (
    ALGORITHM_NAME,
    ALGORITHM_VERSION,
    OptimizationRun,
    OptimizationRunMetrics,
    OptimizationRunRecommendation,
    RunKind,
    RunStatus,
)
from core.model.route_mode import ROUTE_MODE_STATUS, RouteMode
from core.model.route_plan import RoutePlan
from core.model.route_stop import GeocodeStatus, RouteStop, ServiceStatus
from core.model.service_window import WindowEndPolicy, WindowKind
from core.model.solution import RouteSolution, SolutionStatus
from api.map_configuration import (
    MAP_SETTING_KEYS,
    MapConfiguration,
    configured_map_configuration,
    map_configuration_payload,
)
from core.model.value_objects import DataProvenance
from core.repositories import (
    AppSettingsRepository,
    RouteOptimizationRunRepository,
    RoutePlanRepository,
)
from core.time import tzdata
from core.validation.errors import TZDATA_INSTALL_COMMAND, RoutePilotError
from demo.dataset import DEMO_PLAN_ID, build_demo_plan, demo_warning_text
from demo.synthetic_matrix import demo_matrix
from storage import StorageError
from storage.sqlite.app_settings_repository import SqliteAppSettingsRepository
from storage.sqlite.database import (
    SCHEMA_VERSION,
    connect,
    current_version,
    migrate,
    utc_now_iso,
)
from storage.sqlite.optimization_run_repository import SqliteRouteOptimizationRunRepository
from storage.sqlite.route_plan_repository import SqliteRoutePlanRepository

__all__ = [
    "FIRST_STOP_REQUEST_MODES",
    "IMPLEMENTED_ROUTE_MODE",
    "KNOWN_SETTING_KEYS",
    "MAX_RANKED_RECOMMENDATION_CANDIDATES",
    "MAX_REQUEST_BODY_BYTES",
    "PLAN_LOCKS",
    "PLAN_LOCK_TIMEOUT_SECONDS",
    "AppSetting",
    "ApiServices",
    "CapabilityNotImplemented",
    "Conflict",
    "DatabaseState",
    "InvalidInput",
    "NoFirstStopSelected",
    "NotFound",
    "PlanBusy",
    "PlanLockRegistry",
    "PlanRecord",
    "PlanService",
    "RecommendationResult",
    "RecommendationService",
    "RouteResult",
    "RouteService",
    "RunResult",
    "SelectionResult",
    "SelectionService",
    "ServiceError",
    "SettingsService",
    "TimezoneDataUnavailable",
    "UnknownPlan",
    "UnknownRun",
    "UnknownStop",
    "build_run",
    "capability_report",
    "health_payload",
    "run_id_for",
]

#: The one route mode this build implements (D19: the MVP implements SMART_ROUTE only).
IMPLEMENTED_ROUTE_MODE = RouteMode.SMART_ROUTE

#: Settings keys this API understands. The store itself owns no key semantics (D38 / schema
#: section 6), so this list documents the keys the demo surface uses - the approved map tile keys of
#: D15 (``tile_url``, ``tile_attribution``, ``tile_max_zoom``) plus the two map-library asset keys
#: this unit added so the Leaflet location is configuration rather than a URL frozen into
#: ``web/`` (``map_library_url``, ``map_library_css_url``) - and does not restrict what may be
#: stored. The list is taken from :data:`api.map_configuration.MAP_SETTING_KEYS` so the documented
#: keys, their defaults and the payload the UI reads cannot drift apart.
KNOWN_SETTING_KEYS: tuple[str, ...] = MAP_SETTING_KEYS

#: Default database identifier used by ``python -m api.serve``. ``var/`` is gitignored, so no
#: database artifact can be committed (`.gitignore` section "local databases").
DEFAULT_DB_PATH = "var/routepilot.db"

#: The provenance of every plan this API can serve: the deterministic demo fixture is
#: DEMO/SYNTHETIC, and the plan repository is configured with exactly this value (D15/D23).
DEMO_PROVENANCE = DataProvenance.DEMO_SYNTHETIC

#: Upper bound on a JSON request body the transport accepts, in bytes.
MAX_REQUEST_BODY_BYTES = 1_048_576

#: The documented bounded wait for the per-plan single-flight lock (owner decision 5, D39(e)).
#: A request that cannot take the plan's lock within this many seconds is refused with
#: ``409 plan_busy`` instead of queueing behind an exhaustive loop; there is no job queue (D39(e)).
PLAN_LOCK_TIMEOUT_SECONDS = 30.0

#: How many ranked candidates a recommendation response returns **as a top-K view** (v2 section 13).
#: This truncates the reported list only: ``counts.ranked`` still states how many candidates were
#: ranked, no candidate is dropped from the evaluation, and a ranked candidate that is not returned
#: in the K list is exactly the documented "alternatives" boundary. Rejected candidates are always
#: returned in full, because the diagnostics of v2 section 14 are the point of reporting them.
MAX_RANKED_RECOMMENDATION_CANDIDATES = 10

#: ``YYYY-MM-DDTHH:MM:SSZ`` - the storage UTC timestamp convention (schema section 1). A recorded
#: run's ``created_at_utc`` is taken from the same whole-second convention the storage layer writes.
_UTC_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------- #
# framework-neutral service errors
# --------------------------------------------------------------------------- #
class ServiceError(RoutePilotError):
    """Base class of every application-level failure this layer raises."""

    #: Documented API error code (see ``api.serialization.ERROR_CODES``).
    code = "internal_error"


class InvalidInput(ServiceError):
    """The request was understood but violates a rule (422 in the HTTP mapping)."""

    code = "invalid_input"


class NotFound(ServiceError):
    """The addressed thing does not exist (404 in the HTTP mapping)."""

    code = "unknown_path"


class Conflict(ServiceError):
    """The requested change is illegal in the current state (409 in the HTTP mapping)."""

    code = "illegal_state"


class CapabilityNotImplemented(ServiceError):
    """A declared-but-unimplemented capability was requested (501 in the HTTP mapping)."""

    code = "unsupported_capability"


class UnknownPlan(NotFound):
    """No stored plan has the addressed id (404 ``unknown_plan``)."""

    code = "unknown_plan"


class UnknownStop(NotFound):
    """The addressed plan has no stop with that id (404 ``unknown_stop``)."""

    code = "unknown_stop"


class UnknownRun(NotFound):
    """No stored optimization run has the addressed id (404 ``unknown_run``)."""

    code = "unknown_run"


class NoFirstStopSelected(Conflict):
    """A committed route was asked for while no first stop is selected (409, D9/I4).

    ``awaiting_first_stop_choice`` is a valid state, not a corruption: the engine recommends and the
    driver decides, so there is simply no route to commit yet. Reporting it as its own documented
    code keeps it distinguishable from an illegal transition, and the service never invents a route
    (or a selection) to fill the gap.
    """

    code = "no_first_stop_selected"


class PlanBusy(Conflict):
    """Another request holds this plan's computation lock (409 ``plan_busy``, owner decision 5).

    Raised when the documented bounded wait of :data:`PLAN_LOCK_TIMEOUT_SECONDS` expires. The honest
    answer is to refuse: there is no background job queue and no async job/status subsystem, and a
    partial or fabricated result must never be returned (D39(e)).
    """

    code = "plan_busy"


class TimezoneDataUnavailable(ServiceError):
    """No IANA time zone database is reachable, so the request cannot be answered (503)."""

    code = "timezone_data_unavailable"

    #: The exact command that fixes the environment (D12).
    fix = TZDATA_INSTALL_COMMAND


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlanRecord:
    """A stored plan together with the provenance it was read with.

    ``data_provenance`` is not part of :class:`~core.model.route_plan.RoutePlan` (the schema column
    is ``NOT NULL`` and the repository is constructed with it); carrying it beside the plan is what
    lets the API state provenance honestly instead of assuming it (D15/D23).
    """

    plan: Any
    data_provenance: DataProvenance


@dataclass(frozen=True)
class AppSetting:
    """One settings key and its stored value.

    ``configured`` is ``False`` when nothing is stored for the key. The store owns no setting's
    semantics, so no default value is invented here.
    """

    key: str
    value: Any
    configured: bool


@dataclass(frozen=True)
class RecommendationResult:
    """One live recommendation computation (U14): the engine's own report, unchanged.

    ``report`` is the :class:`~core.engine.first_stop.evaluation.FirstStopEvaluationReport` the real
    engine produced - not a copy, not a summary and not a re-derivation - so the transport cannot
    report a candidate the engine did not evaluate. ``computation_seconds`` is the measured
    wall-clock duration of the engine call itself, so a client can see the honest latency of the
    exhaustive loop it just paid for.
    """

    plan: RoutePlan
    report: FirstStopEvaluationReport
    computation_seconds: int


@dataclass(frozen=True)
class SelectionResult:
    """The driver's first-stop decision after a change (U14).

    ``previous_state`` and ``previous_stop_id`` describe the decision **before** this request, so a
    caller can see what changed without diffing two payloads. The plan is the persisted one that was
    read back through the repository.
    """

    plan: RoutePlan
    plan_id: str
    previous_state: FirstStopState
    previous_stop_id: str | None


@dataclass(frozen=True)
class RouteResult:
    """One committed-route computation for the plan's current selection (U14).

    ``solution`` is the engine's own :class:`~core.model.solution.RouteSolution` (order, timelines,
    metrics with both baselines, violations) and ``route_fingerprint`` is the committed route's own
    digest of that exact order (v2 section 7, D4). The API exposes **no matrix fingerprint**: ``core``
    offers no identity helper for the configured travel matrix (``route_fingerprint`` and
    ``RoutePlan.inputs_fingerprint`` *accept* a caller-supplied ``matrix_fingerprint`` but compute
    none), so the payload carries the two fingerprints the engine actually produces and no field
    that would be permanently ``null``.
    """

    plan: RoutePlan
    solution: RouteSolution
    route_fingerprint: str
    computation_seconds: int
    route_seconds: int


@dataclass(frozen=True)
class RunResult:
    """One appended run row plus the derivation that produced it (U14, U11/D38).

    ``run`` is the immutable history row as it was appended and read back; the plan, solution and
    recommendation are the engine objects the row was built from, so a caller can serialize the same
    route without recomputing it. ``recommendation_seconds`` and ``route_seconds`` are the measured
    durations of the two engine phases, and ``computation_seconds`` is their total - the honest
    latency of the recalculation this request paid for. As in :class:`RouteResult`, no matrix
    fingerprint is carried: the API does not expose one.
    """

    plan: RoutePlan
    solution: RouteSolution
    recommendation_report: FirstStopEvaluationReport
    route_fingerprint: str
    run: OptimizationRun
    recommendation_seconds: int
    route_seconds: int
    computation_seconds: int


# --------------------------------------------------------------------------- #
# the per-plan single-flight lock (owner decision 5, D39(e))
# --------------------------------------------------------------------------- #
class _PlanLock:
    """A non-reentrant lock with a *bounded* wait, so contention is an answer, not a queue.

    The bound is part of the contract: a caller either gets the lock inside it or is told the plan is
    busy. Nothing here waits indefinitely and nothing here runs work in the background.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, timeout_seconds: float) -> bool:
        """Take the lock within ``timeout_seconds``; ``False`` means the bound expired."""
        return self._lock.acquire(timeout=max(0.0, float(timeout_seconds)))

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class PlanLockRegistry:
    """One single-flight lock per plan id, shared by every service instance in the process.

    The registry is **module-level** on purpose: the invariant it protects ("two concurrent requests
    for the same plan cannot run the exhaustive loop twice") is a property of the plan in this
    process, not of one service object. A per-``ApiServices`` registry would let two containers over
    the same database run the loop twice for one plan, which is exactly the situation the owner's
    per-plan single-flight requirement rules out. Locks are created on demand and never removed, so a
    plan id can never be served by two different locks; the map is one small object per plan id the
    process has served.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, _PlanLock] = {}

    def lock_for(self, plan_id: str) -> _PlanLock:
        with self._guard:
            lock = self._locks.get(plan_id)
            if lock is None:
                lock = _PlanLock()
                self._locks[plan_id] = lock
            return lock

    @contextmanager
    def held(self, plan_id: str, *, timeout_seconds: float) -> Iterator[None]:
        """Hold the plan's lock for the block, or raise :class:`PlanBusy`.

        A request that times out gets an honest refusal naming the bound it waited for - never a
        partial result and never a second unsynchronised engine run.
        """
        lock = self.lock_for(plan_id)
        if not lock.acquire(timeout_seconds):
            raise PlanBusy(
                f"plan {plan_id!r} is already computing another recommendation or route and did not "
                f"become free within the documented bound of {timeout_seconds:g}s; this API answers "
                "synchronously and has no background job queue, so the request is refused rather "
                "than returning a partial or fabricated result (owner decision 5, D39(e)). Retry "
                "shortly."
            )
        try:
            yield
        finally:
            lock.release()


#: The process-wide single-flight registry (see :class:`PlanLockRegistry`).
PLAN_LOCKS = PlanLockRegistry()


def run_id_for(
    *, plan_id: str, run_kind: RunKind, route_fingerprint: str, sequence: int
) -> str:
    """The deterministic id of the run this recalculation appends.

    Run ids are **API-assigned**, because the domain and the approved schema leave the id to the
    caller and this layer is the caller. The id is a digest of the plan, the run kind, the route
    fingerprint and the plan's run sequence, so the same recalculation of the same plan names the
    same row reproducibly while two successive runs can never collide. It is an identifier, not a
    measurement: no business figure is derived here.
    """
    material = f"{plan_id}|{run_kind.value}|{route_fingerprint}|{sequence}"
    return f"run-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _measured_seconds(duration_seconds: float) -> int:
    """A measured wall-clock duration in whole seconds (latency evidence, never a route metric).

    Durations in this API are integer seconds; a sub-second computation is reported as ``0``, which
    is the honest rounding of a measurement, not a claim that no work happened.
    """
    return max(0, int(round(duration_seconds)))


# --------------------------------------------------------------------------- #
# capability honesty (D16, spec section 23)
# --------------------------------------------------------------------------- #
#: Which provider capability each ``requires_provider`` cost component actually needs.
_COMPONENT_PROVIDER_CAPABILITY: dict[CostComponent, str] = {
    CostComponent.U_TURN_PENALTY: "real_routing",
    CostComponent.WRONG_SIDE_PENALTY: "side_of_road",
    CostComponent.BACKTRACKING_PENALTY: "real_routing",
}

#: Status of every feature this API surface must be honest about.
_CAPABILITY_STATUS: dict[str, str] = {
    "engine": "implemented",
    "demand_side_of_road": "requires_provider",
    "demand_traffic": "requires_provider",
    "demand_turn_by_turn": "planned",
    "demand_geocoding": "planned",
    "demand_real_routing": "unsupported",
}

#: Human-readable explanation of each capability: what a caller may believe.
_CAPABILITY_DETAIL: dict[str, str] = {
    "engine": (
        "the deterministic complete-route optimizer and the exhaustive first-stop recommendation "
        "report a complete route (FINISH leg included) from the DEMO/SYNTHETIC travel matrix"
    ),
    "demand_side_of_road": (
        "the true side of a road cannot be inferred from latitude/longitude, so no side-of-road "
        "logic exists (spec sections 11/23, D16)"
    ),
    "demand_traffic": "no live or historical traffic data is used anywhere",
    "demand_turn_by_turn": (
        "no turn-by-turn instructions are produced; geometry would come from a routing provider"
    ),
    "demand_geocoding": (
        "no GeocodingProvider is implemented: coordinates come from the demo fixture, and an "
        "address is never guessed (spec section 16/24)"
    ),
    "demand_real_routing": (
        "travel time and distance are DEMO/SYNTHETIC and are never real road routing (spec "
        "section 33)"
    ),
}

#: The capabilities a plan/route request may ask for, and whether each is implemented. A request
#: mentioning any of these is refused rather than silently ignored (D16).
DEMAND_CAPABILITIES: tuple[str, ...] = (
    "demand_geocoding",
    "demand_real_routing",
    "demand_traffic",
    "demand_side_of_road",
    "demand_turn_by_turn",
)


def _provider_capabilities() -> ProviderCapabilities:
    """The provider capabilities this build actually has.

    ``ProviderCapabilities`` defaults every flag to ``False`` (D16), which is exactly the honest
    answer here: the demo uses no external provider at all.
    """
    return ProviderCapabilities()


def capability_report() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(implemented, not_implemented)`` capability records for the health payload.

    Every status comes from the domain's own registry rather than from a list maintained here:
    the cost-component declarations of :mod:`core.model.cost_policy` and
    :data:`core.model.route_mode.ROUTE_MODE_STATUS`. A component whose status is
    ``requires_provider`` names the provider capability it needs, so "why is this missing" has a
    real answer. Route modes: :data:`IMPLEMENTED_ROUTE_MODE` is the mode this build ranks with, and
    every other declared mode is reported as not implemented (D19).
    """
    declarations = default_component_declarations()
    implemented: list[dict[str, Any]] = []
    not_implemented: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, requires: str | None = None) -> None:
        entry: dict[str, Any] = {
            "capability": name,
            "status": status,
            "detail": detail,
            "requires": requires,
        }
        (implemented if status == "implemented" else not_implemented).append(entry)

    implemented.append(
        {
            "capability": "engine",
            "status": "implemented",
            "detail": _CAPABILITY_DETAIL["engine"],
            "requires": None,
        }
    )
    for component in CostComponent:
        declaration = declarations[component]
        entry = {
            "capability": f"cost_component:{component.value}",
            "status": declaration.status.value,
            "detail": declaration.note,
            "requires": declaration.requires,
        }
        if declaration.status.value == "implemented":
            implemented.append(entry)
        else:
            not_implemented.append(entry)

    add(
        f"route_mode:{IMPLEMENTED_ROUTE_MODE.value}",
        "implemented",
        (
            "the implemented route mode: the complete elapsed route duration objective "
            "(travel + waiting at 1:1, D35), ranked by the owner's deterministic 5-key tuple"
        ),
    )
    for mode in RouteMode:
        if mode is IMPLEMENTED_ROUTE_MODE:
            continue
        add(
            f"route_mode:{mode.value}",
            ROUTE_MODE_STATUS[mode],
            (
                "declared in the domain and representable, but this build does not produce routes "
                "for it; asking for it is refused instead of falling back to SMART_ROUTE (D19)"
            ),
            requires=f"a routing provider able to price the {mode.value} criterion",
        )

    for capability in DEMAND_CAPABILITIES:
        add(
            f"provider:{capability.removeprefix('demand_')}",
            _CAPABILITY_STATUS[capability],
            _CAPABILITY_DETAIL[capability],
            requires=(
                "a real RoutingProvider/GeocodingProvider and a working network connection"
                if _CAPABILITY_STATUS[capability] != "unsupported"
                else None
            ),
        )

    capabilities = _provider_capabilities()
    for flag, label in (
        ("one_way", "one_way"),
        ("real_road_routing", "real_road_routing"),
    ):
        add(
            f"provider:{label}",
            "requires_provider" if not getattr(capabilities, flag) else "implemented",
            f"the configured providers report {label}={getattr(capabilities, flag)} (D16)",
            requires=None if getattr(capabilities, flag) else f"provider capability {label}",
        )
    return implemented, not_implemented


def _tzdata_payload() -> dict[str, Any]:
    """The IANA database state, exactly as ``core.time.tzdata`` detects it (D2/D12)."""
    report = tzdata.probe_tzdata()
    payload = {
        "source": report.status,
        "iana_version": report.iana_version,
        "detail": report.detail,
        "fallback_candidate": report.fallback_candidate,
        "search_path": list(report.search_path),
        "install_command": report.install_command,
        "available": report.is_available,
    }
    if not report.is_available:
        payload["fix"] = (
            f"{TZDATA_INSTALL_COMMAND} (or set PYTHONTZPATH to a compiled TZif tree for "
            "development only; production never activates a fallback silently)"
        )
    return payload


def health_payload(
    state: DatabaseState, *, map_configuration: MapConfiguration | None = None
) -> dict[str, Any]:
    """The honest health/capability document (spec sections 23/31/33/36, D12/D16/D19/D36/D39).

    It reports the IANA database source and version *or* the honest fallback state, states that
    the only shipped data is the DEMO/SYNTHETIC fixture, lists the implemented capabilities next to
    the not-implemented ones (traffic, side-of-road, turn-by-turn, geocoding, real routing, and
    every route mode except SMART_ROUTE) so a UI can be honest instead of optimistic, reports the
    **map configuration in force** (U15: the approved tile keys of D15 plus the map-library URLs,
    each with whether it was configured or defaulted - see :mod:`api.map_configuration`), and states
    the **accepted MVP latency honestly** (U14): the synchronous exhaustive recommendation has no
    background job, a documented bounded per-plan single-flight wait - reported as the bound this
    instance actually runs with - and a worst case of about 8 seconds at the ~50-enabled-stop
    portfolio scale (D36/D37).

    ``map_configuration`` is passed in by the caller that can read ``app_settings``
    (``SettingsService.map_configuration``); when it is omitted the documented defaults are reported
    with ``source="default"``, so this function stays pure enough for a caller with no database.
    """
    implemented, not_implemented = capability_report()
    report = tzdata.probe_tzdata()
    bound = getattr(state, "plan_lock_timeout_seconds", PLAN_LOCK_TIMEOUT_SECONDS)
    return {
        "status": "ok",
        "api_version": "1",
        "implemented_units": (
            "U13 ships the read/config surface (health, plans, plan controls, settings), U14 adds "
            "the engine-facing surface (the recommendation, the driver's selection, the committed "
            "route, the recalculation that appends one run row and the run history), and U15 ships "
            "the static web workspace in web/ that renders them (map, route/timeline panel and "
            "summary). U16 (the interactive override controls and the run-history view) and U17 "
            "(end-to-end demo and Stage 4 documentation) are still pending."
        ),
        "timezone_data": _tzdata_payload(),
        "demo_data": {
            "present": True,
            "provenance": DataProvenance.DEMO_SYNTHETIC.value,
            "warning": demo_warning_text(),
            "labelled": ["DEMO", "SYNTHETIC"],
        },
        "data_provenance": DataProvenance.DEMO_SYNTHETIC.value,
        "map": map_configuration_payload(map_configuration),
        "database": {
            "identifier": state.display_identifier,
            "schema_version": state.schema_version,
            "schema_target_version": SCHEMA_VERSION,
            "implementation": state.plan_repository_name,
        },
        "computation": {
            "synchronous": True,
            "background_job_queue": False,
            "per_plan_single_flight": True,
            "plan_busy_status": 409,
            "plan_busy_error_code": "plan_busy",
            "lock_wait_bound_seconds": bound,
            "computation_seconds_reported": True,
            "accepted_mvp_latency": (
                "about 8 seconds worst case for the exhaustive first-stop recommendation at the "
                "~50-enabled-stop portfolio scale (D36/D37); this is an ACCEPTED MVP limitation, "
                "reported as measured and never hidden"
            ),
            "notes": (
                "every response carries the measured computation_seconds of its engine work, and a "
                "request that cannot take the plan lock within the bound is refused with 409 "
                "plan_busy instead of returning a partial or fabricated route (owner decision 5)"
            ),
        },
        "implemented_capabilities": implemented,
        "not_implemented_capabilities": not_implemented,
        "route_modes": {
            "implemented": [IMPLEMENTED_ROUTE_MODE.value],
            "not_implemented": [
                mode.value for mode in RouteMode if mode is not IMPLEMENTED_ROUTE_MODE
            ],
        },
        "notes": [
            (
                "local wall-clock service windows are resolved through the plan's IANA zone under "
                "strict DST validation (D3)"
            ),
            (
                "no background job queue: every request answers from the engine and the database "
                "synchronously"
            ),
            (
                "the tzdata state above is the real detected state; a missing database is reported "
                "as missing, never papered over"
            ),
            (
                "the honest timezone state is "
                f"{report.status} (IANA version {report.iana_version or 'unknown'})"
            ),
        ],
    }


# --------------------------------------------------------------------------- #
# database state: one connection per request
# --------------------------------------------------------------------------- #
class _PlanRepositoryFactory(Protocol):
    """Builds a plan repository over one connection for one request."""

    def __call__(
        self, connection: sqlite3.Connection, *, data_provenance: DataProvenance
    ) -> RoutePlanRepository: ...


class _SettingsRepositoryFactory(Protocol):
    """Builds a settings repository over one connection for one request."""

    def __call__(self, connection: sqlite3.Connection) -> AppSettingsRepository: ...


class _RunRepositoryFactory(Protocol):
    """Builds the immutable run-history repository over one connection for one request."""

    def __call__(self, connection: sqlite3.Connection) -> RouteOptimizationRunRepository: ...


def _is_in_memory(identifier: str) -> bool:
    return ":memory:" in identifier or "mode=memory" in identifier


def _factory_name(factory: Any) -> str:
    """A readable name for a repository factory, used only in the health payload."""
    name = getattr(factory, "__name__", None) or getattr(factory, "__qualname__", None)
    if name and name != "<lambda>":
        return f"{getattr(factory, '__module__', '')}.{name}".lstrip(".")
    return f"storage.sqlite.{SqliteRoutePlanRepository.__name__}"


class DatabaseState:
    """The configured database, plus the one-time migration and the per-request connections.

    Configuration: the database identifier is either a filesystem path (the default,
    ``var/routepilot.db``, which lives in a gitignored directory) or an explicit SQLite identifier
    such as ``":memory:"``/``"file:...?mode=memory&cache=shared"``. The storage layer owns the
    connection pragmas; this class never opens a raw ``sqlite3`` connection itself.

    The migration runner runs **once**, here, and a schema that is not current afterwards is a
    loud failure rather than a surprise at the first request.

    ``display_identifier`` is what the caller configured (and what the health payload reports).
    """

    def __init__(
        self,
        identifier: str | Path = DEFAULT_DB_PATH,
        *,
        plan_repository_factory: _PlanRepositoryFactory | None = None,
        settings_repository_factory: _SettingsRepositoryFactory | None = None,
        run_repository_factory: _RunRepositoryFactory | None = None,
        plan_repository_name: str | None = None,
        plan_lock_timeout_seconds: float = PLAN_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.identifier = str(identifier)
        #: The documented bounded wait of this instance's per-plan single-flight lock; the health
        #: payload reports exactly this value, so the documented bound is never a second constant.
        self.plan_lock_timeout_seconds = float(plan_lock_timeout_seconds)
        display = (
            self.identifier
            if _is_in_memory(self.identifier)
            else str(Path(self.identifier))
        )
        self.display_identifier = display
        self._plan_factory: _PlanRepositoryFactory = (
            plan_repository_factory
            if plan_repository_factory is not None
            else lambda connection, *, data_provenance: SqliteRoutePlanRepository(
                connection, data_provenance=data_provenance
            )
        )
        self._settings_factory: _SettingsRepositoryFactory = (
            settings_repository_factory
            if settings_repository_factory is not None
            else SqliteAppSettingsRepository
        )
        self._run_factory: _RunRepositoryFactory = (
            run_repository_factory
            if run_repository_factory is not None
            else SqliteRouteOptimizationRunRepository
        )
        self.plan_repository_name = plan_repository_name or _factory_name(self._plan_factory)
        #: Kept open only for an in-process SQLite database; ``None`` for a file-backed one.
        self._keeper: sqlite3.Connection | None = None
        self.schema_version = self._prepare()

    # -- lifecycle ------------------------------------------------------- #
    def _prepare(self) -> int:
        connection = self.connect()
        try:
            migrate(connection)
            version = current_version(connection)
        except BaseException:
            # Nothing is served if the schema cannot be brought current: close and refuse.
            connection.close()
            raise
        if _is_in_memory(self.identifier):
            # The in-memory database exists only while a connection to it is open.
            self._keeper = connection
        else:
            connection.close()
        if version != SCHEMA_VERSION:
            self.close()
            raise StorageError(
                f"database {self.display_identifier} is at schema version {version} but this "
                f"build requires version {SCHEMA_VERSION}; the migration runner applied every "
                "migration it knows, so the stored schema belongs to a different build"
            )
        return version

    def connect(self) -> sqlite3.Connection:
        """A fresh connection for one request, with the storage pragmas applied.

        For a file-backed database the path is opened directly. For an in-process database
        (``:memory:`` or ``file:...?mode=memory&cache=shared``) the identifier is used as given:
        SQLite keys a shared-cache in-memory database by its name and keeps it for the lifetime of
        the process, which is exactly what makes "one connection per request" work against it. A
        caller that wants a *fresh* in-process database therefore uses a fresh name, and a
        long-running test that needs a guaranteed-empty database uses a file it deletes.
        """
        return connect(self.identifier)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One request's connection, **closed** when the block ends however it ends.

        ``sqlite3.Connection`` is a context manager, but the ``with`` block only commits or rolls
        back the transaction - it does not close the connection. Every request must therefore close
        its own connection explicitly, which is what this helper is for: the service layer opens a
        connection here, uses it and gives it back, so a long-running server cannot accumulate open
        connections (and nothing leaks a WAL or a lock on a file-backed database).
        """
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def close(self) -> None:
        """Release the in-memory keeper connection, if one is held. Idempotent."""
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.close()

    def plan_repository(self, connection: sqlite3.Connection) -> RoutePlanRepository:
        """A plan repository for one request, configured with :data:`DEMO_PROVENANCE`.

        The provenance is decided **here**, once: every plan this transport can create or read is
        the DEMO/SYNTHETIC demo fixture, so the repository is constructed with that provenance and
        reads it back on load (the adapter refuses a row carrying another value, which is a honesty
        guard: it cannot hand back real-routing data as if it were the demo's).
        """
        return self._plan_factory(connection, data_provenance=DEMO_PROVENANCE)

    def settings_repository(self, connection: sqlite3.Connection) -> AppSettingsRepository:
        return self._settings_factory(connection)

    def run_repository(
        self, connection: sqlite3.Connection
    ) -> RouteOptimizationRunRepository:
        """The immutable run-history repository for one request (U14).

        The adapter is constructed without any provenance of its own: a run row carries the
        provenance of the computation that produced it (the demo travel matrix reports
        ``DEMO_SYNTHETIC``), and this API never invents it.
        """
        return self._run_factory(connection)


# --------------------------------------------------------------------------- #
# plans: the read/config surface of U13
# --------------------------------------------------------------------------- #
#: Body fields ``POST /api/plans`` accepts. Anything else is refused, so an unimplemented request
#: can never be silently ignored (D16). ``route_mode`` is accepted only as ``SMART_ROUTE``: every
#: other mode is refused with ``501`` by :meth:`PlanService._require_implemented_route_mode` (D19).
_CREATE_ACCEPTED_FIELDS = frozenset(
    {"id", "provider", "provenance", "kind", "data_provenance", "route_mode"}
)

#: Request fields that amount to asking for a capability this build does not have. The value is
#: ``(documented error code, what the caller asked for)``.
_UNIMPLEMENTED_REQUEST_FIELDS: dict[str, tuple[str, str]] = {
    "geocode": ("demand_geocoding", "geocoding an address"),
    "geocoding": ("demand_geocoding", "geocoding an address"),
    "addresses": ("demand_geocoding", "geocoding a list of addresses"),
    "optimize": ("demand_real_routing", "optimizing a route with a real routing provider"),
    "routing": ("demand_real_routing", "real road routing"),
    "matrix": ("demand_real_routing", "a real travel-time matrix"),
    "traffic": ("demand_traffic", "traffic data"),
    "side_of_road": ("demand_side_of_road", "side-of-road information"),
    "turn_by_turn": ("demand_turn_by_turn", "turn-by-turn navigation"),
    "recommend": (
        "unsupported_capability",
        "a first-stop recommendation requested through the plan-creation body (the recommendation "
        "has its own endpoint, GET /api/plans/{id}/recommendation, U14)",
    ),
    "route": (
        "unsupported_capability",
        "a computed route requested through the plan-creation body (the route has its own "
        "endpoint, GET /api/plans/{id}/route, U14)",
    ),
    "runs": (
        "unsupported_capability",
        "optimization-run history requested through the plan-creation body (it has its own "
        "endpoint, GET /api/plans/{id}/runs, U14)",
    ),
}

#: Per-stop control fields ``PUT /api/plans/{id}`` accepts - exactly the two MVP controls the
#: owner approved. Drag/reorder and the first-stop choice are deliberately absent: the first stop has
#: its own endpoint (``POST``/``DELETE /api/plans/{id}/selection``, U14) because it is a different
#: kind of change (the driver's decision, not a stop attribute), and drag/reorder is out of scope
#: for this stage (D39(d)).
_UPDATE_STOP_FIELDS = frozenset({"stop_id", "enabled", "priority"})

#: A plan id must remain one clean path segment, so it can never smuggle a slash or a control
#: character into storage or into a URL.
_MAX_ID_LENGTH = 200


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidInput(
            f"{field_name} must be a JSON boolean (true/false), got {value!r}; this is the API, "
            "not the storage DDL, so 0/1 is not accepted"
        )
    return value


def _require_priority(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"{field_name} must be a whole number or null, got {value!r}")
    if value < 0:
        raise InvalidInput(f"{field_name} must be >= 0, got {value}")
    return value


def _validate_plan_id(plan_id: str) -> str:
    candidate = _require_str(plan_id, "plan id")
    if len(candidate) > _MAX_ID_LENGTH or any(ord(char) < 32 for char in candidate):
        raise InvalidInput(
            f"plan id {candidate!r} is not usable as a single URL path segment (it must be at "
            f"most {_MAX_ID_LENGTH} characters and carry no control characters)"
        )
    if "/" in candidate or "\\" in candidate:
        ratio = "a slash or backslash"
        raise InvalidInput(f"plan id {candidate!r} must not contain {ratio}")
    return candidate


class PlanService:
    """Plan reads and the two approved MVP edit controls. No route computation lives here."""

    def __init__(self, state: DatabaseState) -> None:
        self._state = state

    def list_plans(self) -> tuple[PlanRecord, ...]:
        """Every stored plan, in the repository's documented deterministic order."""
        with self._state.connection() as connection:
            repository = self._state.plan_repository(connection)
            provenance = _provenance_of(repository)
            return tuple(
                PlanRecord(plan=plan, data_provenance=provenance)
                for plan in repository.list()
            )

    def get_plan(self, plan_id: str) -> PlanRecord:
        """One plan with its stops, its first-stop state and its provenance (D4-D11/D32).

        Raises :class:`NotFound` when no plan has that id - absence is a normal answer from the
        repository, and the HTTP mapping turns it into a documented 404.
        """
        identifier = _validate_plan_id(plan_id)
        with self._state.connection() as connection:
            repository = self._state.plan_repository(connection)
            provenance = _provenance_of(repository)
            plan = repository.get(PlanId(identifier))
        if plan is None:
            raise NotFound(f"no stored plan has id {identifier!r}")
        return PlanRecord(plan=plan, data_provenance=provenance)

    def create_demo_plan(self, body: dict[str, Any] | None = None) -> PlanRecord:
        """Create (or return) the deterministic DEMO/SYNTHETIC demo plan.

        This is the portfolio demo's "open or create the demo plan" step: the fixture is built by
        :func:`demo.dataset.build_demo_plan`, which is deterministic, and is persisted through the
        plan repository. Calling it twice returns the same stored plan instead of duplicating it.

        A request that would imply geocoding or real routing is refused with
        :class:`CapabilityNotImplemented` naming the capability, the requested field and the
        documented error code - never accepted and silently ignored (D16).

        Raises:
            CapabilityNotImplemented: the body asks for an unimplemented capability, or for any
                route mode other than ``SMART_ROUTE`` (D19).
            InvalidInput: the body is not an object, or carries a field this endpoint does not
                accept.
        """
        request = self._validate_create_request(body)
        plan_id = request.get("id") or DEMO_PLAN_ID
        identifier = _validate_plan_id(plan_id)
        plan = build_demo_plan(plan_id=identifier)
        with self._state.connection() as connection:
            repository = self._state.plan_repository(connection)
            provenance = _provenance_of(repository)
            existing = repository.get(PlanId(identifier))
            if existing is None:
                repository.save(plan)
            else:
                plan = existing
        return PlanRecord(plan=plan, data_provenance=provenance)

    def update_plan_controls(self, plan_id: str, body: dict[str, Any] | None) -> PlanRecord:
        """Apply the approved MVP stop controls (``enabled`` and ``priority``) and persist them.

        The change goes through the domain: each stop is rebuilt with
        :func:`dataclasses.replace`, so the model's own validation runs, and the plan is written
        back through the repository (which re-validates on the next load).

        Deliberately absent: drag/reorder (out of scope for this stage, D39(d)) and any change to the
        first-stop choice - that is the driver's **decision**, and it goes through its own endpoint
        (``POST``/``DELETE /api/plans/{id}/selection``, U14), so it is never smuggled into a stop
        attribute. Asking for either here is refused rather than half-implemented.

        Raises:
            InvalidInput: the body is not an object, has no ``stops`` list, or a stop update is
                malformed (422).
            NotFound: the plan, or one of the named stops, does not exist (404).
            Conflict: a flag change carries no meaning (for example an update with no keys at all).
        """
        identifier = _validate_plan_id(plan_id)
        request = _require_object(body, "request body")
        unexpected = sorted(set(request) - {"stops"})
        if unexpected:
            raise InvalidInput(
                "PUT /api/plans/{id} accepts only the approved MVP controls in a 'stops' list; "
                f"unexpected field(s): {', '.join(unexpected)}. Drag/reorder is out of scope for "
                "this stage, and the first-stop choice has its own endpoint: POST|DELETE "
                "/api/plans/{id}/selection."
            )
        if "stops" not in request:
            raise InvalidInput(
                "PUT /api/plans/{id} needs a 'stops' list of {stop_id, enabled} / "
                "{stop_id, priority} entries"
            )
        entries = request["stops"]
        if not isinstance(entries, list) or not entries:
            raise InvalidInput("'stops' must be a non-empty JSON list of stop updates")

        with self._state.connection() as connection:
            repository = self._state.plan_repository(connection)
            provenance = _provenance_of(repository)
            plan = repository.get(PlanId(identifier))
            if plan is None:
                raise NotFound(f"no stored plan has id {identifier!r}")

            by_id = {stop.id: stop for stop in plan.stops}
            updated: dict[str, RouteStop] = {}
            for index, entry in enumerate(entries):
                stop_id, changes = self._stop_changes(index, entry)
                if stop_id not in by_id:
                    raise NotFound(
                        f"plan {identifier!r} has no stop {stop_id!r}"
                    )
                updated[stop_id] = replace(by_id[stop_id], **changes)

            new_plan = replace(
                plan,
                stops=tuple(updated.get(stop.id, stop) for stop in plan.stops),
            )
            repository.save(new_plan)
        return PlanRecord(plan=new_plan, data_provenance=provenance)

    # -- request validation ---------------------------------------------- #
    def _validate_create_request(self, body: dict[str, Any] | None) -> dict[str, Any]:
        if body is None:
            return {}
        request = _require_object(body, "request body")
        self._refuse_unimplemented(request)
        unexpected = sorted(set(request) - _CREATE_ACCEPTED_FIELDS)
        if unexpected:
            raise InvalidInput(
                "POST /api/plans accepts only the demo fixture request fields "
                f"({', '.join(sorted(_CREATE_ACCEPTED_FIELDS))}); unexpected field(s): "
                f"{', '.join(unexpected)}"
            )
        kind = request.get("kind")
        if kind is not None and kind not in ("demo", "demo_fixture"):
            raise CapabilityNotImplemented(
                f"this build can only create the DEMO/SYNTHETIC demo fixture plan, so 'kind' "
                f"must be 'demo'; got {kind!r}. Creating a plan from addresses needs the "
                "geocoding pipeline (spec sections 16/24) and is not implemented."
            )
        provenance = request.get("data_provenance", request.get("provenance"))
        if provenance is not None and provenance != DataProvenance.DEMO_SYNTHETIC.value:
            raise CapabilityNotImplemented(
                f"this build ships DEMO/SYNTHETIC data only, so data_provenance must be "
                f"{DataProvenance.DEMO_SYNTHETIC.value!r}; got {provenance!r}. Real routing data "
                "needs a RoutingProvider that does not exist yet (D15/D16)."
            )
        provider = request.get("provider")
        if provider is not None:
            raise CapabilityNotImplemented(
                f"no external provider is implemented, so 'provider' cannot be honoured "
                f"(got {provider!r}); the demo travels on the deterministic synthetic matrix"
            )
        return request

    def _refuse_unimplemented(self, request: dict[str, Any]) -> None:
        """Refuse any field that asks for a capability this build does not have (D16/D19)."""
        for key, raw in request.items():
            if key in _UNIMPLEMENTED_REQUEST_FIELDS:
                capability, requested = _UNIMPLEMENTED_REQUEST_FIELDS[key]
                raise CapabilityNotImplemented(
                    f"'{key}' asks for {requested}, which is not implemented: the route mode "
                    "support of this build is SMART_ROUTE only and there is no geocoding or real "
                    f"routing capability (capability={capability!r}, D16/D19)"
                )
            if key == "route_mode":
                self._require_implemented_route_mode(raw)
        provider = request.get("provider")
        if isinstance(provider, dict):
            for key in provider:
                if key in _UNIMPLEMENTED_REQUEST_FIELDS:
                    capability, requested = _UNIMPLEMENTED_REQUEST_FIELDS[key]
                    raise CapabilityNotImplemented(
                        f"the requested provider asks for {requested}, which is not implemented "
                        f"(capability={capability!r}, D16)"
                    )

    @staticmethod
    def _require_implemented_route_mode(value: Any) -> RouteMode:
        try:
            mode = value if isinstance(value, RouteMode) else RouteMode(value)
        except ValueError:
            raise InvalidInput(
                f"unknown route mode {value!r}; expected one of "
                f"{[mode.value for mode in RouteMode]}"
            ) from None
        if mode is not IMPLEMENTED_ROUTE_MODE:
            raise CapabilityNotImplemented(
                f"route mode {mode.value!r} is declared in the domain but not implemented "
                f"(status={ROUTE_MODE_STATUS[mode]!r}); this build implements "
                f"{IMPLEMENTED_ROUTE_MODE.value} only and never falls back to it silently (D19)"
            )
        return mode

    @staticmethod
    def _stop_changes(index: int, entry: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(entry, dict):
            raise InvalidInput(f"stops[{index}] must be a JSON object, got {entry!r}")
        unknown = sorted(set(entry) - _UPDATE_STOP_FIELDS)
        if unknown:
            raise InvalidInput(
                f"stops[{index}] accepts only stop_id, enabled and priority; unexpected field(s): "
                f"{', '.join(unknown)}. Drag/reorder is out of scope for this stage, and the "
                "first-stop choice has its own endpoint (POST|DELETE /api/plans/{id}/selection)."
            )
        stop_id = _require_str(entry.get("stop_id"), f"stops[{index}].stop_id")
        changes: dict[str, Any] = {}
        if "enabled" in entry:
            changes["enabled"] = _require_bool(entry["enabled"], f"stops[{index}].enabled")
        if "priority" in entry:
            changes["priority"] = _require_priority(entry["priority"], f"stops[{index}].priority")
        if not changes:
            raise Conflict(
                f"stops[{index}] for stop {stop_id!r} carries no change: give 'enabled' or "
                "'priority'"
            )
        return stop_id, changes


# --------------------------------------------------------------------------- #
# U14 helpers: request vocabulary, the selection state machine and the run row
# --------------------------------------------------------------------------- #
#: The documented ``mode`` vocabulary of the selection endpoint. ``accept`` is a documented
#: shorthand for "the driver pressed start-from-this-stop on the recommendation" and is exactly
#: ``recommend`` semantics; it exists so a UI can express the action it performed.
FIRST_STOP_REQUEST_MODES: tuple[str, ...] = (
    FirstStopMode.RECOMMEND.value,
    FirstStopMode.MANUAL.value,
    "accept",
)


def _require_first_stop_mode(value: Any) -> str:
    """The request's ``mode``, or :class:`InvalidInput` naming the accepted values (422)."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(
            f"mode must be a non-empty string, got {value!r}; expected one of "
            f"{list(FIRST_STOP_REQUEST_MODES)}"
        )
    mode = value.strip()
    if mode not in FIRST_STOP_REQUEST_MODES:
        raise InvalidInput(
            f"unknown first-stop mode {mode!r}; expected one of {list(FIRST_STOP_REQUEST_MODES)}. "
            "A mode/source combination the domain does not accept is refused rather than repaired "
            "(D6/D7)."
        )
    return mode


def _find_stop(plan: RoutePlan, stop_id: str, *, plan_id: str) -> RouteStop:
    """The addressed stop, or :class:`UnknownStop` (404) - never a silent skip."""
    for stop in plan.stops:
        if stop.id == stop_id:
            return stop
    raise UnknownStop(f"plan {plan_id!r} has no stop {stop_id!r}")


def _require_committed_route_is_possible(plan: RoutePlan) -> None:
    """A committed route requires the driver's explicit first stop (I4/D32, D9).

    ``awaiting_first_stop_choice`` is a valid state, not an error: the honest answer is
    :class:`NoFirstStopSelected` (409) and never a route the engine built by choosing a stop itself.
    """
    if plan.first_service_stop.selected_stop_id is None:
        raise NoFirstStopSelected(
            f"plan {plan.id!r} is awaiting_first_stop_choice: no first stop is selected, so there "
            "is no committed route to return. The engine recommends, the driver decides (D4/D32, "
            "I4) - accept the recommendation or choose a stop first."
        )


def _run_status_for(solution: RouteSolution) -> RunStatus:
    """The stored run status, taken from the solution's own status - never a second judgement."""
    if solution.status is SolutionStatus.OK:
        return RunStatus.OK
    if solution.status is SolutionStatus.HAS_INFEASIBLE_WINDOWS:
        return RunStatus.HAS_INFEASIBLE_WINDOWS
    return RunStatus.UNRESOLVED_FIRST_STOP


def _measurement_for(solution: RouteSolution, *, run_id: str) -> OptimizationRunMetrics:
    """The stored metrics of the recorded route, with both real baselines (D22/D38).

    All three routes are the engine's **own** objects: the committed route and the two baselines the
    :class:`~core.model.solution.RouteSolution` already distinguishes - the **user** baseline (the
    driver's BEFORE order, labelled ``BaselineKind.USER_SUPPLIED``) and the **algorithm** baseline
    (the labelled greedy seed).

    Nothing is re-derived here, and in particular the user baseline is **not** re-evaluated. The
    service may have been configured with a travel matrix that is not the demo fixture, so recomputing
    the plan's BEFORE order against ``demo_matrix()`` would produce numbers the engine never produced:
    the stored run would then disagree with the route payload of the same selection (and, for any
    non-demo matrix, the recomputation would differ and fail the append outright). The engine's
    baseline is the authoritative one.
    """
    if solution.algorithm_baseline is None:  # pragma: no cover - the solver always supplies it
        raise StorageError(
            f"run {run_id!r} cannot be recorded: the committed route carries no algorithm baseline, "
            "and the approved schema stores both baselines with every run (D22/D38)"
        )
    if solution.user_baseline is None:  # pragma: no cover - the solver always supplies it
        raise StorageError(
            f"run {run_id!r} cannot be recorded: the committed route carries no user baseline, and "
            "the approved schema stores both baselines with every run (D22/D38)"
        )
    return OptimizationRunMetrics.of(
        user_baseline=solution.user_baseline,
        algorithm_baseline=solution.algorithm_baseline,
        after=solution.metrics,
    )


def _recorded_recommendation(
    report: FirstStopEvaluationReport, *, ranked: tuple[FirstStopCandidate, ...]
) -> OptimizationRunRecommendation:
    """What the run recorded as the recommendation the engine showed (D32).

    The row records **one** list - the engine's own ``ranked`` candidates, passed in here so the
    recommendation and the run's ``top_k`` are provably built from the same list - for both facts it
    stores: ``recommended_stop_id`` is the engine's recommendation (``ranked[0]``) and ``top_k`` is
    that list's head. A row can therefore never claim a ``recommended_stop_id`` that is not
    ``top_k[0]``.

    The run's committed ``order`` may legitimately start at a **different** stop: the driver decides
    (D6/D7/D32), and when they override the engine's ranking both facts belong in the row - what was
    recommended, and what was committed. Recording the engine's own ranking is the honest history;
    rewriting the ranking to match the driver's choice would claim the engine showed something it
    did not. Nothing here selects, pins or applies anything: a recommendation stored in a run is
    history, never plan state (D4/D11/D32).

    The engine's own rules supply what the record needs: ``ranked`` is non-empty exactly when the
    status is ``recommended`` (and then ``ranked[0]`` is the recommendation) and empty otherwise, so
    no status is ever invented here; and every ranked candidate is an enabled stop, while the
    committed route visits exactly the enabled stops, so every ranked id is also part of ``order``.
    """
    return OptimizationRunRecommendation(
        status=report.status,
        recommended_stop_id=report.recommended_stop_id,
        ranked_stop_ids=tuple(candidate.stop_id for candidate in ranked),
        resolved_at=report.resolved_at,
        inputs_fingerprint=report.inputs_fingerprint,
        diagnostics=report.diagnostics,
    )


def build_run(
    *,
    plan: RoutePlan,
    solution: RouteSolution,
    report: FirstStopEvaluationReport,
    run_kind: RunKind,
    run_id: str,
    created_at_utc: datetime,
) -> OptimizationRun:
    """The immutable run row of one recalculation (U11/D38), built from the engine's own objects.

    Every stored figure comes from the engine: the order, the violations, the metrics with both
    baselines (via :func:`_measurement_for`), the recommendation payload the engine showed and its
    top-K head, both taken from the engine's single ranked list (via
    :func:`_recorded_recommendation`). ``created_at_utc`` is **when the row was created**, read from
    the caller's clock seam (:meth:`RouteService._created_at_utc`) - never the plan's
    ``departure_time``, which is the plan's own fact and means something different.
    ``tzdata_version`` is the version actually reported by the environment's IANA database (``None``
    when no database is reachable - never invented).
    """
    ranked = report.ranked
    return OptimizationRun(
        id=RunId(run_id),
        plan_id=PlanId(plan.id),
        run_kind=run_kind,
        algorithm=ALGORITHM_NAME,
        algorithm_version=ALGORITHM_VERSION,
        inputs_fingerprint=solution.inputs_fingerprint,
        route_fingerprint=route_fingerprint(plan, solution.order),
        tzdata_version=tzdata.tzdata_version(),
        cost_policy=plan.cost_policy,
        data_provenance=solution.provenance,
        status=_run_status_for(solution),
        order=solution.order,
        recommendation=_recorded_recommendation(report, ranked=ranked),
        violations=solution.violations,
        metrics=_measurement_for(solution, run_id=run_id),
        created_at_utc=created_at_utc,
        top_k=ranked[:MAX_RANKED_RECOMMENDATION_CANDIDATES] or None,
    )


def _find_run(
    repository: RouteOptimizationRunRepository, *, plan_id: str, run_id: RunId
) -> OptimizationRun:
    """The run just appended, read back through the repository (history is the storage's answer).

    The caller holds the plan's single-flight lock, so no other run of this plan can be appended
    between the append and this read: ``latest`` is exactly the row that was just written, read back
    through the storage adapter with every stored payload re-validated.
    """
    run = repository.latest(PlanId(plan_id))
    if run is None or run.id != run_id:  # pragma: no cover - a vanished append is a storage fault
        raise StorageError(
            f"run {run_id!r} was appended for plan {plan_id!r} but could not be read back from the "
            "run history"
        )
    return run


def _find_run_by_id(state: DatabaseState, run_id: str) -> OptimizationRun:
    """One run by its own id, read through the repositories (404 when no run has it).

    The approved run port reads history **by plan** and the approved schema has no run-id lookup
    outside the primary key, so this transport asks the store instead of issuing SQL of its own:
    the SQLite adapter of this build offers ``RunRepository.get(run_id)``, and a repository that
    does not is read through the plans it belongs to, which is the port's own vocabulary. A missing
    run is :class:`UnknownRun` (404): absence is a normal answer for "the run with this id", not a
    corruption (D26).
    """
    with state.connection() as connection:
        run_repository = state.run_repository(connection)
        loader = getattr(run_repository, "get", None)
        if loader is not None:
            run = loader(RunId(run_id))
        else:
            plan_repository = state.plan_repository(connection)
            run = next(
                (
                    stored
                    for plan in plan_repository.list()
                    for stored in run_repository.list_for_plan(PlanId(plan.id))
                    if stored.id == run_id
                ),
                None,
            )
    if run is None:
        raise UnknownRun(f"no stored optimization run has id {run_id!r}")
    return run


# --------------------------------------------------------------------------- #
# settings: the app_settings store, including the approved tile keys (D15)
# --------------------------------------------------------------------------- #
class SettingsService:
    """Read and write whole JSON settings values by key.

    The store owns no setting's *meaning* (D38 / schema section 6): this service validates that the
    key is usable and that the value is storable JSON, and invents no default. The approved tile
    configuration keys of D15 (``tile_url``, ``tile_attribution``, ``tile_max_zoom``) are
    documented by :data:`KNOWN_SETTING_KEYS` so the UI can find them; the store accepts any key.
    """

    def __init__(self, state: DatabaseState) -> None:
        self._state = state

    def get_setting(self, key: str) -> AppSetting:
        """The stored value of ``key``.

        Raises :class:`NotFound` when the store has no value for the key: the repository answers
        ``None`` for an absent key, which is a real answer, not an error - and inventing a default
        here would be a product decision taken in a transport.

        Documented boundary: ``AppSettingsRepository.get`` returns ``None`` for *both* an absent
        key and a key whose stored JSON value is literally ``null``, so a stored ``null`` is
        reported here as "no value" (404). The API does not pretend to tell those two apart; if the
        product ever needs a storable, distinguishable null, the store's port has to say which it
        is, and that is a storage-contract change rather than a transport decision.
        """
        identifier = _validate_setting_key(key)
        with self._state.connection() as connection:
            value = self._state.settings_repository(connection).get(identifier)
        if value is None:
            raise NotFound(
                f"no value is stored for settings key {identifier!r}; this API reports an unset "
                "setting rather than substituting a default"
            )
        return AppSetting(key=identifier, value=value, configured=True)

    def set_setting(self, key: str, body: dict[str, Any] | None) -> AppSetting:
        """Store the whole JSON ``value`` under ``key`` (upsert).

        Raises:
            InvalidInput: the body is not an object, or carries no ``value`` field (422). The
                ``value`` field must be present explicitly, even when it is ``null``: "store JSON
                null" and "you forgot the field" are different requests. Note the documented
                boundary on :meth:`get_setting`: a stored ``null`` reads back as "no value",
                because the settings port returns ``None`` for both.
        """
        identifier = _validate_setting_key(key)
        request = _require_object(body, "request body")
        unexpected = sorted(set(request) - {"value"})
        if unexpected:
            raise InvalidInput(
                "PUT /api/settings/{key} accepts only a 'value' field; unexpected field(s): "
                f"{', '.join(unexpected)}"
            )
        if "value" not in request:
            raise InvalidInput(
                "PUT /api/settings/{key} needs a 'value' field (use null to store JSON null)"
            )
        value = request["value"]
        with self._state.connection() as connection:
            self._state.settings_repository(connection).set(identifier, value)
        return AppSetting(key=identifier, value=value, configured=True)

    def effective_value(self, key: str) -> Any:
        """The stored value of ``key``, or ``None`` when nothing is stored for it (U15).

        This is the read-only seam the map configuration is assembled through
        (:func:`api.map_configuration.configured_map_configuration`): unlike
        :meth:`get_setting` it does **not** raise for an unset key, because "no value is stored"
        is a legitimate answer there - the map configuration resolves it to the documented default
        and reports that the key was defaulted rather than configured. It invents nothing itself:
        an unset key answers ``None``, and a stored JSON ``null`` is indistinguishable from an
        unset key exactly as the store's own contract documents.
        """
        identifier = _validate_setting_key(key)
        with self._state.connection() as connection:
            return self._state.settings_repository(connection).get(identifier)

    def map_configuration(self) -> MapConfiguration:
        """The map configuration in force, read from ``app_settings`` with documented defaults.

        Configuration isolation (D15/D39(c)) reaches the browser through this read: the UI asks the
        API for the tile URL, attribution, max zoom and map-library URL and hardcodes none of them.
        """
        return configured_map_configuration(self.effective_value)


# --------------------------------------------------------------------------- #
# the engine-facing surface: recommendation, selection, route, run history (U14)
# --------------------------------------------------------------------------- #
class _EngineService:
    """Shared plumbing of the engine-facing services: plan access and the single-flight lock.

    It owns no formula: it loads plans, takes the plan's lock and hands the domain objects to the
    real engine. Every route mode guard is the domain's own (``RouteMode`` + ``ROUTE_MODE_STATUS``),
    never a second registry maintained here.
    """

    def __init__(
        self,
        state: DatabaseState,
        travel_matrix: TravelMatrix | Callable[[], TravelMatrix] | None = None,
        *,
        lock_timeout_seconds: float = PLAN_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._state = state
        self._matrix_source = demo_matrix if travel_matrix is None else travel_matrix
        self._lock_timeout_seconds = float(lock_timeout_seconds)

    # -- travel matrix --------------------------------------------------- #
    def _matrix(self) -> TravelMatrix:
        """The travel matrix this build uses: the deterministic DEMO/SYNTHETIC matrix.

        One instance per computation, so every leg of a computation is priced by one provider; the
        matrix is passed explicitly so a test can supply an equivalent provider without touching the
        engine.
        """
        source = self._matrix_source
        return source() if callable(source) else source

    # -- locking --------------------------------------------------------- #
    def plan_lock(self, plan_id: str):
        """The process-wide single-flight lock of ``plan_id`` (used by the transport's tests too)."""
        return PLAN_LOCKS.lock_for(plan_id)

    @contextmanager
    def _single_flight(self, plan_id: str) -> Iterator[None]:
        with PLAN_LOCKS.held(plan_id, timeout_seconds=self._lock_timeout_seconds):
            yield

    # -- plans ----------------------------------------------------------- #
    def _load(self, connection: sqlite3.Connection, plan_id: str) -> RoutePlan:
        """The stored plan, or :class:`UnknownPlan` when no plan has that id (404)."""
        identifier = _validate_plan_id(plan_id)
        plan = self._state.plan_repository(connection).get(PlanId(identifier))
        if plan is None:
            raise UnknownPlan(f"no stored plan has id {identifier!r}")
        return plan

    @staticmethod
    def _require_implemented_route_mode(plan: RoutePlan) -> None:
        """The plan's route mode must be the one this build implements (501 otherwise, D19).

        A plan this API can create is always ``SMART_ROUTE``; the guard exists so a stored row from
        another build is refused loudly instead of being routed under an objective it never asked
        for.
        """
        if plan.route_mode is not IMPLEMENTED_ROUTE_MODE:
            raise CapabilityNotImplemented(
                f"plan {plan.id!r} has route_mode {plan.route_mode.value!r}, which is declared in "
                f"the domain but not implemented (status={ROUTE_MODE_STATUS[plan.route_mode]!r}); "
                f"this build produces routes for {IMPLEMENTED_ROUTE_MODE.value} only and never "
                "falls back to it silently (D19)"
            )


class RecommendationService(_EngineService):
    """The exhaustive first-stop recommendation, as an advisory answer (U14; v2 sections 12-14)."""

    def recommend(self, plan_id: str, *, matrix: TravelMatrix | None = None) -> RecommendationResult:
        """Recompute the recommendation for ``plan_id`` and return the engine's own report.

        The whole body runs under the plan's single-flight lock: the exhaustive loop runs once for
        one plan at a time, and a caller that cannot take the lock inside the documented bound gets
        :class:`PlanBusy` rather than a second concurrent exhaustive run. Nothing is written: the
        plan, its first-stop state and the run history are all unchanged by this call (owner
        decision 5), because a recommendation is derived and recomputable and is never plan state
        (D4/D11/D32).

        Raises:
            UnknownPlan: no stored plan has that id (404).
            CapabilityNotImplemented: the plan's route mode is not ``SMART_ROUTE`` (501, D19).
            PlanBusy: the plan is already computing another recommendation or route (409).
        """
        with self._single_flight(plan_id):
            with self._state.connection() as connection:
                plan = self._load(connection, plan_id)
                self._require_implemented_route_mode(plan)
            legs = matrix if matrix is not None else self._matrix()
            started = time.perf_counter()
            report = evaluate_first_stop_candidates(plan=plan, travel_matrix=legs)
            elapsed = time.perf_counter() - started
        return RecommendationResult(
            plan=plan, report=report, computation_seconds=_measured_seconds(elapsed)
        )


class SelectionService(_EngineService):
    """The driver's first-stop decision, applied through the domain and persisted (U14; D4-D11/D32).

    The state machine this service enforces, and nothing else:

    ==============================  ==========  ==========================  ========  ===========================
    request                         mode        selection_source            pinned    state
    ==============================  ==========  ==========================  ========  ===========================
    ``mode=recommend``, accepted    ``recommend``  ``accepted_recommendation``  ``True``   ``first_stop_selected``
    ``mode=manual``                 ``manual``     ``manual_choice``            ``True``   ``first_stop_selected``
    ``DELETE`` (cancel / unpin)     unchanged      ``None``                     ``False``  ``awaiting_first_stop_choice``
    ==============================  ==========  ==========================  ========  ===========================

    Refused loudly (never repaired silently): an unknown stop id, a disabled stop, an illegal
    transition (accepting a recommendation that is not the engine's recommended stop, or accepting
    when nothing is recommended), and a mode/source mismatch. A recommendation is never stored as
    the plan's selection, and this service never writes a selection the driver did not make.
    """

    #: Fields ``POST /api/plans/{id}/selection`` accepts. A body carrying anything else is refused,
    #: so an unimplemented request can never be silently ignored (D16).
    ACCEPTED_FIELDS = frozenset({"mode", "stop_id"})

    def __init__(
        self,
        state: DatabaseState,
        travel_matrix: TravelMatrix | Callable[[], TravelMatrix] | None = None,
        *,
        lock_timeout_seconds: float = PLAN_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(state, travel_matrix, lock_timeout_seconds=lock_timeout_seconds)
        #: The last recommendation this service computed, for the accept verification only: the
        #: engine is deterministic for one set of inputs, so re-verifying an unchanged board would
        #: only repeat an exhaustive loop the same process just paid for. Keyed by the plan's own
        #: ``inputs_fingerprint`` and holding exactly one entry, so a changed plan always
        #: recomputes and the memo can never serve a stale answer.
        self._last_report: tuple[str, FirstStopEvaluationReport] | None = None

    def _verification_report(self, plan: RoutePlan) -> FirstStopEvaluationReport:
        fingerprint = plan.inputs_fingerprint()
        if self._last_report is not None and self._last_report[0] == fingerprint:
            return self._last_report[1]
        report = evaluate_first_stop_candidates(plan=plan, travel_matrix=self._matrix())
        self._last_report = (fingerprint, report)
        return report

    def select_first_stop(
        self, plan_id: str, mode: Any, stop_id: Any
    ) -> SelectionResult:
        """Apply the driver's choice ``(mode, stop_id)`` and persist it through the domain.

        ``mode`` is the documented vocabulary of :class:`~core.model.first_stop.FirstStopMode`
        (``recommend`` / ``manual`` / ``accept``); ``accept`` is the shorthand for "the driver
        pressed start-from-this-stop on the recommendation" and is equivalent to
        ``mode=recommend`` with the engine's current recommended stop.

        Raises:
            UnknownPlan: no stored plan has that id (404).
            UnknownStop: the plan has no stop with that id (404).
            InvalidInput: the mode is unknown, the stop is disabled, or the mode/source combination
                is not one the domain accepts (422).
            Conflict: the transition is illegal in the plan's current state - accepting something
                that is not the current recommendation, or when nothing is recommended (409).
            CapabilityNotImplemented: the plan's route mode is not ``SMART_ROUTE`` (501, D19).
        """
        requested = _require_first_stop_mode(mode)
        identifier = _validate_plan_id(plan_id)
        candidate_stop = _require_str(stop_id, "stop_id")
        with self._single_flight(identifier):
            with self._state.connection() as connection:
                repository = self._state.plan_repository(connection)
                plan = self._load(connection, identifier)
                self._require_implemented_route_mode(plan)

                previous = plan.first_service_stop
                stop = _find_stop(plan, candidate_stop, plan_id=identifier)
                if not stop.enabled:
                    raise InvalidInput(
                        f"stop {candidate_stop!r} is disabled, so it cannot be the first service "
                        "stop: the driver's first stop must be a real, enabled stop (D20/D32). "
                        "Restore the stop before choosing it."
                    )
                intent = self._intent_for(plan, requested, candidate_stop)
                new_plan = replace(
                    plan,
                    first_service_stop=intent,
                    order_overrides=plan.order_overrides,
                )
                repository.save(new_plan)
                stored = repository.get(PlanId(identifier))
                if stored is None:  # pragma: no cover - a save that vanished is a storage fault
                    raise StorageError(
                        f"plan {identifier!r} could not be read back after the selection was saved"
                    )
            return SelectionResult(
                plan=stored,
                plan_id=identifier,
                previous_state=plan.first_stop_state,
                previous_stop_id=previous.selected_stop_id,
            )

    def clear_first_stop(self, plan_id: str) -> SelectionResult:
        """Cancel / unpin: back to ``awaiting_first_stop_choice`` with a null stop and source.

        The plan keeps its mode (a driver who was in RECOMMEND mode stays in RECOMMEND mode) and
        gets no substitute stop: the domain never chooses on the driver's behalf (D8/D9).

        Raises:
            UnknownPlan: no stored plan has that id (404).
            Conflict: nothing was selected, so there is nothing to cancel (409).
            CapabilityNotImplemented: the plan's route mode is not ``SMART_ROUTE`` (501, D19).
        """
        identifier = _validate_plan_id(plan_id)
        with self._single_flight(identifier):
            with self._state.connection() as connection:
                repository = self._state.plan_repository(connection)
                plan = self._load(connection, identifier)
                self._require_implemented_route_mode(plan)
                previous = plan.first_service_stop
                if not previous.has_selection:
                    raise Conflict(
                        f"plan {identifier!r} is already awaiting_first_stop_choice: there is no "
                        "selection to cancel, and this API does not treat a no-op as a change (D8)"
                    )
                new_plan = replace(
                    plan,
                    first_service_stop=previous.cleared(),
                    order_overrides=plan.order_overrides,
                )
                repository.save(new_plan)
                stored = repository.get(PlanId(identifier))
                if stored is None:  # pragma: no cover - a save that vanished is a storage fault
                    raise StorageError(
                        f"plan {identifier!r} could not be read back after the selection was cleared"
                    )
            return SelectionResult(
                plan=stored,
                plan_id=identifier,
                previous_state=plan.first_stop_state,
                previous_stop_id=previous.selected_stop_id,
            )

    # -- the state machine ----------------------------------------------- #
    def _intent_for(
        self, plan: RoutePlan, requested: str, stop_id: str
    ) -> FirstStopIntent:
        """The domain intent this request describes, or a loud refusal.

        The engine recommends; the driver decides (D4/D32). "Accept the recommendation" is only
        legal for the stop the engine currently recommends: this service recomputes the
        recommendation and, when it can be recomputed, requires the accepted stop to be the
        recommended one (and not a rejected candidate, v2 section 14). Acceptance is deliberately a
        **one-time** verification, not a later constraint: once the driver has chosen, a stop they
        committed stays chosen (I3), and re-checking a stored decision against a fresh
        recommendation is exactly the silent repair D5/D32 forbid.
        """
        if requested == "accept":
            self._require_accepts_the_recommendation(plan, stop_id)
            return FirstStopIntent.accepted_recommendation(stop_id)
        if requested == FirstStopMode.RECOMMEND.value:
            self._require_accepts_the_recommendation(plan, stop_id)
            return FirstStopIntent(
                FirstStopMode.RECOMMEND,
                stop_id,
                SelectionSource.ACCEPTED_RECOMMENDATION,
                True,
            )
        # ``manual``: the driver chose a stop themselves. A manual choice is always
        # ``manual_choice``, exactly as D6/D7 require, and FirstStopIntent rejects any other
        # combination rather than normalising it.
        return FirstStopIntent.manual_choice(stop_id, mode=FirstStopMode.MANUAL)

    def _require_accepts_the_recommendation(self, plan: RoutePlan, stop_id: str) -> None:
        """The stop must be the engine's current recommendation (D32, v2 section 14).

        The check runs the real engine once, inside the caller's single-flight lock, and refuses
        when nothing is recommended or when the named stop is not the recommended one - including
        the case where the stop was evaluated and **rejected** as infeasible, which is never
        presented as an accepted recommendation (v2 section 14).
        """
        report = self._verification_report(plan)
        recommended = report.recommended_stop_id
        if recommended is None:
            raise Conflict(
                f"plan {plan.id!r} has no recommendation to accept: the engine's outcome is "
                f"{report.status.value!r}, so there is no fully feasible first stop to accept. "
                "Choose a stop manually instead (v2 section 14, D9)."
            )
        if recommended != stop_id:
            rejected = stop_id in {candidate.stop_id for candidate in report.rejected}
            detail = (
                "it was evaluated and REJECTED because its complete route misses a hard window, so "
                "it is never presented as an accepted recommendation (v2 section 14)"
                if rejected
                else "it is not the stop the engine recommends"
            )
            raise Conflict(
                f"stop {stop_id!r} cannot be accepted as the recommendation: the engine recommends "
                f"{recommended!r} and {detail}. Send mode='manual' to choose another first stop, "
                "or accept the recommended one (D32)."
            )


class RouteService(_EngineService):
    """The committed route and the immutable run history (U14; v2 sections 12/15, D22, D38)."""

    def __init__(
        self,
        state: DatabaseState,
        travel_matrix: TravelMatrix | Callable[[], TravelMatrix] | None = None,
        *,
        lock_timeout_seconds: float = PLAN_LOCK_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(state, travel_matrix, lock_timeout_seconds=lock_timeout_seconds)
        #: The clock a recorded run's ``created_at_utc`` comes from. It mirrors the injectable seam
        #: of ``storage.sqlite.route_plan_repository``: production reads the real UTC wall clock and
        #: a test pins the instant, so "when was this row created" is both honest and testable.
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def _created_at_utc(self) -> datetime:
        """The UTC instant this run row is created, in the storage timestamp convention.

        ``created_at_utc`` means **when the row was created** - the plan's ``departure_time`` is a
        different fact and stays where it belongs. The instant is written through
        :func:`storage.sqlite.database.utc_now_iso`, the same whole-second UTC helper the plan
        repository stamps its own rows with, and is parsed back from that text so the value held here
        is exactly the whole-second instant that will be stored (the run repository refuses a
        sub-second ``created_at_utc`` rather than truncating it silently).
        """
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise StorageError(
                "the run clock must return a timezone-aware datetime (storage stores UTC only), "
                f"got {moment!r}"
            )
        return datetime.strptime(utc_now_iso(moment), _UTC_Z_FORMAT).replace(tzinfo=timezone.utc)

    def committed_route(
        self, plan_id: str, *, matrix: TravelMatrix | None = None
    ) -> RouteResult:
        """Compute the committed complete route for the plan's **current** selection.

        The route is produced by the real optimizer through the existing solve boundary
        (:func:`core.engine.optimizer.solve.solve_route`): the same pipeline that priced the
        candidate the driver accepted (I6). Nothing is written: a route request never appends a run
        row (owner decision 5) and never changes the plan.

        Raises:
            UnknownPlan: no stored plan has that id (404).
            NoFirstStopSelected: the plan is still ``awaiting_first_stop_choice`` (409, I4/D9) - the
                honest answer instead of an invented route.
            CapabilityNotImplemented: the plan's route mode is not ``SMART_ROUTE`` (501, D19).
            PlanBusy: the plan is already computing (409).
        """
        with self._single_flight(plan_id):
            with self._state.connection() as connection:
                plan = self._load(connection, plan_id)
                self._require_implemented_route_mode(plan)
            return self._compute_route(plan, matrix=matrix)

    def optimize_and_record(self, plan_id: str) -> RunResult:
        """Recalculate the committed route AND append exactly one immutable run row.

        This is the only endpoint that writes history (owner decision 5, D39(e)). The run kind is
        ``optimize`` for the plan's **first** recorded run and ``reoptimize`` for every later
        recalculation of the same plan - the documented rule: the first row records the initial
        optimization, and every subsequent recalculation is a reoptimization of the same plan. The
        kind is read from the stored history inside the same lock, so two concurrent requests cannot
        both claim to be the first run.

        The row records the engine's own objects: both fingerprints, the tzdata version actually in
        use, the cost policy actually used, the order, the recommendation payload that was shown
        (**history**, never plan state), the top-K candidates, the explicit violations and the
        metrics with both baselines (U11/D38). Nothing here recomputes a metric.

        Raises:
            UnknownPlan: no stored plan has that id (404).
            NoFirstStopSelected: the plan is still ``awaiting_first_stop_choice`` (409, I4/D9).
            CapabilityNotImplemented: the plan's route mode is not ``SMART_ROUTE`` (501, D19).
            PlanBusy: the plan is already computing (409).
        """
        with self._single_flight(plan_id):
            legs = self._matrix()
            with self._state.connection() as connection:
                repository = self._state.plan_repository(connection)
                run_repository = self._state.run_repository(connection)
                plan = self._load(connection, plan_id)
                self._require_implemented_route_mode(plan)
                _require_committed_route_is_possible(plan)

                recommendation_started = time.perf_counter()
                report = evaluate_first_stop_candidates(plan=plan, travel_matrix=legs)
                recommendation_seconds = (
                    time.perf_counter() - recommendation_started
                )

                route_started = time.perf_counter()
                result = self._compute_route(plan, matrix=legs, measured=False)
                route_seconds = time.perf_counter() - route_started

                existing = run_repository.list_for_plan(PlanId(plan.id))
                sequence = len(existing)
                run_kind = RunKind.OPTIMIZE if sequence == 0 else RunKind.REOPTIMIZE
                run = build_run(
                    plan=plan,
                    solution=result.solution,
                    report=report,
                    run_kind=run_kind,
                    run_id=run_id_for(
                        plan_id=plan.id,
                        run_kind=run_kind,
                        route_fingerprint=result.route_fingerprint,
                        sequence=sequence,
                    ),
                    created_at_utc=self._created_at_utc(),
                )
                run_repository.append(run)
                stored = _find_run(
                    run_repository, plan_id=plan.id, run_id=run.id
                )
            return RunResult(
                plan=plan,
                solution=result.solution,
                recommendation_report=report,
                route_fingerprint=result.route_fingerprint,
                run=stored,
                recommendation_seconds=_measured_seconds(recommendation_seconds),
                route_seconds=_measured_seconds(route_seconds),
                computation_seconds=_measured_seconds(
                    recommendation_seconds + route_seconds
                ),
            )

    def list_runs(self, plan_id: str) -> tuple[OptimizationRun, ...]:
        """Every stored run of ``plan_id``, oldest first - read-only, no lock, no write.

        Reading history never needs the computation lock (it runs no engine work) and never appends
        anything: the repository's documented order is the answer.
        """
        identifier = _validate_plan_id(plan_id)
        with self._state.connection() as connection:
            run_repository = self._state.run_repository(connection)
            plan = self._state.plan_repository(connection).get(PlanId(identifier))
            if plan is None:
                raise UnknownPlan(f"no stored plan has id {identifier!r}")
            return tuple(run_repository.list_for_plan(PlanId(identifier)))

    def get_run(self, run_id: str) -> OptimizationRun:
        """One stored run by its own id (404 when no run has it) - read-only, no lock, no write."""
        identifier = _require_str(run_id, "run id")
        return _find_run_by_id(self._state, identifier)

    # -- the one route computation both endpoints share ------------------- #
    def _compute_route(
        self, plan: RoutePlan, *, matrix: TravelMatrix | None = None, measured: bool = True
    ) -> RouteResult:
        """Run the real optimizer for ``plan``'s selection and fingerprint the committed route.

        The caller holds the plan's single-flight lock. The engine's ``RouteSolution`` is returned
        unchanged; ``computation_seconds`` is the measured duration of the engine work only (a
        caller inside :meth:`optimize_and_record` measures its own phases instead). No matrix
        fingerprint is carried: ``core`` computes none for the configured travel matrix, and this
        layer invents no metric (see :class:`RouteResult`).
        """
        _require_committed_route_is_possible(plan)
        legs = matrix if matrix is not None else self._matrix()
        started = time.perf_counter()
        solution = solve_route(plan=plan, travel_matrix=legs)
        elapsed = time.perf_counter() - started
        fingerprint = route_fingerprint(plan, solution.order)
        return RouteResult(
            plan=plan,
            solution=solution,
            route_fingerprint=fingerprint,
            computation_seconds=_measured_seconds(elapsed) if measured else 0,
            route_seconds=_measured_seconds(elapsed),
        )


# --------------------------------------------------------------------------- #
# the container the transport talks to
# --------------------------------------------------------------------------- #
class ApiServices:
    """Everything a transport needs: the database state and the service objects.

    ``travel_matrix`` and ``clock`` are the two configuration seams of this container: the matrix is
    the provider every engine call is priced with (the DEMO/SYNTHETIC fixture unless one is
    injected), and the clock is what a recorded run's ``created_at_utc`` is read from. Both default
    to production behaviour and mirror the injection seams of the storage repositories.
    """

    def __init__(
        self,
        identifier: str | Path = DEFAULT_DB_PATH,
        *,
        plan_repository_factory: _PlanRepositoryFactory | None = None,
        settings_repository_factory: _SettingsRepositoryFactory | None = None,
        run_repository_factory: _RunRepositoryFactory | None = None,
        travel_matrix: TravelMatrix | Callable[[], TravelMatrix] | None = None,
        plan_lock_timeout_seconds: float = PLAN_LOCK_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = DatabaseState(
            identifier,
            plan_repository_factory=plan_repository_factory,
            settings_repository_factory=settings_repository_factory,
            run_repository_factory=run_repository_factory,
            plan_lock_timeout_seconds=plan_lock_timeout_seconds,
        )
        self.plan_lock_timeout_seconds = float(plan_lock_timeout_seconds)
        self.plans = PlanService(self.state)
        self.settings = SettingsService(self.state)
        self.recommendations = RecommendationService(
            self.state, travel_matrix, lock_timeout_seconds=self.plan_lock_timeout_seconds
        )
        self.selections = SelectionService(
            self.state, travel_matrix, lock_timeout_seconds=self.plan_lock_timeout_seconds
        )
        self.routes = RouteService(
            self.state,
            travel_matrix,
            lock_timeout_seconds=self.plan_lock_timeout_seconds,
            clock=clock,
        )

    def close(self) -> None:
        self.state.close()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_object(body: Any, what: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise InvalidInput(
            f"{what} must be a JSON object, got {type(body).__name__}; a body that is not a JSON "
            "object is refused rather than coerced"
        )
    return body


def _validate_setting_key(key: str) -> str:
    candidate = _require_str(key, "settings key")
    if len(candidate) > _MAX_ID_LENGTH or any(character.isspace() for character in candidate):
        raise InvalidInput(
            f"settings key {candidate!r} must not contain whitespace and must be at most "
            f"{_MAX_ID_LENGTH} characters"
        )
    if any(character in candidate for character in ("/", "\\", "?", "#")):
        raise InvalidInput(
            f"settings key {candidate!r} must be a single URL path segment (no /, \\, ? or #)"
        )
    return candidate


def _provenance_of(repository: RoutePlanRepository) -> DataProvenance:
    """The provenance of the plans this API reads.

    The domain :class:`~core.model.route_plan.RoutePlan` does not carry provenance, and the
    storage adapter keeps the value it was constructed with privately, so provenance is decided in
    one place: :meth:`DatabaseState.plan_repository` always constructs the repository with
    ``DEMO_SYNTHETIC``, because every plan this API can serve comes from the deterministic demo
    fixture. A repository that does expose ``data_provenance`` publicly (a test double, or a future
    real-data adapter) is believed instead of assumed, so this API can never report synthetic data
    as real routing or the other way round (D15/D23).
    """
    provenance = getattr(repository, "data_provenance", None)
    if provenance is None:
        return DEMO_PROVENANCE
    return provenance if isinstance(provenance, DataProvenance) else DataProvenance(provenance)


# Re-exported domain enums, so a transport can name the values it validates without importing
# ``core`` itself. They are documentation, not logic.
FIRST_STOP_MODES: tuple[str, ...] = tuple(mode.value for mode in FirstStopMode)
FIRST_STOP_SOURCES: tuple[str, ...] = tuple(source.value for source in SelectionSource)
FIRST_STOP_STATES: tuple[str, ...] = tuple(state.value for state in FirstStopState)
WINDOW_KINDS: tuple[str, ...] = tuple(kind.value for kind in WindowKind)
WINDOW_END_POLICIES: tuple[str, ...] = tuple(policy.value for policy in WindowEndPolicy)
GEOCODE_STATUSES: tuple[str, ...] = tuple(status.value for status in GeocodeStatus)
SERVICE_STATUSES: tuple[str, ...] = tuple(status.value for status in ServiceStatus)
