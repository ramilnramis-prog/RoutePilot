"""Framework-agnostic application layer for the RoutePilot API (Stage 4 U13).

This module holds the application logic that the HTTP transport calls. It is deliberately **not**
an HTTP module:

* no HTTP types, no status codes, no JSON and no request/response objects appear here - the
  transport (today stdlib ``http.server``, later possibly FastAPI) owns all of that;
* failures are raised as the framework-neutral :class:`ServiceError` hierarchy below, whose names
  describe the *application* outcome (``NotFound``, ``InvalidInput``, ``CapabilityNotImplemented``,
  ...). ``api/http_server.py`` maps those names onto HTTP status codes in one documented table, so
  replacing the transport never touches this file;
* results are Python data and domain objects (:class:`PlanRecord`, :class:`AppSetting`,
  ``dict``/``list`` of plain values). ``api/serialization.py`` turns domain objects into
  JSON-ready payloads.

Two architectural rules from the architecture document are enforced by construction:

* ``api/`` may import ``core/``, ``storage/`` and the deterministic ``demo/`` fixture, and nothing
  imports ``api/`` from the inside - ``core/`` stays pure (D1, ``tests/test_core_isolation.py``);
* **no business formula lives in the transport or in this layer.** Every number the API reports is
  read from the domain objects, the storage repositories or ``core``'s own capability tables. A
  service method may validate a request and apply the approved MVP controls (``enabled`` and
  ``priority``) through the domain, and may not compute a metric of its own.

Concurrency: a ``sqlite3`` connection is not safe to share between threads, so every request gets
its **own** connection, opened from the configured database identifier through
:func:`storage.sqlite.database.connect` (which enables ``PRAGMA foreign_keys`` and verifies it).
An in-process SQLite database opened on a ``file:...?mode=memory&cache=shared`` URI would be
destroyed as soon as its last connection closes, so in that one case a keeper connection is held
open for the lifetime of the :class:`DatabaseState` (see :meth:`DatabaseState.close`). A file-backed
database holds no keeper connection at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from core.engine.providers import ProviderCapabilities
from core.model.cost_policy import CostComponent, default_component_declarations
from core.model.first_stop import FirstStopMode, FirstStopState, SelectionSource
from core.model.ids import PlanId
from core.model.route_mode import ROUTE_MODE_STATUS, RouteMode
from core.model.route_stop import GeocodeStatus, RouteStop, ServiceStatus
from core.model.service_window import WindowEndPolicy, WindowKind
from core.model.value_objects import DataProvenance
from core.repositories import AppSettingsRepository, RoutePlanRepository
from core.time import tzdata
from core.validation.errors import TZDATA_INSTALL_COMMAND, RoutePilotError
from demo.dataset import DEMO_PLAN_ID, build_demo_plan, demo_warning_text
from storage import StorageError
from storage.sqlite.app_settings_repository import SqliteAppSettingsRepository
from storage.sqlite.database import SCHEMA_VERSION, connect, current_version, migrate
from storage.sqlite.route_plan_repository import SqliteRoutePlanRepository

__all__ = [
    "AppSetting",
    "ApiServices",
    "CapabilityNotImplemented",
    "Conflict",
    "DatabaseState",
    "InvalidInput",
    "NotFound",
    "PlanRecord",
    "PlanService",
    "ServiceError",
    "SettingsService",
    "capability_report",
    "health_payload",
]

#: The one route mode this build implements (D19: the MVP implements SMART_ROUTE only).
IMPLEMENTED_ROUTE_MODE = RouteMode.SMART_ROUTE

#: Settings keys this API understands. The store itself owns no key semantics (D38 / schema
#: section 6), so this list documents the keys the demo surface uses - including the approved map
#: tile keys of D15 - and does not restrict what may be stored.
KNOWN_SETTING_KEYS: tuple[str, ...] = (
    "tile_url",
    "tile_attribution",
    "tile_max_zoom",
)

#: Default database identifier used by ``python -m api.serve``. ``var/`` is gitignored, so no
#: database artifact can be committed (`.gitignore` section "local databases").
DEFAULT_DB_PATH = "var/routepilot.db"

#: The provenance of every plan this API can serve: the deterministic demo fixture is
#: DEMO/SYNTHETIC, and the plan repository is configured with exactly this value (D15/D23).
DEMO_PROVENANCE = DataProvenance.DEMO_SYNTHETIC

#: Upper bound on a JSON request body the transport accepts, in bytes.
MAX_REQUEST_BODY_BYTES = 1_048_576


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


def health_payload(state: DatabaseState) -> dict[str, Any]:
    """The honest health/capability document (spec sections 23/31/33/36, D12/D16/D19).

    It reports the IANA database source and version *or* the honest fallback state, states that
    the only shipped data is the DEMO/SYNTHETIC fixture, and lists the implemented capabilities
    next to the not-implemented ones (traffic, side-of-road, turn-by-turn, geocoding, real
    routing, and every route mode except SMART_ROUTE) so a UI can be honest instead of optimistic.
    """
    implemented, not_implemented = capability_report()
    report = tzdata.probe_tzdata()
    return {
        "status": "ok",
        "api_version": "1",
        "read_only_units": (
            "U13 ships the read/config surface only: no recommendation, selection, route or run "
            "endpoints yet (U14), and no web UI yet (U15)"
        ),
        "timezone_data": _tzdata_payload(),
        "demo_data": {
            "present": True,
            "provenance": DataProvenance.DEMO_SYNTHETIC.value,
            "warning": demo_warning_text(),
            "labelled": ["DEMO", "SYNTHETIC"],
        },
        "data_provenance": DataProvenance.DEMO_SYNTHETIC.value,
        "database": {
            "identifier": state.display_identifier,
            "schema_version": state.schema_version,
            "schema_target_version": SCHEMA_VERSION,
            "implementation": state.plan_repository_name,
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
        plan_repository_name: str | None = None,
    ) -> None:
        self.identifier = str(identifier)
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
        "a first-stop recommendation (the recommendation endpoint arrives in U14)",
    ),
    "route": ("unsupported_capability", "a computed route (the route endpoint arrives in U14)"),
    "runs": ("unsupported_capability", "optimization-run history (arrives in U14)"),
}

#: Per-stop control fields ``PUT /api/plans/{id}`` accepts - exactly the two MVP controls the
#: owner approved. Drag/reorder and the first-stop choice are deliberately absent (U14).
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

        Deliberately absent, because they belong to U14: drag/reorder, and any change to the
        first-stop choice. Asking for either is refused rather than half-implemented.

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
                f"unexpected field(s): {', '.join(unexpected)}. Drag/reorder and the first-stop "
                "choice arrive in U14."
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
                f"{', '.join(unknown)}. Drag/reorder and the first-stop choice arrive in U14."
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


# --------------------------------------------------------------------------- #
# the container the transport talks to
# --------------------------------------------------------------------------- #
class ApiServices:
    """Everything a transport needs: the database state and the two service objects."""

    def __init__(
        self,
        identifier: str | Path = DEFAULT_DB_PATH,
        *,
        plan_repository_factory: _PlanRepositoryFactory | None = None,
        settings_repository_factory: _SettingsRepositoryFactory | None = None,
    ) -> None:
        self.state = DatabaseState(
            identifier,
            plan_repository_factory=plan_repository_factory,
            settings_repository_factory=settings_repository_factory,
        )
        self.plans = PlanService(self.state)
        self.settings = SettingsService(self.state)

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
