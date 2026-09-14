"""SQLite ``RoutePlanRepository``: plan and stop persistence with exact round-trip (U10; D38).

Implements the pure port :class:`core.repositories.RoutePlanRepository` against the APPROVED
schema ``docs/STORAGE_SCHEMA.md`` (sections 3, 4 and 7), using stdlib ``sqlite3`` only - no ORM and
no third-party dependency. Storage depends on ``core``; ``core`` never imports this module.

What this repository promises, and what it deliberately does not do
------------------------------------------------------------------

**One transaction per save.** :meth:`SqliteRoutePlanRepository.save` upserts the ``route_plans``
row and replaces that plan's ``route_stops`` rows inside a single transaction. Because
``route_plans.first_stop_selected_stop_id`` references ``route_stops(id)``, the order inside that
transaction is: clear the plan's selection reference, upsert the plan row (selection columns NULL),
delete the plan's old stop rows, insert the new stop rows, then restore the selection reference.
Without that order a legal save of an updated plan would violate the foreign key - and a failure
anywhere in the block rolls the whole save back, so a partially written plan is never observable.

**START and FINISH are plan columns, never stop rows** (I1/I2): ``departure_label`` /
``departure_latitude`` / ``departure_longitude`` / ``departure_time_utc`` and ``finish_label`` /
``finish_latitude`` / ``finish_longitude`` carry ``plan.departure`` / ``plan.finish`` /
``plan.departure_time`` (every field of :class:`~core.model.value_objects.PlaceRef`, which is a
different type from a service stop). No code path here can insert a START or FINISH row.

**The driver's decision is stored; a recommendation never is.** ``first_stop_mode``,
``first_stop_selected_stop_id`` (NULL while ``awaiting_first_stop_choice``),
``first_stop_selection_source`` (NULL while nothing is selected) and ``first_stop_pinned`` come from
``plan.first_service_stop`` (:class:`~core.model.first_stop.FirstStopIntent`). There is no
``recommended_stop_id`` column and no recommendation is ever written as plan state (D4/D11/D32).

**Service windows stay local wall clock.** ``service_window_kind`` plus ``HH:MM:SS``
``service_window_start`` / ``service_window_end`` are stored exactly as the domain holds them -
never as resolved instants - so the window is re-resolved against the plan's IANA zone, under strict
DST validation, every time the loaded plan is evaluated (D3, schema section 1).

**``input_position`` is provenance, never route order** (D33). It is written as given, read back in
``input_position`` order, never renumbered or compacted, and gaps survive.

**Loading validates through the domain.** Every value object is rebuilt by its domain constructor,
so a hand-edited row fails loudly instead of producing a half-valid plan (D38 acceptance item 5).
The exact error types on the load path are:

===============================================  =====================================================
stored problem                                   error raised
===============================================  =====================================================
unknown but well-formed IANA zone                ``UnknownTimezoneError`` (via ``RoutePlan``)
syntactically invalid IANA zone                  ``InvalidTimezoneNameError`` (via ``RoutePlan``)
unknown cost-policy name                         ``InvalidCostPolicyError`` (registry check here)
unknown cost component in the weights            ``InvalidCostPolicyError`` (via ``RouteCostPolicy``)
weight for a non-implemented component           ``UnsupportedFeatureError`` (capability rule, D16)
unknown order-override constraint kind           ``InvalidRoutePlanError`` (via ``OrderConstraint``)
declared-but-unimplemented ``position`` kind     ``UnsupportedConstraintError`` (D16/D21)
malformed JSON / wrong JSON shape / missing key  ``StoredPlanError`` (invalid stored state)
unknown order-override envelope version          ``StoredPlanError`` (the domain models no version)
resolved instant in a wall-clock column          ``StoredPlanError``
``enabled`` that is not 0/1, bad timestamp       ``StoredPlanError``
shape-valid UTC timestamp naming no instant      ``StoredPlanError`` (calendar-invalid timestamp
                                                 in any stored column, e.g.
                                                 ``2026-02-30T01:00:00Z``; the interpreter's own
                                                 ``ValueError`` never escapes)
``data_provenance`` missing or not accepted      ``StoredPlanError``
rejected ``geocode_status`` / ``service_status`` ``InvalidRouteStopError`` (via ``RouteStop``)
negative or non-integer ``service_duration_sec`` ``InvalidRouteStopError`` (via ``RouteStop``)
fixed-window shape violations                    ``InvalidServiceWindowError`` (via ``ServiceWindow``)
inconsistent first-stop decision (D5-D7/D21)     ``InvalidRoutePlanError`` (via ``FirstStopIntent`` /
                                                 ``RoutePlan``, e.g. MANUAL with an
                                                 ``accepted_recommendation`` source, a selection
                                                 that is not an enabled stop of the plan, or a
                                                 selection whose ``selection_source`` is missing)
===============================================  =====================================================

Nothing is repaired silently and no default is substituted anywhere on the load path.

Columns the domain does not model (Deliverable 4) - handled honestly, never invented
------------------------------------------------------------------------------------

``route_plans.name``
    has no counterpart on :class:`~core.model.route_plan.RoutePlan` and is written as ``NULL`` on
    every save (including updates, so a name written by another tool is not preserved through this
    repository).

``route_plans.data_provenance``
    is ``NOT NULL`` in the schema but is **not** carried by the domain, which is exactly why it is
    required explicitly at construction: ``SqliteRoutePlanRepository(connection,
    data_provenance=DataProvenance.DEMO_SYNTHETIC)``. The configured value is written on save, and
    on load the stored value must be a real :class:`~core.model.value_objects.DataProvenance` member
    **and** the same value this repository was constructed with. The second half is a deliberate
    honesty guard: ``get`` cannot return the provenance (the domain has nowhere to put it), so a
    repository configured for one provenance refuses to hand back a row carrying another instead of
    silently presenting the plan as if it were its own. The refusal names the plan id and the fix
    (construct the repository with that provenance).

``route_plans.inputs_fingerprint``
    is nullable and recomputable, so :meth:`save` stores ``plan.inputs_fingerprint()`` and the load
    path never compares it: the authoritative per-run fingerprints live in
    ``route_optimization_runs`` (Stage 3 unit U11), and a plan-level fingerprint may legitimately
    include a travel-matrix fingerprint that this schema does not persist (section 8), so a
    mismatch here would be a false alarm rather than a defect.

``route_plans.created_at_utc`` / ``updated_at_utc``
    are repository-managed UTC ISO-8601 text with a trailing ``Z``. ``created_at_utc`` is preserved
    across updates and ``updated_at_utc`` is refreshed on every save.

``route_stops.geocode_provider`` / ``geocode_checked_at_utc``
    have no counterpart in the stop model and are stored as ``NULL``. This is a recorded gap, not a
    silent fabrication: which provider produced the coordinates and when it was last checked is
    information a later geocoding stage must fill, and inventing it here would be exactly the kind
    of unverifiable claim the product forbids. ``route_stops.created_at_utc`` is preserved for stop
    ids that already belong to the plan (a re-save updates rather than re-creates them) and
    ``updated_at_utc`` is refreshed on every save.

Known storage boundary (schema-imposed, not a domain rule)
---------------------------------------------------------

``route_stops.id`` is the primary key, so a stop id is globally unique **in storage** even though
the domain only requires uniqueness inside one plan. Two plans that share a stop id therefore
cannot both be persisted: the second :meth:`save` fails with the underlying
``sqlite3.IntegrityError`` and the transaction is rolled back, so nothing partial is written. The
raw driver error is propagated rather than disguised as a domain error, because it is a storage
constraint and nothing else.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, time, timezone
from enum import Enum
from typing import Any, TypeVar

from core.model.cost_policy import (
    DEMO_PROVISIONAL_POLICY_NAME,
    SMART_ROUTE_ELAPSED_POLICY_NAME,
    RouteCostPolicy,
    default_component_declarations,
    demo_provisional_policy,
    empty_cost_policy,
    smart_route_elapsed_policy,
)
from core.model.first_stop import FirstStopIntent
from core.model.ids import PlanId
from core.model.order_override import OrderConstraint, OrderOverrides
from core.model.route_mode import RouteMode
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.service_window import ServiceWindow, WindowEndPolicy, WindowKind
from core.model.value_objects import DataProvenance, GeoPoint, PlaceRef
from core.validation.errors import InvalidCostPolicyError
from storage import StoredPlanError, StorageError
from storage.sqlite.database import utc_now_iso

__all__ = ["ORDER_OVERRIDES_VERSION", "REGISTERED_COST_POLICIES", "SqliteRoutePlanRepository"]

#: Version of the order-override envelope this build writes and accepts (D30, schema section 3.1).
#: ``version`` is mandatory; anything else is rejected on load instead of guessed.
ORDER_OVERRIDES_VERSION = 1

#: The name the neutral, unweighted policy carries (derived from the code, not copied by hand).
_UNWEIGHTED_POLICY_NAME = empty_cost_policy().name

#: Cost policies this build implements, by name (D35 default, D31 non-default study, neutral
#: baseline). A stored policy name is only meaningful when the build knows the policy that owns
#: those weights - the reason an unknown name is refused at save **and** on load, and the reason
#: ``declarations`` (static capability metadata, D16) are rebuilt from the code's registry
#: ``core.model.cost_policy.default_component_declarations`` instead of being persisted.
REGISTERED_COST_POLICIES: Mapping[str, Callable[[], RouteCostPolicy]] = {
    SMART_ROUTE_ELAPSED_POLICY_NAME: smart_route_elapsed_policy,
    DEMO_PROVISIONAL_POLICY_NAME: demo_provisional_policy,
    _UNWEIGHTED_POLICY_NAME: empty_cost_policy,
}

#: The exact key set of a stored cost-policy payload. A payload of another shape is refused rather
#: than guessed at, so a format change is a loud failure instead of a silent partial read.
_COST_POLICY_KEYS = frozenset({"name", "weights", "provisional", "notes"})

#: ``HH:MM:SS`` local wall clock (schema section 1) - never a date and never an offset.
_WALL_CLOCK_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$")

#: ``YYYY-MM-DDTHH:MM:SSZ`` - UTC ISO-8601 with a trailing ``Z`` (schema section 1).
_UTC_Z_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_PLAN_COLUMNS = (
    "id",
    "name",
    "timezone",
    "departure_label",
    "departure_latitude",
    "departure_longitude",
    "departure_time_utc",
    "finish_label",
    "finish_latitude",
    "finish_longitude",
    "route_mode",
    "first_stop_mode",
    "first_stop_selected_stop_id",
    "first_stop_selection_source",
    "first_stop_pinned",
    "window_end_policy",
    "order_overrides_json",
    "cost_policy_json",
    "default_service_duration_sec",
    "inputs_fingerprint",
    "data_provenance",
    "created_at_utc",
    "updated_at_utc",
)

_STOP_COLUMNS = (
    "id",
    "plan_id",
    "input_position",
    "raw_address",
    "normalized_address",
    "latitude",
    "longitude",
    "geocode_status",
    "geocode_provider",
    "geocode_checked_at_utc",
    "service_window_kind",
    "service_window_start",
    "service_window_end",
    "window_end_policy",
    "service_duration_sec",
    "priority",
    "service_status",
    "enabled",
    "notes",
    "created_at_utc",
    "updated_at_utc",
)

_PLAN_INSERT = (
    f"INSERT INTO route_plans ({', '.join(_PLAN_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _PLAN_COLUMNS)})"
)

_PLAN_UPDATE_ASSIGNMENTS = ", ".join(
    f"{column} = ?" for column in _PLAN_COLUMNS if column not in ("id", "created_at_utc")
)
_PLAN_UPDATE = f"UPDATE route_plans SET {_PLAN_UPDATE_ASSIGNMENTS} WHERE id = ?"

_STOP_INSERT = (
    f"INSERT INTO route_stops ({', '.join(_STOP_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _STOP_COLUMNS)})"
)

_EnumT = TypeVar("_EnumT", bound=Enum)


class SqliteRoutePlanRepository:
    """Store :class:`~core.model.route_plan.RoutePlan` objects in SQLite (schema sections 3-4, 7).

    ``connection`` must be an open connection to a migrated database - use
    :func:`storage.sqlite.database.connect` followed by
    :func:`storage.sqlite.database.migrate`. The constructor refuses a connection whose rows are
    not ``sqlite3.Row`` (the repository reads columns by name), verifies that foreign keys are
    enforced (enabling them itself if the setting was turned off) and that the approved tables
    exist, so a misconfiguration fails at construction instead of silently changing the delete
    cascade or the meaning of a read later.

    ``data_provenance`` is required (the schema column is ``NOT NULL`` and the domain does not carry
    provenance): it is the provenance this repository writes, and the only one it will read back.
    ``clock`` is a testability seam returning a timezone-aware UTC ``datetime``; it exists so
    deterministic tests can pin the repository-managed timestamps, and defaults to the real clock.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        data_provenance: DataProvenance | str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection = connection
        self._data_provenance = _coerce_provenance(data_provenance)
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._ensure_row_factory(connection)
        self._ensure_foreign_keys(connection)
        self._ensure_schema(connection)

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #
    def save(self, plan: RoutePlan) -> None:
        """Insert or update ``plan`` and its stops in one transaction (schema sections 3-4).

        Raises:
            StorageError: ``plan`` carries a cost policy this build cannot store (an unregistered
                policy name, or declarations that are not the code's registry), a departure time
                with sub-second precision (the storage convention is whole seconds, and silently
                truncating it would break the exact round-trip this repository promises), or a
                clock that does not return a timezone-aware ``datetime``.
            sqlite3.Error: the database refused the write (for example two plans sharing a stop id,
                which the primary key forbids). The transaction is rolled back first.
        """
        if not isinstance(plan, RoutePlan):
            raise StorageError(f"save() needs a RoutePlan, got {type(plan).__name__}")
        self._require_storable_policy(plan.cost_policy)

        if plan.departure_time.microsecond:
            raise StorageError(
                f"plan {plan.id!r}: departure_time {plan.departure_time.isoformat()} has "
                "sub-second precision, which the storage convention (UTC ISO-8601 seconds with a "
                "trailing Z, schema section 1) cannot represent; storing it would silently change "
                "the departure instant, so it is refused instead"
            )
        departure_time = utc_now_iso(plan.departure_time)

        now = self._now_iso()
        intent = plan.first_service_stop

        with self._connection:
            existing = self._connection.execute(
                "SELECT created_at_utc FROM route_plans WHERE id = ?", (str(plan.id),)
            ).fetchone()
            created_at = now if existing is None else _utc_z_text(
                existing["created_at_utc"], "route_plans.created_at_utc"
            )
            stop_created_at = self._existing_stop_created_at(plan.id)

            values: tuple[Any, ...] = (
                str(plan.id),
                None,  # name: no domain counterpart - always written as NULL (Deliverable 4)
                plan.timezone,
                plan.departure.label,
                float(plan.departure.point.latitude),
                float(plan.departure.point.longitude),
                departure_time,
                plan.finish.label,
                float(plan.finish.point.latitude),
                float(plan.finish.point.longitude),
                plan.route_mode.value,
                intent.mode.value,
                None,  # first_stop_selected_stop_id: restored after the stops exist (FK)
                None,  # first_stop_selection_source: written with the selection, never alone
                1 if intent.pinned else 0,
                plan.window_end_policy.value,
                _order_overrides_json(plan.order_overrides),
                _cost_policy_json(plan.cost_policy),
                plan.default_service_duration,
                plan.inputs_fingerprint(),
                self._data_provenance.value,
                created_at,
                now,
            )

            if existing is None:
                self._connection.execute(_PLAN_INSERT, values)
            else:
                # The id is the WHERE clause and created_at_utc is preserved across updates, so
                # both are excluded from the assignments.
                update_values = tuple(
                    value
                    for column, value in zip(_PLAN_COLUMNS, values)
                    if column not in ("id", "created_at_utc")
                ) + (str(plan.id),)
                self._connection.execute(_PLAN_UPDATE, update_values)

            # Safe now: the plan row no longer references any stop of this plan.
            self._connection.execute(
                "DELETE FROM route_stops WHERE plan_id = ?", (str(plan.id),)
            )
            self._connection.executemany(
                _STOP_INSERT,
                [
                    self._stop_values(
                        plan_id=plan.id,
                        stop=stop,
                        created_at=stop_created_at.get(stop.id, now),
                        updated_at=now,
                    )
                    for stop in plan.stops
                ],
            )

            if intent.selected_stop_id is not None:
                # Selection and its provenance are restored together, so they cannot diverge.
                self._connection.execute(
                    "UPDATE route_plans SET first_stop_selected_stop_id = ?, "
                    "first_stop_selection_source = ? WHERE id = ?",
                    (
                        str(intent.selected_stop_id),
                        intent.selection_source.value,  # type: ignore[union-attr]
                        str(plan.id),
                    ),
                )

    def delete(self, plan_id: PlanId) -> bool:
        """Delete the plan; its stops and optimization runs go with it (schema sections 4-5).

        Returns ``True`` when a plan was removed and ``False`` when there was none. The cascade is
        the schema's own (``ON DELETE CASCADE``) and is enforced because this repository verifies
        ``PRAGMA foreign_keys`` at construction.
        """
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM route_plans WHERE id = ?", (str(plan_id),)
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def get(self, plan_id: PlanId) -> RoutePlan | None:
        """The stored plan, or ``None`` when no plan has that id (a documented choice).

        Absence is not an error: the caller asked for a plan that is not there. Every other
        failure to reconstruct the plan is raised - a corrupt or hand-edited row never becomes a
        ``None`` and never becomes a half-valid plan.
        """
        row = self._connection.execute(
            "SELECT * FROM route_plans WHERE id = ?", (str(plan_id),)
        ).fetchone()
        if row is None:
            return None
        return self._plan_from_row(row)

    def list(self) -> tuple[RoutePlan, ...]:
        """Every stored plan, ordered by ``created_at_utc`` ascending, then ``id``.

        Creation order is the natural order for "the driver's plans"; ``id`` is the deterministic
        tie-break, so the order is total even when several plans share a timestamp (which the
        stored second precision makes possible).
        """
        rows = self._connection.execute(
            "SELECT * FROM route_plans ORDER BY created_at_utc ASC, id ASC"
        ).fetchall()
        return tuple(self._plan_from_row(row) for row in rows)

    # ------------------------------------------------------------------ #
    # plan mapping (stored row -> domain)
    # ------------------------------------------------------------------ #
    def _plan_from_row(self, row: sqlite3.Row) -> RoutePlan:
        self._require_provenance(row)
        plan_id = row["id"]
        return RoutePlan(
            id=PlanId(plan_id),
            timezone=row["timezone"],
            departure=PlaceRef(
                row["departure_label"],
                GeoPoint(
                    _coordinate(row["departure_latitude"], "departure_latitude"),
                    _coordinate(row["departure_longitude"], "departure_longitude"),
                ),
            ),
            departure_time=_parse_utc_z(row["departure_time_utc"], "departure_time_utc"),
            finish=PlaceRef(
                row["finish_label"],
                GeoPoint(
                    _coordinate(row["finish_latitude"], "finish_latitude"),
                    _coordinate(row["finish_longitude"], "finish_longitude"),
                ),
            ),
            stops=self._stops_for(plan_id),
            cost_policy=_cost_policy_from_json(row["cost_policy_json"]),
            route_mode=_coerce_enum(
                RouteMode, row["route_mode"], "route_plans.route_mode", StoredPlanError
            ),
            window_end_policy=_coerce_enum(
                WindowEndPolicy,
                row["window_end_policy"],
                "route_plans.window_end_policy",
                StoredPlanError,
            ),
            first_service_stop=FirstStopIntent(
                mode=row["first_stop_mode"],
                selected_stop_id=row["first_stop_selected_stop_id"],
                selection_source=row["first_stop_selection_source"],
                pinned=_flag(row["first_stop_pinned"], "route_plans.first_stop_pinned"),
            ),
            order_overrides=_order_overrides_from_json(row["order_overrides_json"]),
            default_service_duration=row["default_service_duration_sec"],
        )

    def _stops_for(self, plan_id: str) -> tuple[RouteStop, ...]:
        rows = self._connection.execute(
            "SELECT * FROM route_stops WHERE plan_id = ? ORDER BY input_position ASC",
            (plan_id,),
        ).fetchall()
        return tuple(self._stop_from_row(row) for row in rows)

    def _stop_from_row(self, row: sqlite3.Row) -> RouteStop:
        kind = _coerce_enum(
            WindowKind,
            row["service_window_kind"],
            "route_stops.service_window_kind",
            StoredPlanError,
        )
        window = ServiceWindow(
            window_kind=kind,
            start_local=_parse_wall_clock(row["service_window_start"]),
            end_local=_parse_wall_clock(row["service_window_end"]),
            window_end_policy=(
                None
                if row["window_end_policy"] is None
                else _coerce_enum(
                    WindowEndPolicy,
                    row["window_end_policy"],
                    "route_stops.window_end_policy",
                    StoredPlanError,
                )
            ),
        )
        return RouteStop(
            id=row["id"],
            raw_address=row["raw_address"],
            # geocode_status and service_status are passed through as stored text on purpose: the
            # domain's own coercion is what rejects a value it does not accept, and it reports its
            # own error type (InvalidRouteStopError) instead of a generic storage failure.
            geocode_status=row["geocode_status"],
            service_status=row["service_status"],
            latitude=_optional_coordinate(row["latitude"], "route_stops.latitude"),
            longitude=_optional_coordinate(row["longitude"], "route_stops.longitude"),
            normalized_address=row["normalized_address"],
            notes=row["notes"],
            input_position=row["input_position"],
            priority=_optional_priority(row["priority"]),
            # service_duration_sec is passed through raw as well: negative and non-integer stored
            # values are rejected by RouteStop's own rules, never repaired to a plausible duration.
            service_duration=row["service_duration_sec"],
            service_window=window,
            enabled=_flag(row["enabled"], "route_stops.enabled"),
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _stop_values(
        self, *, plan_id: str, stop: RouteStop, created_at: str, updated_at: str
    ) -> tuple[Any, ...]:
        window = stop.service_window
        return (
            str(stop.id),
            str(plan_id),
            stop.input_position,
            stop.raw_address,
            stop.normalized_address,
            stop.latitude,
            stop.longitude,
            stop.geocode_status.value,
            None,  # geocode_provider: no domain counterpart (Deliverable 4)
            None,  # geocode_checked_at_utc: no domain counterpart (Deliverable 4)
            window.window_kind.value,
            _write_wall_clock(window.start_local),
            _write_wall_clock(window.end_local),
            (
                window.window_end_policy.value
                if window.window_end_policy is not None
                else None
            ),
            stop.service_duration,
            stop.priority,
            stop.service_status.value,
            1 if stop.enabled else 0,
            stop.notes,
            created_at,
            updated_at,
        )

    def _existing_stop_created_at(self, plan_id: str) -> dict[str, str]:
        """``created_at_utc`` of the plan's current stop rows, by stop id.

        A re-save replaces the stop set, but a stop that was already part of the plan keeps its
        creation timestamp: "created" must not silently become "last written".
        """
        rows = self._connection.execute(
            "SELECT id, created_at_utc FROM route_stops WHERE plan_id = ?", (str(plan_id),)
        ).fetchall()
        return {
            row["id"]: _utc_z_text(row["created_at_utc"], "route_stops.created_at_utc")
            for row in rows
        }

    def _require_provenance(self, row: sqlite3.Row) -> None:
        stored = row["data_provenance"]
        if stored is None:
            raise StoredPlanError(
                f"plan {row['id']!r}: data_provenance is NULL, but the schema declares it NOT NULL "
                "and the domain does not carry provenance, so the repository cannot accept the row"
            )
        try:
            member = DataProvenance(stored)
        except ValueError:
            raise StoredPlanError(
                f"plan {row['id']!r}: data_provenance={stored!r} is not one of "
                f"{[item.value for item in DataProvenance]}"
            ) from None
        if member is not self._data_provenance:
            raise StoredPlanError(
                f"plan {row['id']!r}: stored data_provenance={member.value} but this repository "
                f"was constructed with {self._data_provenance.value}. The domain carries no "
                "provenance, so get() could not report the difference - construct the repository "
                "with the provenance of the plans it should read instead of mixing them silently"
            )

    def _require_storable_policy(self, policy: object) -> None:
        if not isinstance(policy, RouteCostPolicy):
            raise StorageError(
                f"cost_policy must be a RouteCostPolicy, got {type(policy).__name__}"
            )
        if policy.name not in REGISTERED_COST_POLICIES:
            raise StorageError(
                f"cost policy name {policy.name!r} is not a policy this build implements "
                f"({', '.join(sorted(REGISTERED_COST_POLICIES))}); its weights would be stored "
                "without the policy identity that gives them meaning, so the plan could never be "
                "loaded back (D13/D16/D35: weights are explicit configuration)"
            )
        if dict(policy.declarations) != default_component_declarations():
            raise StorageError(
                f"cost policy {policy.name!r} carries capability declarations that are not the "
                "code's registry; declarations are static capability metadata rebuilt from code on "
                "load and are never persisted, so storing this policy would silently change it"
            )

    def _now_iso(self) -> str:
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise StorageError(
                "the repository clock must return a timezone-aware datetime (storage stores UTC "
                f"only), got {moment!r}"
            )
        return utc_now_iso(moment)

    @staticmethod
    def _ensure_row_factory(connection: sqlite3.Connection) -> None:
        """Rows are read by column name, so the connection must hand back ``sqlite3.Row``."""
        if connection.row_factory is not sqlite3.Row:
            raise StorageError(
                "this repository reads rows by column name, so the connection needs "
                "row_factory = sqlite3.Row; open it with storage.sqlite.database.connect()"
            )

    @staticmethod
    def _ensure_foreign_keys(connection: sqlite3.Connection) -> None:
        """Guarantee the cascade this repository's ``delete`` relies on (schema sections 4-5)."""
        connection.execute("PRAGMA foreign_keys = ON")
        if not connection.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise StorageError(
                "could not enforce PRAGMA foreign_keys on this connection; the plan/stop/run "
                "cascade cannot be guaranteed, so the repository refuses to run"
            )

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        missing = [
            table
            for table in ("route_plans", "route_stops")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            is None
        ]
        if missing:
            raise StorageError(
                f"the database is missing {', '.join(missing)}: apply the approved schema first "
                "with storage.sqlite.database.migrate(connection)"
            )


# --------------------------------------------------------------------------- #
# stored-text helpers: shape problems are storage errors, content problems are the domain's
# --------------------------------------------------------------------------- #
def _coerce_provenance(value: DataProvenance | str) -> DataProvenance:
    if isinstance(value, DataProvenance):
        return value
    try:
        return DataProvenance(value)
    except ValueError:
        raise StorageError(
            f"data_provenance={value!r} is not one of "
            f"{[item.value for item in DataProvenance]}; the repository needs the provenance it "
            "writes and reads, because the domain does not carry it"
        ) from None


def _coerce_enum(
    enum_type: type[_EnumT], value: object, field: str, error_type: type[Exception]
) -> _EnumT:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError:
        raise error_type(
            f"{field}={value!r} is not one of {[member.value for member in enum_type]}"
        ) from None


def _flag(value: object, field: str) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise StoredPlanError(
            f"{field}={value!r} must be an integer 0 or 1 (storage booleans are integers)"
        )
    return bool(value)


def _coordinate(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoredPlanError(f"{field}={value!r} must be a number")
    return float(value)


def _optional_coordinate(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _coordinate(value, field)


def _optional_priority(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoredPlanError(f"route_stops.priority={value!r} must be an integer or NULL")
    return value


def _utc_z_text(text: object, field: str) -> str:
    """Validate the storage timestamp convention and return the text unchanged.

    Only ``YYYY-MM-DDTHH:MM:SSZ`` is accepted: an offset or a local timestamp is a different
    instant, and guessing which one was meant is exactly what the convention forbids. Returning the
    text itself (rather than a ``datetime``) keeps every stored value in one representation.

    The shape is not the whole rule. ``2026-02-30T01:00:00Z`` and ``2026-13-01T01:00:00Z`` match the
    shape while naming no instant, and ``strptime`` reports that as a bare ``ValueError`` ("day is
    out of range for month"), which would leak an interpreter accident out of the read path. Every
    stored timestamp in this module therefore goes through the real calendar as well, and an
    impossible date is refused as a :class:`~storage.StoredPlanError` naming the column and the
    value - exactly like the other malformed stored timestamps, and never repaired into a plausible
    one (D26 taxonomy; D38 acceptance item 5: hand-edited rows fail loudly).
    """
    if not isinstance(text, str) or not _UTC_Z_RE.match(text):
        raise StoredPlanError(
            f"{field}={text!r} is not UTC ISO-8601 with a trailing Z "
            "(for example 2026-09-11T01:00:00Z, schema section 1)"
        )
    try:
        datetime.strptime(text, _UTC_FORMAT)
    except (ValueError, OverflowError):
        raise StoredPlanError(
            f"{field}={text!r} has the stored timestamp shape but is not a real calendar date and "
            "time, so it names no instant; stored timestamps are instants and an impossible date is "
            "refused rather than repaired or carried forward (schema section 1)"
        ) from None
    return text


def _parse_utc_z(text: object, field: str) -> datetime:
    """The same validation, as a timezone-aware UTC ``datetime`` for the domain.

    ``strptime`` cannot fail here: :func:`_utc_z_text` has already proved both the stored shape and
    the real calendar, so the load path never surfaces the interpreter's own ``ValueError``.
    """
    return datetime.strptime(_utc_z_text(text, field), _UTC_FORMAT).replace(tzinfo=timezone.utc)


def _write_wall_clock(value: time | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%H:%M:%S")


def _parse_wall_clock(text: object) -> time | None:
    """Parse a stored local ``HH:MM:SS`` wall clock, or refuse the row.

    A resolved instant (``2026-09-11T01:00:00Z``) or a date is refused: the window must stay a
    local wall-clock value so the plan's IANA zone resolves it under strict DST rules after every
    load (D3, schema section 1).
    """
    if text is None:
        return None
    if not isinstance(text, str) or not _WALL_CLOCK_RE.match(text):
        raise StoredPlanError(
            f"service window value {text!r} is not a local HH:MM:SS wall-clock time; storage keeps "
            "the customer's local opening hours and never a resolved instant (schema section 1)"
        )
    return time.fromisoformat(text)


def _json_object(text: object, field: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise StoredPlanError(f"{field} must be TEXT holding a JSON object, got {text!r}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise StoredPlanError(f"{field} is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise StoredPlanError(
            f"{field} must hold a JSON object, got {type(payload).__name__}"
        )
    return payload


def _require_keys(payload: Mapping[str, Any], field: str, *, allowed: frozenset[str]) -> None:
    found = set(payload)
    missing = sorted(allowed - found)
    unknown = sorted(found - allowed)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise StoredPlanError(
            f"{field} does not have the stored shape ({', '.join(details)}); the reader refuses "
            "to guess at a payload written by another format"
        )


# --------------------------------------------------------------------------- #
# JSON columns
# --------------------------------------------------------------------------- #
def _cost_policy_json(policy: RouteCostPolicy) -> str:
    """The stored policy: name, weights, ``provisional`` and ``notes`` (D13/D16/D31/D35).

    ``declarations`` are deliberately absent: they are the code's static capability registry
    (``default_component_declarations``) and are rebuilt on load, while the weights themselves are
    stored truth, because D13/D16/D35 make weights explicit configuration rather than hidden
    defaults.
    """
    return json.dumps(
        {
            "name": policy.name,
            "weights": {
                component.value: weight
                for component, weight in sorted(
                    policy.weights.items(), key=lambda item: item[0].value
                )
            },
            "provisional": policy.provisional,
            "notes": policy.notes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _cost_policy_from_json(text: object) -> RouteCostPolicy:
    payload = _json_object(text, "route_plans.cost_policy_json")
    _require_keys(payload, "route_plans.cost_policy_json", allowed=_COST_POLICY_KEYS)

    name = payload["name"]
    if not isinstance(name, str) or not name.strip():
        raise StoredPlanError(
            f"route_plans.cost_policy_json: name={name!r} must be a non-empty policy name"
        )
    if name not in REGISTERED_COST_POLICIES:
        # The domain's own cost-policy error: a name this build does not implement means the
        # weights have no policy identity behind them, and D13/D16/D35 forbid guessing one.
        raise InvalidCostPolicyError(
            f"stored cost policy name {name!r} is not a policy this build implements "
            f"({', '.join(sorted(REGISTERED_COST_POLICIES))}); the weights are explicit "
            "configuration and are never adopted under an unknown policy name"
        )

    weights = payload["weights"]
    if not isinstance(weights, dict):
        raise StoredPlanError(
            f"route_plans.cost_policy_json: weights must be a JSON object, got "
            f"{type(weights).__name__}"
        )
    provisional = payload["provisional"]
    if not isinstance(provisional, bool):
        raise StoredPlanError(
            f"route_plans.cost_policy_json: provisional must be true or false, got "
            f"{provisional!r}"
        )
    notes = payload["notes"]
    if not isinstance(notes, str):
        raise StoredPlanError(
            f"route_plans.cost_policy_json: notes must be a string, got {notes!r}"
        )

    # Weights go to the domain as stored: RouteCostPolicy re-validates every component name,
    # rejects non-finite or negative weights, and refuses to weight a component whose capability
    # status is not "implemented" (D16).
    return RouteCostPolicy(
        name=name,
        weights=dict(weights),
        declarations=default_component_declarations(),
        provisional=provisional,
        notes=notes,
    )


def _order_overrides_json(overrides: OrderOverrides) -> str:
    """The versioned envelope of D30 (schema section 3.1), always written with ``version``."""
    return json.dumps(
        {
            "version": ORDER_OVERRIDES_VERSION,
            "constraints": [
                {
                    "kind": constraint.kind.value,
                    "stop_id": constraint.stop_id,
                    "position": constraint.position,
                }
                for constraint in overrides.constraints
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _order_overrides_from_json(text: object) -> OrderOverrides:
    payload = _json_object(text, "route_plans.order_overrides_json")
    _require_keys(
        payload,
        "route_plans.order_overrides_json",
        allowed=frozenset({"version", "constraints"}),
    )

    version = payload["version"]
    if version != ORDER_OVERRIDES_VERSION:
        # The domain models no envelope version, so this is a storage-level refusal: readers must
        # reject an unknown version instead of guessing what its constraints mean (D30).
        raise StoredPlanError(
            f"route_plans.order_overrides_json: version={version!r} is not the version this build "
            f"writes and understands ({ORDER_OVERRIDES_VERSION}); an unknown envelope version is "
            "rejected rather than guessed (D30)"
        )

    raw_constraints = payload["constraints"]
    if not isinstance(raw_constraints, list):
        raise StoredPlanError(
            "route_plans.order_overrides_json: constraints must be a JSON array, got "
            f"{type(raw_constraints).__name__}"
        )

    constraints = []
    for position, item in enumerate(raw_constraints):
        where = f"route_plans.order_overrides_json.constraints[{position}]"
        if not isinstance(item, dict):
            raise StoredPlanError(f"{where} must be a JSON object, got {type(item).__name__}")
        _require_keys(item, where, allowed=frozenset({"kind", "stop_id", "position"}))
        # OrderConstraint applies the domain's own rules to the content: an unknown constraint
        # kind, or a first_stop constraint carrying a position, is the domain's error and not a
        # storage guess.
        constraints.append(
            OrderConstraint(
                kind=item["kind"],
                stop_id=item["stop_id"],
                position=item["position"],
            )
        )
    return OrderOverrides(tuple(constraints))
