"""SQLite ``RouteOptimizationRunRepository``: immutable run history (U11; D38).

Implements the pure port :class:`core.repositories.RouteOptimizationRunRepository` against the
APPROVED schema ``docs/STORAGE_SCHEMA.md`` section 5, using stdlib ``sqlite3`` only - no ORM and no
third-party dependency. Storage depends on ``core``; ``core`` never imports this module.

What this repository promises, and what it deliberately does not do
------------------------------------------------------------------

**Append-only, by construction.** The class exposes exactly three methods - ``append``,
``list_for_plan`` and ``latest`` - and no update and no delete of a run. A stored run is a historical
fact, so there is no API that could rewrite one; the approved MVP keeps every run (D38: KEEP ALL
RUNS, no retention policy). ``get(run_id)`` (added by Stage 4 U14) is an **additive read lookup**
over the table's existing primary key - it is a read, it changes no stored byte, and the append-only
guarantee is untouched.

**One row per append, one transaction.** :meth:`SqliteRouteOptimizationRunRepository.append` writes
exactly one ``route_optimization_runs`` row inside a single transaction, so a partially written run
is never observable. ``created_at_utc`` is the run's own timestamp, written as given: a run row has
no "last written" stamp because it is never rewritten. The stored text is whole seconds
(``YYYY-MM-DDTHH:MM:SSZ``, schema section 1), so an instant with sub-second precision is **refused**
before any row is written - the run's own ``created_at_utc`` as a :class:`storage.StoredRunError`,
and any instant inside a payload by the codec's own error - instead of being silently truncated into
a row that could never load back equal to the run that was appended.

**A run never creates a plan.** ``plan_id`` is a foreign key to ``route_plans`` and this repository
issues no plan insert anywhere. Appending a run for a plan that does not exist therefore fails
loudly with :class:`storage.StoredRunError` (naming the run and the plan, with the foreign-key
detail) instead of inventing the plan the run belongs to.

**Ordering is deterministic and documented.** :meth:`list_for_plan` returns runs ordered by
``created_at_utc`` ascending and then by insertion order (SQLite ``rowid``) ascending, so two runs
that share a stored second - the storage convention is whole seconds - still come back in the order
they were appended. :meth:`latest` is the same order read backwards, so it always returns the run
``list_for_plan`` puts last, or ``None`` when the plan has no runs.

**The payloads are validated by the domain on load, never trusted.** The ``*_json`` columns hold
domain value objects, and this module does not parse their content itself: it hands each column to
the codec that owns its shape (:mod:`core.model.optimization_run`), which rebuilds real domain value
objects - ``RouteMetrics``, ``OptimizationRunRecommendation``, ``FirstStopCandidate``, ``Violation``
- and re-validates the run's own rules. A hand-edited row therefore fails loudly (D38 acceptance
item 5). The error types on the load path are:

=================================================  ==================================================
stored problem                                     error raised
=================================================  ==================================================
malformed JSON in any ``*_json`` column            ``InvalidOptimizationRunError`` (via the codec)
wrong JSON shape / missing / unknown key           ``InvalidOptimizationRunError`` (via the codec)
unknown ``run_kind`` / ``status``                  ``InvalidOptimizationRunError`` from the record
unknown ``data_provenance``                        ``InvalidOptimizationRunError`` from the record
                                                   (the DDL's own CHECK constraints already make
                                                   such a value unstorable; the record re-validates
                                                   it on the way in and on the way out)
status contradicting the stored order              ``InvalidOptimizationRunError``
status contradicting the stored violations         ``InvalidOptimizationRunError``
top-K candidates that are not the ranked head      ``InvalidOptimizationRunError``
recommendation naming a stop outside ``order``     ``StoredRunError`` (the record's audit rule,
                                                   D32, re-checked on load)
a ranked candidate the run marks as violating      ``StoredRunError`` (the record's v2 section 14 /
                                                   D32 rule, re-checked on load)
unknown stored cost-policy name                    ``InvalidCostPolicyError`` (the domain's own)
unknown cost component in the weights              ``InvalidCostPolicyError`` (via ``RouteCostPolicy``)
weight for a non-implemented component             ``UnsupportedFeatureError`` (capability rule, D16)
negative / non-integer stored metric               ``InvalidRoutePlanError`` (via ``RouteMetrics``)
violation missing its id, kind or message          ``InvalidRoutePlanError`` (via ``Violation``)
malformed TEXT where a JSON column is expected     ``StoredRunError`` (stored shape, not content)
``created_at_utc`` not UTC ISO-8601 with ``Z``     ``StoredRunError``
shape-valid UTC timestamp naming no instant        ``StoredRunError`` (calendar-invalid timestamp
                                                   such as ``2026-02-30T01:00:00Z``; the
                                                   interpreter's own ``ValueError`` never escapes)
missing NOT NULL column, unknown plan              ``StoredRunError``
duplicate run id                                   ``StoredRunError`` (the primary key forbids it;
                                                   a run is never overwritten)
=================================================  ==================================================

Nothing is repaired silently and no default is substituted anywhere on the load path.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from core.model.cost_policy import (
    DEMO_PROVISIONAL_POLICY_NAME,
    SMART_ROUTE_ELAPSED_POLICY_NAME,
    RouteCostPolicy,
    default_component_declarations,
    demo_provisional_policy,
    empty_cost_policy,
    smart_route_elapsed_policy,
)
from core.model.ids import PlanId, RunId
from core.model.optimization_run import (
    RANKED_VIOLATING_STOP_MARKER,
    RECOMMENDATION_OUTSIDE_ORDER_MARKER,
    OptimizationRun,
    decode_order_json,
    decode_recommendation_json,
    decode_run_metrics_json,
    decode_top_k_json,
    decode_violations_json,
    encode_cost_policy_json,
    encode_order_json,
    encode_recommendation_json,
    encode_run_metrics_json,
    encode_top_k_json,
    encode_violations_json,
)
from core.model.value_objects import DataProvenance
from core.validation.errors import InvalidCostPolicyError, InvalidOptimizationRunError
from storage import StorageError, StoredRunError
from storage.sqlite.database import utc_now_iso

__all__ = [
    "REGISTERED_COST_POLICIES",
    "RUN_COLUMNS",
    "SqliteRouteOptimizationRunRepository",
]

#: ``id`` is the table's primary key, so this returns at most one row.
_RUN_BY_ID = "SELECT * FROM route_optimization_runs WHERE id = ? LIMIT 1"

#: Cost policies this build implements, by name. A stored policy name is only meaningful when the
#: build knows the policy that owns those weights - the reason an unknown name is refused on load
#: rather than adopted (D13/D16/D35). Same registry and same payload as the plan repository, so a
#: run and the plan it belongs to can never disagree about the stored policy format.
REGISTERED_COST_POLICIES: dict[str, Callable[[], RouteCostPolicy]] = {
    SMART_ROUTE_ELAPSED_POLICY_NAME: smart_route_elapsed_policy,
    DEMO_PROVISIONAL_POLICY_NAME: demo_provisional_policy,
    empty_cost_policy().name: empty_cost_policy,
}

#: The exact key set of a stored cost-policy payload (the shape ``encode_cost_policy_json`` writes).
_COST_POLICY_KEYS = frozenset({"name", "weights", "provisional", "notes"})

#: ``YYYY-MM-DDTHH:MM:SSZ`` - UTC ISO-8601 with a trailing ``Z`` (schema section 1).
_UTC_Z_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The approved column set of ``route_optimization_runs`` (schema section 5), in schema order.
RUN_COLUMNS = (
    "id",
    "plan_id",
    "run_kind",
    "algorithm",
    "algorithm_version",
    "inputs_fingerprint",
    "route_fingerprint",
    "tzdata_version",
    "cost_policy_json",
    "data_provenance",
    "status",
    "order_json",
    "first_stop_recommendation_json",
    "top_k_json",
    "violations_json",
    "metrics_json",
    "created_at_utc",
)

_RUN_INSERT = (
    f"INSERT INTO route_optimization_runs ({', '.join(RUN_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in RUN_COLUMNS)})"
)

#: The documented order of ``list_for_plan``: the stored timestamp first, then insertion order.
#: ``rowid`` is SQLite's own monotonically increasing row identifier, so it is exactly the order in
#: which rows were appended and it is stable for a table without ``WITHOUT ROWID``.
_RUN_ORDER = "ORDER BY created_at_utc ASC, rowid ASC"
_RUN_REVERSE_ORDER = "ORDER BY created_at_utc DESC, rowid DESC"


class SqliteRouteOptimizationRunRepository:
    """Store and read immutable :class:`~core.model.optimization_run.OptimizationRun` history.

    ``connection`` must be an open connection to a migrated database - use
    :func:`storage.sqlite.database.connect` followed by :func:`storage.sqlite.database.migrate`.
    The constructor refuses a connection whose rows are not ``sqlite3.Row``, verifies that foreign
    keys are enforced (enabling them itself if the setting was turned off) and that the approved
    tables exist, so a misconfiguration fails at construction instead of silently accepting an
    orphan run later.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._ensure_row_factory(connection)
        self._ensure_foreign_keys(connection)
        self._ensure_schema(connection)

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #
    def append(self, run: OptimizationRun) -> None:
        """Append ``run`` as exactly one new row, in one transaction (schema section 5).

        The run's own ``created_at_utc`` is written as given: a run row is history and is never
        rewritten, so there is no repository-managed timestamp here. The instant is stored as
        ``YYYY-MM-DDTHH:MM:SSZ`` (whole seconds, schema section 1), so a sub-second
        ``created_at_utc`` is **refused** rather than silently truncated, exactly as the plan
        repository refuses a sub-second ``departure_time``.

        Raises:
            StorageError: ``run`` is not an :class:`~core.model.optimization_run.OptimizationRun`,
                or its cost policy is not one this build implements (its weights would then be
                stored without the policy identity that gives them meaning).
            StoredRunError: ``run.created_at_utc`` carries sub-second precision the storage
                convention cannot represent, or a payload instant does (the codec refuses it as an
                ``InvalidOptimizationRunError`` - see below), no plan with ``run.plan_id`` exists, or
                a run with that id is already stored. Nothing is repaired: a run never creates a
                plan, a run is never overwritten by a second append, and an instant is never
                truncated.
            InvalidOptimizationRunError: a payload carries an instant with sub-second precision, so
                the stored text could not reproduce it. Raised while the payload is encoded, before
                any row is written.
        """
        if not isinstance(run, OptimizationRun):
            raise StorageError(
                f"append() needs an OptimizationRun, got {type(run).__name__}"
            )
        self._require_storable_policy(run.cost_policy)
        if run.created_at_utc.microsecond:
            raise StoredRunError(
                f"run {run.id!r}: created_at_utc {run.created_at_utc.isoformat()} has sub-second "
                "precision, which the storage convention (UTC ISO-8601 seconds with a trailing Z, "
                "schema section 1) cannot represent; storing it would silently change the run's own "
                "instant, so it is refused instead"
            )

        values: tuple[Any, ...] = (
            str(run.id),
            str(run.plan_id),
            run.run_kind.value,
            run.algorithm,
            run.algorithm_version,
            run.inputs_fingerprint,
            run.route_fingerprint,
            run.tzdata_version,
            encode_cost_policy_json(run.cost_policy),
            run.data_provenance.value,
            run.status.value,
            encode_order_json(run.order),
            encode_recommendation_json(run.recommendation),
            None if run.top_k is None else encode_top_k_json(run.top_k),
            encode_violations_json(run.violations),
            encode_run_metrics_json(run.metrics),
            utc_now_iso(run.created_at_utc),
        )
        try:
            with self._connection:
                self._connection.execute(_RUN_INSERT, values)
        except sqlite3.IntegrityError as error:
            raise StoredRunError(self._describe_integrity_error(run, error)) from error

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def list_for_plan(self, plan_id: PlanId) -> tuple[OptimizationRun, ...]:
        """Every run of ``plan_id``, oldest first: ``created_at_utc``, then insertion order.

        An unknown plan and a plan with no runs are the same normal answer - an empty tuple - because
        this method asks "which runs does this plan have?". A corrupt stored run still fails loudly
        on the way out rather than being skipped.
        """
        rows = self._connection.execute(
            f"SELECT * FROM route_optimization_runs WHERE plan_id = ? {_RUN_ORDER}",  # noqa: S608
            (str(plan_id),),
        ).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def latest(self, plan_id: PlanId) -> OptimizationRun | None:
        """The newest run of ``plan_id``, or ``None`` when it has none (a documented choice).

        Exactly the run :meth:`list_for_plan` would put last: the same two keys read backwards, so
        the two methods can never disagree about which run is newest.
        """
        row = self._connection.execute(
            f"SELECT * FROM route_optimization_runs WHERE plan_id = ? "  # noqa: S608
            f"{_RUN_REVERSE_ORDER} LIMIT 1",
            (str(plan_id),),
        ).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    def get(self, run_id: RunId) -> OptimizationRun | None:
        """One run by its own id, or ``None`` when no run has it.

        An **additive read lookup**, not a change to the port or to the schema: the approved
        ``route_optimization_runs`` table already has ``id`` as its primary key, so a run can be
        addressed directly, and ``api``'s ``GET /api/runs/{id}`` needs exactly that. It reads one
        row through the same ``_run_from_row`` path as every other read, so a hand-edited row fails
        loudly here too and nothing is repaired or reordered.
        """
        row = self._connection.execute(_RUN_BY_ID, (str(run_id),)).fetchone()
        if row is None:
            return None
        return self._run_from_row(row)

    # ------------------------------------------------------------------ #
    # row -> domain
    # ------------------------------------------------------------------ #
    def _run_from_row(self, row: sqlite3.Row) -> OptimizationRun:
        run_id = row["id"]
        plan_id = row["plan_id"]
        if run_id is None or plan_id is None:
            raise StoredRunError(
                "a route_optimization_runs row is missing its NOT NULL id or plan_id; the schema "
                "declares both NOT NULL, so the row cannot be read as a run"
            )
        order = decode_order_json(_json_column(row["order_json"], "order_json", run_id))
        recommendation = decode_recommendation_json(
            _json_column(
                row["first_stop_recommendation_json"], "first_stop_recommendation_json", run_id
            )
        )
        try:
            run = OptimizationRun(
                id=RunId(run_id),
                plan_id=PlanId(plan_id),
                run_kind=row["run_kind"],
                algorithm=row["algorithm"],
                algorithm_version=row["algorithm_version"],
                inputs_fingerprint=row["inputs_fingerprint"],
                route_fingerprint=row["route_fingerprint"],
                tzdata_version=row["tzdata_version"],
                cost_policy=_cost_policy_from_json(
                    _json_column(row["cost_policy_json"], "cost_policy_json", run_id), run_id
                ),
                data_provenance=row["data_provenance"],
                status=row["status"],
                order=order,
                recommendation=recommendation,
                violations=decode_violations_json(
                    _json_column(row["violations_json"], "violations_json", run_id)
                ),
                metrics=decode_run_metrics_json(
                    _json_column(row["metrics_json"], "metrics_json", run_id)
                ),
                created_at_utc=_parse_utc_z(
                    row["created_at_utc"], "route_optimization_runs.created_at_utc"
                ),
                top_k=(
                    None
                    if row["top_k_json"] is None
                    else decode_top_k_json(_json_column(row["top_k_json"], "top_k_json", run_id))
                ),
            )
        except InvalidOptimizationRunError as error:
            if not any(
                marker in str(error)
                for marker in (RECOMMENDATION_OUTSIDE_ORDER_MARKER, RANKED_VIOLATING_STOP_MARKER)
            ):
                # Every other record rule already reports itself as the domain error the load path
                # documents; the two audit-coherence rules of D32 are storage-coherence problems, so
                # a hand-edited row is reported as such instead of as a plain domain error.
                raise
            raise StoredRunError(str(error)) from error
        self._require_recommendation_inside_run(run)
        return run

    @staticmethod
    def _require_recommendation_inside_run(run: OptimizationRun) -> None:
        """A stored recommendation may only name stops of the stored route (audit coherence, D32).

        Both rules are checked by the record itself in ``__post_init__``, so a hand-edited row is
        normally refused by the constructor above and re-reported as a :class:`StoredRunError`; this
        pass is therefore defence in depth for a row that reaches the reader without that constructor.
        It is deliberately kept rather than deleted, and it is the place the two cross-payload facts
        are stated: a ranked candidate outside ``order``, and a ranked candidate the same run marks
        as a violating stop (v2 section 14: an infeasible complete route is never a ranked
        candidate). It is a run rule, not a repair: such a recommendation is contradictory history.
        """
        ranked = run.recommendation.ranked_stop_ids
        unknown = [stop_id for stop_id in ranked if stop_id not in run.order]
        if unknown:
            raise StoredRunError(
                f"run {run.id!r} stores a recommendation naming stop(s) "
                f"{RECOMMENDATION_OUTSIDE_ORDER_MARKER} "
                f"{list(run.order)!r}: {[str(stop_id) for stop_id in unknown]!r}. A recommendation "
                "is the audit record of what this run showed about this route (D32), so it cannot "
                "reference a route the run did not commit."
            )
        violating = {str(violation.stop_id) for violation in run.violations}
        contradictory = [str(stop_id) for stop_id in ranked if str(stop_id) in violating]
        if contradictory:
            raise StoredRunError(
                f"run {run.id!r} marks ranked candidate(s) {contradictory!r} as a violating stop: "
                "an infeasible complete route is never a ranked candidate (v2 section 14, D32), so "
                "the stored recommendation contradicts the stored violations"
            )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _require_storable_policy(self, policy: object) -> None:
        if not isinstance(policy, RouteCostPolicy):
            raise StorageError(
                f"cost_policy must be a RouteCostPolicy, got {type(policy).__name__}"
            )
        if policy.name not in REGISTERED_COST_POLICIES:
            raise StorageError(
                f"cost policy name {policy.name!r} is not a policy this build implements "
                f"({', '.join(sorted(REGISTERED_COST_POLICIES))}); its weights would be stored "
                "without the policy identity that gives them meaning, so the run could never be "
                "loaded back (D13/D16/D35: weights are explicit configuration)"
            )
        if dict(policy.declarations) != default_component_declarations():
            raise StorageError(
                f"cost policy {policy.name!r} carries capability declarations that are not the "
                "code's registry; declarations are static capability metadata rebuilt from code on "
                "load and are never persisted, so storing this policy would silently change it"
            )

    @staticmethod
    def _describe_integrity_error(run: OptimizationRun, error: sqlite3.IntegrityError) -> str:
        detail = str(error)
        if "FOREIGN KEY" in detail.upper():
            return (
                f"run {run.id!r} cannot be appended: no plan {str(run.plan_id)!r} exists. A run "
                "records what happened to a stored plan, so this repository never creates one "
                "(schema section 5: plan_id is a foreign key). Save the plan first."
            )
        if "UNIQUE" in detail.upper() or "PRIMARY KEY" in detail.upper():
            return (
                f"run {run.id!r} is already stored: run history is append-only, so a second append "
                "with the same id is refused instead of overwriting the historical row"
            )
        return f"run {run.id!r} could not be appended: {detail}"

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
        """Guarantee the plan reference this repository relies on (schema section 5)."""
        connection.execute("PRAGMA foreign_keys = ON")
        if not connection.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise StorageError(
                "could not enforce PRAGMA foreign_keys on this connection; a run could then be "
                "appended for a plan that does not exist, so the repository refuses to run"
            )

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        missing = [
            table
            for table in ("route_plans", "route_optimization_runs")
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
def _json_column(value: object, column: str, run_id: object) -> str:
    """A JSON column must be TEXT; the content is then the domain codec's business."""
    if not isinstance(value, str):
        raise StoredRunError(
            f"run {run_id!r}: {column} must be TEXT holding JSON, got {type(value).__name__}"
        )
    return value


def _utc_z_text(text: object, field: str) -> str:
    """Validate the storage timestamp convention and return the text unchanged.

    Only ``YYYY-MM-DDTHH:MM:SSZ`` is accepted, and the calendar is checked as well: a shape-valid
    timestamp that names no instant (``2026-02-30T01:00:00Z``) is refused as a
    :class:`~storage.StoredRunError` naming the value, so the interpreter's own ``ValueError`` never
    escapes the read path (D26; D38 acceptance item 5).
    """
    if not isinstance(text, str) or not _UTC_Z_RE.match(text):
        raise StoredRunError(
            f"{field}={text!r} is not UTC ISO-8601 with a trailing Z "
            "(for example 2026-09-11T01:00:00Z, schema section 1)"
        )
    try:
        datetime.strptime(text, _UTC_FORMAT)
    except (ValueError, OverflowError):
        raise StoredRunError(
            f"{field}={text!r} has the stored timestamp shape but is not a real calendar date and "
            "time, so it names no instant; stored timestamps are instants and an impossible date is "
            "refused rather than repaired (schema section 1)"
        ) from None
    return text


def _parse_utc_z(text: object, field: str) -> datetime:
    """The same validation, as a timezone-aware UTC ``datetime`` for the domain."""
    return datetime.strptime(_utc_z_text(text, field), _UTC_FORMAT).replace(tzinfo=timezone.utc)


def _cost_policy_from_json(text: object, run_id: object) -> RouteCostPolicy:
    """Rebuild the stored policy exactly as the plan repository does (one shared stored format).

    The weights are stored truth (D13/D16/D35) and are handed to :class:`RouteCostPolicy`, which
    re-validates every component name, rejects non-finite or negative weights and refuses to weight
    a component whose capability status is not ``implemented`` (D16). ``declarations`` are rebuilt
    from the code's registry instead of being persisted, because they are static capability metadata.
    """
    field = f"run {run_id!r}: route_optimization_runs.cost_policy_json"
    try:
        payload = json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError as error:
        raise StoredRunError(f"{field} is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise StoredRunError(f"{field} must hold a JSON object")
    found = set(payload)
    missing = sorted(_COST_POLICY_KEYS - found)
    unknown = sorted(found - _COST_POLICY_KEYS)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise StoredRunError(
            f"{field} does not have the stored shape ({', '.join(details)}); the reader refuses to "
            "guess at a payload written by another format"
        )

    name = payload["name"]
    if not isinstance(name, str) or not name.strip():
        raise StoredRunError(f"{field}: name={name!r} must be a non-empty policy name")
    if name not in REGISTERED_COST_POLICIES:
        raise InvalidCostPolicyError(
            f"stored cost policy name {name!r} is not a policy this build implements "
            f"({', '.join(sorted(REGISTERED_COST_POLICIES))}); the weights are explicit "
            "configuration and are never adopted under an unknown policy name"
        )
    weights = payload["weights"]
    if not isinstance(weights, dict):
        raise StoredRunError(
            f"{field}: weights must be a JSON object, got {type(weights).__name__}"
        )
    provisional = payload["provisional"]
    if not isinstance(provisional, bool):
        raise StoredRunError(f"{field}: provisional must be true or false, got {provisional!r}")
    notes = payload["notes"]
    if not isinstance(notes, str):
        raise StoredRunError(f"{field}: notes must be a string, got {notes!r}")

    return RouteCostPolicy(
        name=name,
        weights=dict(weights),
        declarations=default_component_declarations(),
        provisional=provisional,
        notes=notes,
    )
