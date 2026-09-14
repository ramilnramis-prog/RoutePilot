"""Stage 3 U10 (D38): plan and stop persistence with exact round-trip.

Covers the approved schema's plan/stop tables (``docs/STORAGE_SCHEMA.md`` sections 3, 4 and 7):

* exact round-trip through the SQLite repository (``RoutePlan == loaded RoutePlan``) for the shipped
  demo plan, the ~50-enabled-stop portfolio fixture, the ``awaiting_first_stop_choice`` state, an
  accepted-recommendation selection, per-stop ``window_end_policy`` overrides, all three window
  kinds, unknown durations with a plan default, disabled stops, gapped ``input_position`` values and
  non-ASCII text;
* **semantic** survival: after a reload the plan's ``inputs_fingerprint`` is unchanged and an actual
  exhaustive evaluation plus an optimizer run produce the same order, metrics, violations and
  timelines - demonstrated on a plan whose local wall-clock windows sit across a DST transition, so
  the strict DST re-validation of D3 really happens after the load;
* loud load-time validation (D38 acceptance item 5): rows are mutated with raw SQL after a legal
  save and the resulting failure is asserted to be exactly the domain's (or, for a malformed stored
  payload, storage's) own error type;
* repository semantics: one transaction per save, no duplicated stop rows, ``list`` order,
  ``get`` on an absent id, cascade deletion of stops and runs, ``input_position`` across
  update/disable/append, and a re-save that changes nothing observable.

Every database here is ``:memory:``: no files, no scratch directories, no network, no wall-clock
dependence (the repository's clock is injected), so the module is deterministic and offline.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import unittest
from datetime import datetime, time, timedelta, timezone
from typing import Any

from core.engine.first_stop.evaluation import evaluate_first_stop_candidates
from core.engine.optimizer.optimize import optimize
from core.engine.optimizer.route_problem import build_problem
from core.model.cost_policy import (
    DEMO_PROVISIONAL_POLICY_NAME,
    SMART_ROUTE_ELAPSED_POLICY_NAME,
    ComponentStatus,
    CostComponent,
    CostComponentDeclaration,
    RouteCostPolicy,
    default_component_declarations,
)
from core.model.first_stop import FirstStopIntent, FirstStopMode
from core.model.ids import StopId
from core.model.order_override import OrderOverrides
from core.model.route_plan import RoutePlan
from core.model.route_stop import GeocodeStatus
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.value_objects import DataProvenance
from core.repositories import RoutePlanRepository
from core.time import tzdata
from core.validation.errors import (
    InvalidCostPolicyError,
    InvalidRoutePlanError,
    InvalidRouteStopError,
    InvalidServiceWindowError,
    InvalidTimezoneNameError,
    UnknownTimezoneError,
    UnsupportedConstraintError,
    UnsupportedFeatureError,
    ValidationError,
)
from demo.dataset import build_demo_plan
from demo.scale_dataset import (
    PORTFOLIO_DISABLED_STOP_COUNT,
    PORTFOLIO_ENABLED_STOP_COUNT,
    build_portfolio_plan,
)
from storage import StoredPlanError, StorageError
from storage.sqlite.database import connect, migrate
from storage.sqlite.route_plan_repository import (
    ORDER_OVERRIDES_VERSION,
    SqliteRoutePlanRepository,
)
from tests.support import BERLIN, FixedTravelMatrix, build_plan, place, stop, utc

PROVENANCE = DataProvenance.DEMO_SYNTHETIC
OTHER_PROVENANCE = DataProvenance.REAL_ROUTING

#: The repository clock starts here in every test; a fixed value keeps stored timestamps exact.
CLOCK_START = datetime(2026, 9, 11, 5, 0, 0, tzinfo=timezone.utc)

#: Whether the machine can resolve IANA zones at all. Existence of a zone can only be validated
#: when a database is reachable (``tests/__init__`` activates the documented offline fallback).
TZDATA_AVAILABLE = tzdata.probe_tzdata().is_available

#: Berlin's 2026 spring-forward: 02:00 local -> 03:00 local on 2026-03-29, so 02:xx does not exist.
DST_TRANSITION_DATE = datetime(2026, 3, 29, tzinfo=timezone.utc)

#: What the Berlin wall clock 03:30 resolves to on that date (CEST, UTC+2). Before the transition
#: the same local time would have been 02:30 UTC, so this value proves the reloaded wall clock was
#: re-resolved under the post-transition offset instead of a stored instant.
BERLIN_WINDOW_OPEN_AFTER_TRANSITION = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


class DeterministicClock:
    """A clock the tests control: each call returns the current instant, then advances it."""

    def __init__(self, start: datetime, *, step_seconds: int = 0) -> None:
        self.current = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        moment = self.current
        self.current = self.current + self._step
        return moment


class RepositoryTestCase(unittest.TestCase):
    """Shared ``:memory:`` database, schema and repository."""

    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        self.clock = DeterministicClock(CLOCK_START)
        self.repository = self.make_repository()

    def make_repository(
        self,
        *,
        connection: sqlite3.Connection | None = None,
        clock: Any = None,
        provenance: DataProvenance | str = PROVENANCE,
    ) -> SqliteRoutePlanRepository:
        return SqliteRoutePlanRepository(
            connection if connection is not None else self.connection,
            data_provenance=provenance,
            clock=clock if clock is not None else self.clock,
        )

    # -- raw SQL helpers (used to hand-edit a legally saved database) -------- #
    def _update(self, table: str, key: str, value: str, assignments: dict[str, Any]) -> None:
        columns = ", ".join(f"{column} = ?" for column in assignments)
        with self.connection:
            self.connection.execute(
                f"UPDATE {table} SET {columns} WHERE {key} = ?",
                (*assignments.values(), value),
            )

    def update_plan(self, **assignments: Any) -> None:
        self._update("route_plans", "id", str(self.plan.id), assignments)

    def update_stop(self, stop_id: str, **assignments: Any) -> None:
        self._update("route_stops", "id", stop_id, assignments)

    def sql(self, query: str, *parameters: Any) -> list[sqlite3.Row]:
        return list(self.connection.execute(query, parameters).fetchall())

    def scalar(self, query: str, *parameters: Any) -> Any:
        return self.connection.execute(query, parameters).fetchone()[0]

    def stop_rows(self) -> list[sqlite3.Row]:
        return self.sql("SELECT * FROM route_stops ORDER BY input_position")

    def dump(self) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        """Every plan and stop row, in a stable order, as plain tuples."""
        plans = [tuple(row) for row in self.sql("SELECT * FROM route_plans ORDER BY id")]
        stops = [tuple(row) for row in self.sql("SELECT * FROM route_stops ORDER BY id")]
        return plans, stops

    # -- round-trip helper -------------------------------------------------- #
    def assert_round_trip(self, plan: RoutePlan) -> RoutePlan:
        """Save, load, and prove the loaded plan is the saved plan plus its row-level provenance."""
        self.repository.save(plan)
        loaded = self.repository.get(plan.id)
        self.assertIsNotNone(loaded, msg=f"plan {plan.id!r} did not come back")
        self.assertEqual(loaded, plan, msg="domain value equality must hold after a round-trip")

        rows = self.stop_rows()
        self.assertEqual(
            [row["id"] for row in rows],
            [some_stop.id for some_stop in plan.stops],
            msg="stored stops must be exactly the plan's stops, in input_position order",
        )
        self.assertEqual(
            [row["input_position"] for row in rows],
            [some_stop.input_position for some_stop in plan.stops],
        )
        return loaded  # type: ignore[return-value]


def _plain_plan(plan_id: str = "round-trip") -> RoutePlan:
    """A small valid plan used by the round-trip matrix."""
    return build_plan(
        stop("a", 55.80, 37.70),
        stop("b", 55.82, 37.72, service_duration=None),
        stop("c", 55.84, 37.74, enabled=False),
        plan_id=plan_id,
        default_service_duration=300,
        input_positions=[0, 1, 4],
    )


# --------------------------------------------------------------------------- #
# the port itself
# --------------------------------------------------------------------------- #
class RepositoryPortTests(RepositoryTestCase):
    """``core/repositories.py`` is a pure port: core types only, no storage, no SQL."""

    def test_the_sqlite_repository_is_an_instance_of_the_port(self) -> None:
        self.assertIsInstance(self.repository, RoutePlanRepository)

    def test_the_port_speaks_domain_types_only(self) -> None:
        import typing

        from core.model.ids import PlanId

        hints = typing.get_type_hints(RoutePlanRepository.save)
        self.assertIs(hints["plan"], RoutePlan)
        self.assertIs(typing.get_type_hints(RoutePlanRepository.get)["plan_id"], PlanId)
        self.assertEqual(
            typing.get_type_hints(RoutePlanRepository.get)["return"], RoutePlan | None
        )
        self.assertEqual(
            typing.get_type_hints(RoutePlanRepository.list)["return"], tuple[RoutePlan, ...]
        )
        self.assertIs(typing.get_type_hints(RoutePlanRepository.delete)["return"], bool)

    def test_the_port_declares_the_schema_section_7_signatures(self) -> None:
        import inspect

        self.assertEqual(list(inspect.signature(RoutePlanRepository.save).parameters), ["self", "plan"])
        self.assertEqual(
            list(inspect.signature(RoutePlanRepository.get).parameters), ["self", "plan_id"]
        )
        self.assertEqual(list(inspect.signature(RoutePlanRepository.list).parameters), ["self"])
        self.assertEqual(
            list(inspect.signature(RoutePlanRepository.delete).parameters), ["self", "plan_id"]
        )

    def test_a_non_storage_implementation_satisfies_the_port(self) -> None:
        """The port is implementable with pure core types - that is what keeps it in ``core/``."""

        class InMemory:
            def __init__(self) -> None:
                self.plans: dict[str, RoutePlan] = {}

            def save(self, plan: RoutePlan) -> None:
                self.plans[plan.id] = plan

            def get(self, plan_id: str) -> RoutePlan | None:
                return self.plans.get(plan_id)

            def list(self) -> tuple[RoutePlan, ...]:
                return tuple(self.plans.values())

            def delete(self, plan_id: str) -> bool:
                return self.plans.pop(plan_id, None) is not None

        self.assertIsInstance(InMemory(), RoutePlanRepository)


# --------------------------------------------------------------------------- #
# Deliverable 6: the round-trip matrix
# --------------------------------------------------------------------------- #
class RoundTripTests(RepositoryTestCase):
    def test_the_shipped_demo_plan_round_trips_exactly(self) -> None:
        plan = build_demo_plan()
        self.assertEqual((len(plan.active_stops()), len(plan.disabled_stops())), (31, 1))
        self.assert_round_trip(plan)

    def test_the_portfolio_fixture_round_trips_exactly(self) -> None:
        plan = build_portfolio_plan()
        self.assertEqual(len(plan.active_stops()), PORTFOLIO_ENABLED_STOP_COUNT)
        self.assertEqual(len(plan.disabled_stops()), PORTFOLIO_DISABLED_STOP_COUNT)
        self.assert_round_trip(plan)

    def test_a_plan_awaiting_the_first_stop_choice_round_trips_with_no_selection(self) -> None:
        from core.model.first_stop import FirstStopState

        plan = _plain_plan("awaiting")
        self.assertIs(plan.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)
        loaded = self.assert_round_trip(plan)
        self.assertIs(loaded.first_stop_state, FirstStopState.AWAITING_FIRST_STOP_CHOICE)

        row = self.sql("SELECT * FROM route_plans WHERE id = 'awaiting'")[0]
        self.assertEqual(row["first_stop_mode"], "recommend")
        self.assertIsNone(row["first_stop_selected_stop_id"])
        self.assertIsNone(row["first_stop_selection_source"])
        self.assertEqual(row["first_stop_pinned"], 0)

    def test_an_accepted_recommendation_selection_round_trips(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            plan_id="accepted",
            first_service_stop=FirstStopIntent.accepted_recommendation(StopId("b")),
            order_overrides=OrderOverrides.first_stop(StopId("b")),
            input_positions=[0, 1],
        )
        loaded = self.assert_round_trip(plan)
        self.assertEqual(loaded.first_service_stop.selected_stop_id, "b")
        self.assertEqual(loaded.order_overrides.first_stop_id(), "b")

        row = self.sql("SELECT * FROM route_plans WHERE id = 'accepted'")[0]
        self.assertEqual(row["first_stop_mode"], "recommend")
        self.assertEqual(row["first_stop_selected_stop_id"], "b")
        self.assertEqual(row["first_stop_selection_source"], "accepted_recommendation")
        self.assertEqual(row["first_stop_pinned"], 1)
        self.assertEqual(
            json.loads(row["order_overrides_json"]),
            {
                "version": ORDER_OVERRIDES_VERSION,
                "constraints": [{"kind": "first_stop", "stop_id": "b", "position": None}],
            },
        )

    def test_a_manual_choice_selection_round_trips(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            plan_id="manual",
            first_service_stop=FirstStopIntent.manual_choice(
                StopId("b"), mode=FirstStopMode.MANUAL
            ),
            input_positions=[0, 1],
        )
        self.assert_round_trip(plan)
        row = self.sql("SELECT * FROM route_plans WHERE id = 'manual'")[0]
        self.assertEqual(row["first_stop_mode"], "manual")
        self.assertEqual(row["first_stop_selection_source"], "manual_choice")
        self.assertEqual(row["first_stop_pinned"], 1)

    def test_per_stop_window_end_policy_overrides_round_trip(self) -> None:
        plan = build_plan(
            stop(
                "a",
                55.80,
                37.70,
                window=ServiceWindow.fixed(time(8, 0), time(18, 0)),
            ),
            stop(
                "b",
                55.82,
                37.72,
                window=ServiceWindow.fixed(
                    time(9, 0), time(17, 0), window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END
                ),
            ),
            plan_id="overrides",
            window_end_policy=WindowEndPolicy.SERVICE_FINISH_BEFORE_END,
            input_positions=[0, 1],
        )
        loaded = self.assert_round_trip(plan)
        self.assertEqual(
            loaded.stop_by_id(StopId("a")).service_window.window_end_policy, None
        )
        self.assertIs(
            loaded.stop_by_id(StopId("b")).service_window.window_end_policy,
            WindowEndPolicy.SERVICE_START_BEFORE_END,
        )

        stored = {row["id"]: row["window_end_policy"] for row in self.stop_rows()}
        self.assertIsNone(stored["a"], msg="NULL means 'inherit the plan default' (D29)")
        self.assertEqual(stored["b"], "service_start_before_end")
        row = self.sql("SELECT window_end_policy FROM route_plans WHERE id = 'overrides'")[0]
        self.assertEqual(row["window_end_policy"], "service_finish_before_end")

    def test_all_three_window_kinds_round_trip(self) -> None:
        plan = build_plan(
            stop("fixed", 55.80, 37.70, window=ServiceWindow.fixed(time(8, 0), time(18, 0))),
            stop("open", 55.82, 37.72, window=ServiceWindow.unrestricted()),
            stop("unknown", 55.84, 37.74, window=ServiceWindow.unknown()),
            plan_id="kinds",
            input_positions=[0, 1, 2],
        )
        self.assert_round_trip(plan)
        stored = {row["id"]: row for row in self.stop_rows()}
        self.assertEqual(stored["fixed"]["service_window_kind"], "fixed")
        self.assertEqual(stored["fixed"]["service_window_start"], "08:00:00")
        self.assertEqual(stored["fixed"]["service_window_end"], "18:00:00")
        self.assertEqual(stored["open"]["service_window_kind"], "unrestricted")
        self.assertEqual(stored["unknown"]["service_window_kind"], "unknown")
        for row in (stored["open"], stored["unknown"]):
            self.assertIsNone(row["service_window_start"])
            self.assertIsNone(row["service_window_end"])

    def test_unknown_duration_with_a_plan_default_round_trips_as_null(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70, service_duration=None),
            plan_id="duration",
            default_service_duration=420,
            input_positions=[0],
        )
        loaded = self.assert_round_trip(plan)
        self.assertIsNone(loaded.stops[0].service_duration)
        self.assertEqual(loaded.default_service_duration, 420)
        row = self.stop_rows()[0]
        self.assertIsNone(row["service_duration_sec"], msg="unknown is stored as NULL, never 0")
        self.assertEqual(
            self.scalar("SELECT default_service_duration_sec FROM route_plans WHERE id = 'duration'"),
            420,
        )

    def test_disabled_stops_survive_without_renumbering(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72, enabled=False),
            stop("c", 55.84, 37.74),
            plan_id="disabled",
            input_positions=[0, 1, 2],
        )
        loaded = self.assert_round_trip(plan)
        self.assertEqual([some_stop.id for some_stop in loaded.disabled_stops()], ["b"])
        self.assertEqual([some_stop.id for some_stop in loaded.active_stops()], ["a", "c"])
        self.assertEqual(loaded.user_baseline_order(), (StopId("a"), StopId("c")))
        stored = {row["id"]: row["enabled"] for row in self.stop_rows()}
        self.assertEqual(stored, {"a": 1, "b": 0, "c": 1})

    def test_gaps_in_input_position_round_trip(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            stop("c", 55.84, 37.74),
            plan_id="gaps",
            input_positions=[0, 4, 9],
        )
        loaded = self.assert_round_trip(plan)
        self.assertEqual([some_stop.input_position for some_stop in loaded.stops], [0, 4, 9])

    def test_non_ascii_text_round_trips(self) -> None:
        plan = build_plan(
            dataclasses.replace(
                stop("a", 55.80, 37.70),
                raw_address="Улица Ленина 5, Москва",
                normalized_address="Straße 5 — München",
                notes="позвонить за 10 минут 🚚",
            ),
            plan_id="non-ascii",
            input_positions=[0],
        )
        loaded = self.assert_round_trip(plan)
        self.assertEqual(loaded.stops[0].raw_address, "Улица Ленина 5, Москва")
        self.assertEqual(loaded.stops[0].normalized_address, "Straße 5 — München")
        self.assertEqual(loaded.stops[0].notes, "позвонить за 10 минут 🚚")
        row = self.stop_rows()[0]
        self.assertEqual(row["notes"], "позвонить за 10 минут 🚚")

    def test_start_and_finish_are_plan_columns_and_never_stop_rows(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            plan_id="locations",
            departure=place("Warehouse", 55.75, 37.62),
            finish=place("Depot", 55.70, 37.55),
            input_positions=[0, 1],
        )
        self.assert_round_trip(plan)

        row = self.sql("SELECT * FROM route_plans WHERE id = 'locations'")[0]
        self.assertEqual(row["departure_label"], "Warehouse")
        self.assertEqual(row["departure_latitude"], 55.75)
        self.assertEqual(row["departure_longitude"], 37.62)
        self.assertEqual(row["finish_label"], "Depot")
        self.assertEqual(row["finish_latitude"], 55.70)
        self.assertEqual(row["finish_longitude"], 37.55)
        # START and FINISH are a PlaceRef, a different type from a service stop (I1/I2): no code
        # path can write them as route_stops rows, and the row set is exactly the plan's stops.
        labels = {row["departure_label"], row["finish_label"]}
        self.assertEqual(len(self.stop_rows()), len(plan.stops))
        self.assertFalse(labels & {row["id"] for row in self.stop_rows()})

    def test_stored_windows_are_local_wall_clock_and_timestamps_carry_z(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70, window=ServiceWindow.fixed(time(8, 15), time(18, 45))),
            plan_id="wall-clock",
            departure_time=utc(2026, 9, 11, 1, 0),
            input_positions=[0],
        )
        self.assert_round_trip(plan)
        row = self.stop_rows()[0]
        self.assertEqual(row["service_window_start"], "08:15:00")
        self.assertEqual(row["service_window_end"], "18:45:00")
        plan_row = self.sql("SELECT * FROM route_plans WHERE id = 'wall-clock'")[0]
        self.assertEqual(plan_row["departure_time_utc"], "2026-09-11T01:00:00Z")
        self.assertEqual(plan_row["created_at_utc"], "2026-09-11T05:00:00Z")
        self.assertEqual(plan_row["updated_at_utc"], "2026-09-11T05:00:00Z")

    def test_columns_the_domain_does_not_model_are_handled_honestly(self) -> None:
        plan = _plain_plan("honest")
        self.assert_round_trip(plan)
        row = self.sql("SELECT * FROM route_plans WHERE id = 'honest'")[0]
        self.assertIsNone(row["name"], msg="no domain counterpart: never invented")
        self.assertEqual(row["data_provenance"], PROVENANCE.value)
        self.assertEqual(
            row["inputs_fingerprint"],
            plan.inputs_fingerprint(),
            msg="the plan-level fingerprint is recomputable and stored, not trusted",
        )
        for stop_row in self.stop_rows():
            self.assertIsNone(stop_row["geocode_provider"])
            self.assertIsNone(stop_row["geocode_checked_at_utc"])
            self.assertEqual(stop_row["created_at_utc"], "2026-09-11T05:00:00Z")
            self.assertEqual(stop_row["updated_at_utc"], "2026-09-11T05:00:00Z")

    def test_a_stored_plan_never_carries_a_recommendation(self) -> None:
        # The engine recommends, the driver decides (D4/D32): there is no recommended_stop_id
        # column anywhere, and none may be added.
        self.assert_round_trip(_plain_plan("no-recommendation"))
        for row in self.sql("SELECT * FROM route_plans") + self.sql("SELECT * FROM route_stops"):
            self.assertNotIn("recommended_stop_id", row.keys())


# --------------------------------------------------------------------------- #
# save/list/get/delete semantics
# --------------------------------------------------------------------------- #
class SaveSemanticsTests(RepositoryTestCase):
    def test_saving_twice_does_not_duplicate_stop_rows(self) -> None:
        plan = _plain_plan("twice")
        self.repository.save(plan)
        self.repository.save(plan)
        self.assertEqual(len(self.stop_rows()), len(plan.stops))
        positions = [row["input_position"] for row in self.stop_rows()]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertEqual(self.repository.get(plan.id), plan)

    def test_save_preserves_created_at_and_refreshes_updated_at(self) -> None:
        self.clock = DeterministicClock(CLOCK_START, step_seconds=30)
        self.repository = self.make_repository()
        plan = _plain_plan("timestamps")
        self.repository.save(plan)
        first = self.sql("SELECT * FROM route_plans WHERE id = 'timestamps'")[0]
        self.repository.save(plan)
        second = self.sql("SELECT * FROM route_plans WHERE id = 'timestamps'")[0]
        self.assertEqual(first["created_at_utc"], "2026-09-11T05:00:00Z")
        self.assertEqual(first["updated_at_utc"], "2026-09-11T05:00:00Z")
        self.assertEqual(second["created_at_utc"], first["created_at_utc"])
        self.assertEqual(second["updated_at_utc"], "2026-09-11T05:00:30Z")

    def test_a_stop_that_stays_in_the_plan_keeps_its_creation_timestamp(self) -> None:
        self.clock = DeterministicClock(CLOCK_START, step_seconds=30)
        self.repository = self.make_repository()
        plan = _plain_plan("stop-timestamps")
        self.repository.save(plan)
        self.repository.save(plan)
        for row in self.stop_rows():
            self.assertEqual(row["created_at_utc"], "2026-09-11T05:00:00Z")
            self.assertEqual(row["updated_at_utc"], "2026-09-11T05:00:30Z")

    def test_a_failed_save_leaves_nothing_behind(self) -> None:
        """One transaction: a mid-save failure rolls the whole save back."""
        first = _plain_plan("plan-a")
        self.repository.save(first)
        clashing = build_plan(
            stop("b", 55.82, 37.72), stop(first.stops[0].id, 55.86, 37.76),
            plan_id="plan-b", input_positions=[0, 1],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save(clashing)

        self.assertIsNone(self.repository.get(clashing.id), msg="no partial plan row survived")
        self.assertEqual(self.repository.get(first.id), first)
        self.assertEqual(len(self.stop_rows()), len(first.stops))

    def test_save_refuses_an_unregistered_cost_policy_name(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            plan_id="unknown-policy",
            cost_policy=RouteCostPolicy(
                name="custom_policy_v3",
                weights={CostComponent.TRAVEL_TIME: 1.0},
            ),
            input_positions=[0],
        )
        with self.assertRaises(StorageError) as context:
            self.repository.save(plan)
        self.assertIn("custom_policy_v3", str(context.exception))
        self.assertEqual(self.sql("SELECT * FROM route_plans"), [])

    def test_save_refuses_a_policy_whose_declarations_are_not_the_code_registry(self) -> None:
        declarations = default_component_declarations()
        declarations[CostComponent.PRIORITY_PENALTY] = CostComponentDeclaration(
            CostComponent.PRIORITY_PENALTY, ComponentStatus.UNSUPPORTED, None, "edited by hand"
        )
        plan = build_plan(
            stop("a", 55.80, 37.70),
            plan_id="foreign-declarations",
            cost_policy=RouteCostPolicy(
                name=SMART_ROUTE_ELAPSED_POLICY_NAME,
                weights={CostComponent.TRAVEL_TIME: 1.0, CostComponent.WAITING_TIME: 1.0},
                declarations=declarations,
            ),
            input_positions=[0],
        )
        with self.assertRaises(StorageError) as context:
            self.repository.save(plan)
        self.assertIn("declarations", str(context.exception))

    def test_save_refuses_a_departure_time_with_sub_second_precision(self) -> None:
        plan = build_plan(
            stop("a", 55.80, 37.70),
            plan_id="sub-second",
            departure_time=datetime(2026, 9, 11, 1, 0, 0, 500000, tzinfo=timezone.utc),
            input_positions=[0],
        )
        with self.assertRaises(StorageError) as context:
            self.repository.save(plan)
        self.assertIn("sub-second", str(context.exception))
        self.assertEqual(self.sql("SELECT * FROM route_plans"), [])

    def test_changing_the_selected_first_stop_across_saves(self) -> None:
        first = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            plan_id="reselect",
            first_service_stop=FirstStopIntent.accepted_recommendation(StopId("a")),
            order_overrides=OrderOverrides.first_stop(StopId("a")),
            input_positions=[0, 1],
        )
        self.repository.save(first)
        second = dataclasses.replace(
            first,
            first_service_stop=FirstStopIntent.manual_choice(StopId("b")),
            order_overrides=OrderOverrides.first_stop(StopId("b")),
        )
        self.repository.save(second)
        self.assertEqual(self.repository.get("reselect"), second)
        row = self.sql("SELECT * FROM route_plans WHERE id = 'reselect'")[0]
        self.assertEqual(row["first_stop_selected_stop_id"], "b")
        self.assertEqual(row["first_stop_selection_source"], "manual_choice")

    def test_a_save_that_drops_the_previously_selected_stop_is_allowed(self) -> None:
        """The transaction clears the selection reference before the old stop rows are deleted."""
        first = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            plan_id="dropped-selection",
            first_service_stop=FirstStopIntent.accepted_recommendation(StopId("b")),
            order_overrides=OrderOverrides.first_stop(StopId("b")),
            input_positions=[0, 1],
        )
        self.repository.save(first)
        without_b = dataclasses.replace(
            first,
            stops=(first.stops[0],),
            first_service_stop=FirstStopIntent.recommend(),
            order_overrides=OrderOverrides.empty(),
        )
        self.repository.save(without_b)
        self.assertEqual(self.repository.get("dropped-selection"), without_b)
        self.assertEqual([row["id"] for row in self.stop_rows()], ["a"])

    def test_repository_refuses_an_unmigrated_database(self) -> None:
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(StorageError) as context:
            self.make_repository(connection=connection)
        self.assertIn("migrate", str(context.exception))

    def test_repository_refuses_an_unknown_provenance_argument(self) -> None:
        with self.assertRaises(StorageError) as context:
            self.make_repository(provenance="MADE_UP")
        self.assertIn("MADE_UP", str(context.exception))

    def test_repository_re_enables_foreign_keys_on_the_connection_it_is_given(self) -> None:
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        migrate(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)

        self.make_repository(connection=connection)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_database_without_a_sqlite_row_factory_is_refused(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        migrate(connection)
        connection.row_factory = None
        with self.assertRaises(StorageError) as context:
            self.make_repository(connection=connection)
        self.assertIn("row_factory", str(context.exception))

    def test_a_naive_clock_is_refused_instead_of_assumed_to_be_utc(self) -> None:
        plan = _plain_plan("naive-clock")
        self.repository = self.make_repository(clock=lambda: datetime(2026, 9, 11, 5, 0, 0))
        with self.assertRaises(StorageError) as context:
            self.repository.save(plan)
        self.assertIn("timezone-aware", str(context.exception))


class ListGetDeleteTests(RepositoryTestCase):
    def test_get_returns_none_for_an_absent_plan(self) -> None:
        self.assertIsNone(self.repository.get("no-such-plan"))

    def test_list_is_empty_on_a_fresh_database(self) -> None:
        self.assertEqual(self.repository.list(), ())

    def test_list_is_ordered_by_creation_time(self) -> None:
        self.clock = DeterministicClock(CLOCK_START, step_seconds=60)
        self.repository = self.make_repository()
        self.repository.save(_plain_plan("second-lexically"))
        self.repository.save(build_plan(stop("z", 55.80, 37.70), plan_id="aaa", input_positions=[0]))
        self.assertEqual([plan.id for plan in self.repository.list()], ["second-lexically", "aaa"])

    def test_list_breaks_ties_by_id_deterministically(self) -> None:
        for plan_id in ("c-plan", "a-plan", "b-plan"):
            self.repository.save(
                build_plan(
                    stop(f"stop-{plan_id}", 55.80, 37.70),
                    plan_id=plan_id,
                    input_positions=[0],
                )
            )
        self.assertEqual(
            [plan.id for plan in self.repository.list()], ["a-plan", "b-plan", "c-plan"]
        )

    def test_delete_removes_the_plan_its_stops_and_its_runs(self) -> None:
        plan = _plain_plan("cascade")
        self.repository.save(plan)
        self._insert_run(plan.id)

        self.assertTrue(self.repository.delete(plan.id))
        self.assertIsNone(self.repository.get(plan.id))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_stops"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM route_optimization_runs"), 0)

    def test_delete_of_an_absent_plan_is_false(self) -> None:
        self.assertFalse(self.repository.delete("no-such-plan"))

    def test_delete_leaves_other_plans_untouched(self) -> None:
        keep = _plain_plan("keep")
        drop = build_plan(stop("z", 55.86, 37.76), plan_id="drop", input_positions=[0])
        self.repository.save(keep)
        self.repository.save(drop)
        self.assertTrue(self.repository.delete(drop.id))
        self.assertEqual(self.repository.list(), (keep,))
        self.assertEqual(len(self.stop_rows()), len(keep.stops))

    def _insert_run(self, plan_id: str) -> None:
        """One run row, so the cascade to ``route_optimization_runs`` is proved (U11 owns the repo).

        The run repository does not exist yet, so the row is written with raw SQL; every NOT NULL
        column of the approved schema is satisfied literally.
        """
        now = "2026-09-11T05:00:00Z"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO route_optimization_runs (
                    id, plan_id, run_kind, algorithm, algorithm_version, inputs_fingerprint,
                    route_fingerprint, cost_policy_json, data_provenance, status, order_json,
                    first_stop_recommendation_json, violations_json, metrics_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run-1",
                    str(plan_id),
                    "optimize",
                    "greedy_seed+2opt",
                    "1",
                    "inputs-digest",
                    "route-digest",
                    "{}",
                    PROVENANCE.value,
                    "ok",
                    "[]",
                    "{}",
                    "[]",
                    "{}",
                    now,
                ),
            )


class InputPositionLifecycleTests(RepositoryTestCase):
    def test_input_position_survives_update_disable_and_append(self) -> None:
        original = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            stop("c", 55.84, 37.74),
            stop("d", 55.86, 37.76),
            plan_id="lifecycle",
            input_positions=[0, 1, 4, 7],
        )
        self.repository.save(original)
        loaded = self.repository.get(original.id)
        self.assertEqual([some_stop.input_position for some_stop in loaded.stops], [0, 1, 4, 7])

        # 1. an ordinary update of content must not touch provenance
        updated = dataclasses.replace(
            loaded,
            stops=tuple(
                dataclasses.replace(some_stop, notes="updated")
                if some_stop.id == "b"
                else some_stop
                for some_stop in loaded.stops
            ),
        )
        self.repository.save(updated)
        reloaded = self.repository.get(original.id)
        self.assertEqual([some_stop.input_position for some_stop in reloaded.stops], [0, 1, 4, 7])

        # 2. disabling a stop excludes it from routing without renumbering the rest
        disabled = dataclasses.replace(
            reloaded,
            stops=tuple(
                dataclasses.replace(some_stop, enabled=False)
                if some_stop.id == "b"
                else some_stop
                for some_stop in reloaded.stops
            ),
        )
        self.repository.save(disabled)
        reloaded = self.repository.get(original.id)
        self.assertEqual([some_stop.input_position for some_stop in reloaded.stops], [0, 1, 4, 7])
        self.assertEqual([some_stop.id for some_stop in reloaded.active_stops()], ["a", "c", "d"])

        # 3. appending takes next_input_position(): max + 1, never a renumbering
        self.assertEqual(reloaded.next_input_position(), 8)
        appended = dataclasses.replace(
            reloaded,
            stops=reloaded.stops
            + (stop("e", 55.88, 37.78, input_position=reloaded.next_input_position()),),
        )
        self.repository.save(appended)
        reloaded = self.repository.get(original.id)
        self.assertEqual(
            [some_stop.input_position for some_stop in reloaded.stops], [0, 1, 4, 7, 8]
        )
        self.assertEqual(
            [row["input_position"] for row in self.stop_rows()], [0, 1, 4, 7, 8]
        )

    def test_resaving_a_loaded_plan_changes_nothing_observable(self) -> None:
        plan = _plain_plan("resave")
        self.repository.save(plan)
        before = self.dump()

        loaded = self.repository.get(plan.id)
        self.repository.save(loaded)
        after = self.dump()

        self.assertEqual(before, after, msg="a re-save must be a no-op on a stored plan")
        self.assertEqual(self.repository.get(plan.id), plan)


# --------------------------------------------------------------------------- #
# Deliverable 6: semantic survival, including strict DST re-validation after load
# --------------------------------------------------------------------------- #
class SemanticSurvivalTests(RepositoryTestCase):
    def _dst_plan(self) -> RoutePlan:
        """A Berlin plan whose fixed windows sit across the 2026-03-29 spring-forward."""
        return build_plan(
            stop(
                "S1",
                52.53,
                13.43,
                window=ServiceWindow.fixed(time(3, 30), time(6, 0)),
            ),
            stop(
                "S2",
                52.55,
                13.45,
                window=ServiceWindow.fixed(time(4, 0), time(8, 0)),
                service_duration=900,
            ),
            stop("S3", 52.57, 13.47, window=ServiceWindow.unrestricted()),
            stop(
                "S4",
                52.59,
                13.49,
                window=ServiceWindow.unknown(),
                service_duration=None,
            ),
            stop(
                "S5",
                52.61,
                13.51,
                window=ServiceWindow.fixed(
                    time(5, 0),
                    time(7, 0),
                    window_end_policy=WindowEndPolicy.SERVICE_START_BEFORE_END,
                ),
                priority=2,
            ),
            stop("S6", 52.63, 13.53, enabled=False),
            plan_id="dst-plan",
            timezone_name=BERLIN,
            departure=place("Depot", 52.50, 13.40),
            finish=place("End", 52.65, 13.55),
            departure_time=datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc),
            default_service_duration=300,
            first_service_stop=FirstStopIntent.accepted_recommendation(StopId("S2")),
            window_end_policy=WindowEndPolicy.SERVICE_FINISH_BEFORE_END,
            input_positions=[0, 1, 4, 7, 9, 12],
        )

    def test_a_reloaded_plan_evaluates_and_optimizes_identically(self) -> None:
        plan = self._dst_plan()
        self.assertLess(plan.departure_time, DST_TRANSITION_DATE)
        self.repository.save(plan)
        loaded = self.repository.get(plan.id)
        self.assertEqual(loaded, plan)

        # The inputs fingerprint is the domain's own digest of the routing inputs: identical after
        # a reload, so a cached recommendation cannot be invalidated by storage.
        self.assertEqual(loaded.inputs_fingerprint(), plan.inputs_fingerprint())

        matrix = FixedTravelMatrix()
        original_report = evaluate_first_stop_candidates(plan=plan, travel_matrix=matrix)
        loaded_report = evaluate_first_stop_candidates(plan=loaded, travel_matrix=matrix)

        self.assertEqual(
            [candidate.stop_id for candidate in original_report.ranked],
            [candidate.stop_id for candidate in loaded_report.ranked],
        )
        self.assertEqual(original_report.ranked, loaded_report.ranked)
        self.assertEqual(original_report.rejected, loaded_report.rejected)
        self.assertEqual(original_report.diagnostics, loaded_report.diagnostics)
        self.assertEqual(original_report.inputs_fingerprint, loaded_report.inputs_fingerprint)
        self.assertEqual(original_report, loaded_report)

        # An actual optimizer run on the reloaded plan, compared down to the timelines: the
        # complete route (order, metrics, violations, instants) is the same route.
        chosen = plan.first_service_stop.selected_stop_id
        original_route = optimize(
            build_problem(plan=plan, travel_matrix=matrix, first_stop_id=chosen)
        )
        loaded_route = optimize(
            build_problem(plan=loaded, travel_matrix=matrix, first_stop_id=chosen)
        )
        self.assertEqual(original_route.order, loaded_route.order)
        self.assertEqual(original_route.evaluation.metrics, loaded_route.evaluation.metrics)
        self.assertEqual(original_route.evaluation.violations, loaded_route.evaluation.violations)
        self.assertEqual(original_route.evaluation.timelines, loaded_route.evaluation.timelines)

    def test_computing_a_recommendation_never_writes_a_selection_to_storage(self) -> None:
        """A recommendation is derived and advisory: it is never persisted as plan state (D32)."""
        plan = build_plan(
            stop("a", 55.80, 37.70),
            stop("b", 55.82, 37.72),
            stop("c", 55.84, 37.74),
            plan_id="advisory",
            input_positions=[0, 1, 2],
        )
        self.repository.save(plan)

        report = evaluate_first_stop_candidates(plan=plan, travel_matrix=FixedTravelMatrix())
        self.assertIsNotNone(report.recommended_stop_id, msg="the fixture must recommend a stop")
        self.assertIsNone(plan.first_service_stop.selected_stop_id)

        row = self.sql("SELECT * FROM route_plans WHERE id = 'advisory'")[0]
        self.assertIsNone(row["first_stop_selected_stop_id"])
        self.assertIsNone(row["first_stop_selection_source"])
        self.assertEqual(row["first_stop_pinned"], 0)
        self.assertEqual(self.repository.get(plan.id), plan)

    def test_the_reloaded_wall_clock_window_is_re_resolved_under_strict_dst(self) -> None:
        plan = self._dst_plan()
        self.repository.save(plan)
        loaded = self.repository.get(plan.id)

        # Stored as a local wall clock, not as an instant and not with an offset.
        stored = {row["id"]: row for row in self.stop_rows()}
        self.assertEqual(stored["S1"]["service_window_start"], "03:30:00")

        matrix = FixedTravelMatrix()
        chosen = plan.first_service_stop.selected_stop_id
        route = optimize(build_problem(plan=loaded, travel_matrix=matrix, first_stop_id=chosen))
        timeline = {entry.stop_id: entry for entry in route.evaluation.timelines}["S1"]
        self.assertEqual(
            timeline.service_window_start,
            BERLIN_WINDOW_OPEN_AFTER_TRANSITION,
            msg=(
                "03:30 Berlin on the spring-forward date is 01:30 UTC (CEST); the pre-transition "
                "offset would have produced 02:30 UTC, so the reloaded wall clock was re-resolved "
                "by the strict DST rules of D3 rather than read back as a stored instant"
            ),
        )


# --------------------------------------------------------------------------- #
# Deliverable 5: loud load-time validation of a hand-edited database
# --------------------------------------------------------------------------- #
class LoadValidationTests(RepositoryTestCase):
    """Mutate a legally saved row with raw SQL, then require the exact error type."""

    def setUp(self) -> None:
        super().setUp()
        self.plan = build_plan(
            stop("a", 55.80, 37.70, window=ServiceWindow.fixed(time(8, 0), time(18, 0))),
            stop("b", 55.82, 37.72),
            stop("c", 55.84, 37.74, window=ServiceWindow.unknown()),
            stop(
                "d",
                None,
                None,
                geocode_status=GeocodeStatus.PENDING,
                window=ServiceWindow.unknown(),
                service_duration=None,
            ),
            plan_id="validation-plan",
            default_service_duration=300,
            first_service_stop=FirstStopIntent.accepted_recommendation(StopId("a")),
            input_positions=[0, 1, 4, 7],
        )
        self.repository.save(self.plan)
        self.assertEqual(self.repository.get(self.plan.id), self.plan)

    def assert_load_raises(self, error_type: type[BaseException], *, containing: str = "") -> None:
        with self.assertRaises(error_type) as context:
            self.repository.get(self.plan.id)
        self.assertIs(
            type(context.exception),
            error_type,
            msg=f"expected exactly {error_type.__name__}, got {type(context.exception).__name__}",
        )
        if containing:
            self.assertIn(containing, str(context.exception))

    def ignore_check_constraints(self) -> None:
        """Let a write through that the approved DDL's CHECK would otherwise refuse.

        Used only to reach the *domain's* own validation: the schema keeps a legal database legal,
        and the domain must still reject a value that a hand edit (or a foreign tool with the
        pragma) slipped past it.
        """
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.addCleanup(self.connection.execute, "PRAGMA ignore_check_constraints = OFF")
        self.assertEqual(self.connection.execute("PRAGMA ignore_check_constraints").fetchone()[0], 1)

    def cost_policy_json(self, **overrides: Any) -> str:
        payload = {
            "name": SMART_ROUTE_ELAPSED_POLICY_NAME,
            "weights": {"travel_time": 1.0, "waiting_time": 1.0},
            "provisional": False,
            "notes": "",
        }
        payload.update(overrides)
        return json.dumps(payload)

    # -- timezone ----------------------------------------------------------- #
    def test_a_syntactically_invalid_timezone_is_rejected(self) -> None:
        self.update_plan(timezone="Not A Zone")
        self.assert_load_raises(InvalidTimezoneNameError)

    @unittest.skipUnless(TZDATA_AVAILABLE, "no IANA database: zone existence cannot be checked")
    def test_an_unknown_iana_timezone_is_rejected(self) -> None:
        self.update_plan(timezone="Mars/Olympus")
        self.assert_load_raises(UnknownTimezoneError, containing="Mars/Olympus")

    # -- cost policy -------------------------------------------------------- #
    def test_an_unknown_cost_policy_name_is_rejected(self) -> None:
        self.update_plan(cost_policy_json=self.cost_policy_json(name="mystery_policy_v9"))
        self.assert_load_raises(InvalidCostPolicyError, containing="mystery_policy_v9")

    def test_a_known_non_default_cost_policy_name_still_loads(self) -> None:
        self.update_plan(
            cost_policy_json=self.cost_policy_json(
                name=DEMO_PROVISIONAL_POLICY_NAME,
                weights={"travel_time": 1.0, "waiting_time": 2.0},
                provisional=True,
                notes="D31 sensitivity study",
            )
        )
        loaded = self.repository.get(self.plan.id)
        self.assertEqual(loaded.cost_policy.name, DEMO_PROVISIONAL_POLICY_NAME)
        self.assertEqual(loaded.cost_policy.weight(CostComponent.WAITING_TIME), 2.0)
        self.assertTrue(loaded.cost_policy.provisional)
        self.assertEqual(loaded.cost_policy.notes, "D31 sensitivity study")

    def test_a_malformed_cost_policy_payload_is_rejected(self) -> None:
        for payload, expected in (
            ("{not json at all", StoredPlanError),
            ("[1, 2, 3]", StoredPlanError),
            ('{"name": "smart_route_elapsed_v1", "weights": {}, "provisional": false}', StoredPlanError),
            ('{"name": "smart_route_elapsed_v1", "weights": [], "provisional": false, "notes": ""}', StoredPlanError),
            ('{"name": "smart_route_elapsed_v1", "weights": {}, "provisional": "yes", "notes": ""}', StoredPlanError),
            ('{"name": "", "weights": {}, "provisional": false, "notes": ""}', StoredPlanError),
            ('{"name": "smart_route_elapsed_v1", "weights": {}, "provisional": false, "notes": "", "extra": 1}', StoredPlanError),
        ):
            with self.subTest(payload=payload):
                self.update_plan(cost_policy_json=payload)
                self.assert_load_raises(expected)

    def test_cost_policy_content_is_re_validated_by_the_domain(self) -> None:
        cases = {
            "unknown component": (
                self.cost_policy_json(weights={"warp_drive": 1.0}),
                InvalidCostPolicyError,
            ),
            "negative weight": (
                self.cost_policy_json(weights={"travel_time": -1.0}),
                InvalidCostPolicyError,
            ),
            "unimplemented component": (
                self.cost_policy_json(weights={"u_turn_penalty": 1.0}),
                UnsupportedFeatureError,
            ),
        }
        for label, (payload, expected) in cases.items():
            with self.subTest(case=label):
                self.update_plan(cost_policy_json=payload)
                self.assert_load_raises(expected)

    # -- order overrides ---------------------------------------------------- #
    def test_an_unknown_order_override_version_is_rejected(self) -> None:
        for payload in (
            '{"version": 2, "constraints": []}',
            '{"version": "1", "constraints": []}',
            '{"constraints": []}',
        ):
            with self.subTest(payload=payload):
                self.update_plan(order_overrides_json=payload)
                self.assert_load_raises(StoredPlanError)

    def test_a_malformed_order_override_payload_is_rejected(self) -> None:
        for payload in (
            "{}",
            "[]",
            '{"version": 1, "constraints": {}}',
            '{"version": 1, "constraints": [1]}',
            '{"version": 1, "constraints": [{"kind": "first_stop"}]}',
            '{"version": 1, "constraints": [], "extra": true}',
        ):
            with self.subTest(payload=payload):
                self.update_plan(order_overrides_json=payload)
                self.assert_load_raises(StoredPlanError)

    def test_an_unknown_constraint_kind_is_rejected_by_the_domain(self) -> None:
        self.update_plan(
            order_overrides_json=(
                '{"version": 1, "constraints": '
                '[{"kind": "zigzag", "stop_id": "a", "position": null}]}'
            )
        )
        self.assert_load_raises(InvalidRoutePlanError, containing="zigzag")

    def test_a_declared_but_unimplemented_constraint_kind_is_not_silently_ignored(self) -> None:
        self.update_plan(
            order_overrides_json=(
                '{"version": 1, "constraints": '
                '[{"kind": "position", "stop_id": "b", "position": 0}]}'
            )
        )
        self.assert_load_raises(UnsupportedConstraintError)

    def test_an_override_disagreeing_with_the_selection_is_rejected(self) -> None:
        self.update_plan(
            order_overrides_json=(
                '{"version": 1, "constraints": '
                '[{"kind": "first_stop", "stop_id": "b", "position": null}]}'
            )
        )
        self.assert_load_raises(InvalidRoutePlanError, containing="two conflicting sources")

    # -- stops: duration, statuses, shapes ---------------------------------- #
    def test_a_negative_or_non_integer_service_duration_is_rejected(self) -> None:
        for value in (-60, 0, 12.5, "not-a-number"):
            with self.subTest(value=value):
                self.update_stop("b", service_duration_sec=value)
                self.assert_load_raises(InvalidRouteStopError, containing="service_duration")
                self.update_stop("b", service_duration_sec=600)

    def test_a_service_status_the_domain_rejects_is_rejected(self) -> None:
        # The approved DDL's CHECK refuses it first - and that is asserted, so the two layers are
        # documented rather than assumed.
        with self.assertRaises(sqlite3.IntegrityError):
            self.update_stop("b", service_status="maybe")

        self.ignore_check_constraints()
        self.update_stop("b", service_status="maybe")
        self.assert_load_raises(InvalidRouteStopError, containing="service_status")

    def test_a_geocode_status_the_domain_rejects_is_rejected(self) -> None:
        # The DDL's own CHECK refuses it first; only once a writer bypasses that check can the
        # domain's rule be reached - and then it is the domain that reports the failure.
        with self.assertRaises(sqlite3.IntegrityError):
            self.update_stop("b", geocode_status="guessed")

        self.ignore_check_constraints()
        self.update_stop("b", geocode_status="guessed")
        self.assert_load_raises(InvalidRouteStopError, containing="geocode_status")

    def test_resolved_geocoding_without_coordinates_is_rejected_by_the_domain(self) -> None:
        self.ignore_check_constraints()
        self.update_stop("d", geocode_status=GeocodeStatus.RESOLVED.value)
        self.assert_load_raises(InvalidRouteStopError, containing="coordinates")

    def test_a_malformed_fixed_window_is_rejected_by_the_domain(self) -> None:
        self.ignore_check_constraints()
        self.update_stop("a", service_window_start=None, service_window_end=None)
        self.assert_load_raises(InvalidServiceWindowError)

    def test_a_resolved_instant_in_a_wall_clock_column_is_rejected(self) -> None:
        for value in ("2026-09-11T01:00:00Z", "25:00:00", "8:00", "08:00"):
            with self.subTest(value=value):
                self.update_stop("a", service_window_start=value)
                self.assert_load_raises(StoredPlanError)
                self.update_stop("a", service_window_start="08:00:00")

    def test_a_negative_input_position_is_rejected(self) -> None:
        self.ignore_check_constraints()
        self.update_stop("b", input_position=-1)
        self.assert_load_raises(InvalidRouteStopError, containing="input_position")

    def test_an_enabled_flag_that_is_not_a_boolean_is_rejected(self) -> None:
        self.update_stop("b", enabled=2)
        self.assert_load_raises(StoredPlanError, containing="enabled")

    # -- plan columns ------------------------------------------------------- #
    def test_a_timestamp_without_the_z_suffix_is_rejected(self) -> None:
        for value in ("2026-09-11T01:00:00", "2026-09-11T01:00:00+00:00", "2026-09-11", 12345):
            with self.subTest(value=value):
                self.update_plan(departure_time_utc=value)
                self.assert_load_raises(StoredPlanError)

    def test_a_calendar_invalid_departure_timestamp_is_a_storage_error(self) -> None:
        """A shape-valid but impossible date is stored state, never an interpreter accident.

        ``2026-02-30`` and ``2026-09-31`` satisfy the storage shape (``_UTC_Z_RE``) while naming no
        instant, so a bare ``datetime.strptime`` raises its own ``ValueError`` ("day is out of range
        for month"). D38 acceptance item 5 requires a hand-edited row to fail loudly as a storage
        error that names the column and the value, and the failed read must leave the row exactly as
        it found it - an impossible date is refused, never repaired.
        """
        # The last value proves the guard is real calendar validation, not a special case for
        # "day 30/31": the month itself is impossible, and it reaches the same stored-state error.
        for value in ("2026-02-30T01:00:00Z", "2026-09-31T01:00:00Z", "2026-13-01T01:00:00Z"):
            with self.subTest(value=value):
                self.update_plan(departure_time_utc=value)
                row_before_failed_load = self.dump()

                with self.assertRaises(StoredPlanError) as context:
                    self.repository.get(self.plan.id)

                self.assertIs(
                    type(context.exception),
                    StoredPlanError,
                    msg=f"expected exactly StoredPlanError, got {type(context.exception).__name__}",
                )
                self.assertNotIsInstance(
                    context.exception,
                    ValueError,
                    msg="an impossible stored date must not surface as the interpreter's own error",
                )
                self.assertIn("departure_time_utc", str(context.exception))
                self.assertIn(value, str(context.exception))
                self.assertEqual(
                    self.dump(),
                    row_before_failed_load,
                    msg="a failed load must not rewrite or repair the stored row",
                )
                self.assertEqual(
                    self.scalar(
                        "SELECT departure_time_utc FROM route_plans WHERE id = ?",
                        str(self.plan.id),
                    ),
                    value,
                    msg="the refused value must still be the stored value",
                )

    def test_a_calendar_invalid_created_at_is_a_storage_error_too(self) -> None:
        """Every stored timestamp shares the same calendar check, not just ``departure_time_utc``.

        ``created_at_utc`` is validated whenever a save preserves it - for the plan row and for each
        surviving stop row (``_utc_z_text``) - so the identical impossible dates must be refused
        there, named by column, instead of being carried forward into the next write or reported by
        ``strptime``.
        """
        plan_created = self.scalar(
            "SELECT created_at_utc FROM route_plans WHERE id = ?", str(self.plan.id)
        )
        stop_created = self.scalar("SELECT created_at_utc FROM route_stops WHERE id = ?", "a")
        columns = (
            (
                "route_plans.created_at_utc",
                lambda value: self.update_plan(created_at_utc=value),
                lambda: self.update_plan(created_at_utc=plan_created),
            ),
            (
                "route_stops.created_at_utc",
                lambda value: self.update_stop("a", created_at_utc=value),
                lambda: self.update_stop("a", created_at_utc=stop_created),
            ),
        )
        for value in ("2026-02-30T01:00:00Z", "2026-09-31T01:00:00Z"):
            for column, edit, restore in columns:
                with self.subTest(column=column, value=value):
                    edit(value)
                    with self.assertRaises(StoredPlanError) as context:
                        self.repository.save(self.plan)

                    self.assertIs(
                        type(context.exception),
                        StoredPlanError,
                        msg=f"expected exactly StoredPlanError, got "
                        f"{type(context.exception).__name__}",
                    )
                    self.assertNotIsInstance(context.exception, ValueError)
                    self.assertIn(column, str(context.exception))
                    self.assertIn(value, str(context.exception))
                    restore()

        # The failed saves changed nothing: the untouched plan still round-trips.
        self.repository.save(self.plan)
        self.assertEqual(self.repository.get(self.plan.id), self.plan)

    def test_a_non_numeric_coordinate_is_rejected(self) -> None:
        self.update_plan(departure_latitude="north")
        self.assert_load_raises(StoredPlanError)

    def test_a_coordinate_outside_its_range_is_rejected_by_the_domain(self) -> None:
        self.update_plan(finish_latitude=91.5)
        self.assert_load_raises(ValidationError, containing="latitude")

    def test_a_stored_provenance_this_repository_cannot_accept_is_rejected(self) -> None:
        self.update_plan(data_provenance=OTHER_PROVENANCE.value)
        self.assert_load_raises(StoredPlanError, containing=OTHER_PROVENANCE.value)

    def test_a_repository_configured_for_the_stored_provenance_reads_it(self) -> None:
        self.update_plan(data_provenance=OTHER_PROVENANCE.value)
        other = self.make_repository(provenance=OTHER_PROVENANCE)
        self.assertEqual(other.get(self.plan.id), self.plan)

    def test_a_made_up_provenance_is_rejected(self) -> None:
        self.ignore_check_constraints()
        self.update_plan(data_provenance="MADE_UP")
        self.assert_load_raises(StoredPlanError, containing="MADE_UP")

    # -- the first-stop decision -------------------------------------------- #
    def test_manual_mode_with_an_accepted_recommendation_source_is_rejected(self) -> None:
        self.update_plan(
            first_stop_mode="manual", first_stop_selection_source="accepted_recommendation"
        )
        self.assert_load_raises(InvalidRoutePlanError, containing="manual_choice")

    def test_a_selection_that_is_not_a_stop_of_the_plan_is_rejected(self) -> None:
        other = build_plan(stop("other-stop", 55.90, 37.80), plan_id="other", input_positions=[0])
        self.repository.save(other)
        self.update_plan(first_stop_selected_stop_id="other-stop")
        self.assert_load_raises(InvalidRoutePlanError, containing="not a stop of")

    def test_a_selection_that_is_disabled_is_rejected(self) -> None:
        self.update_stop("a", enabled=0)
        self.assert_load_raises(InvalidRoutePlanError, containing="disabled")

    def test_a_selection_without_its_provenance_is_rejected(self) -> None:
        self.update_plan(first_stop_selection_source=None)
        self.assert_load_raises(InvalidRoutePlanError, containing="how the driver chose")

    def test_a_selection_that_is_not_pinned_is_rejected(self) -> None:
        self.update_plan(first_stop_pinned=0)
        self.assert_load_raises(InvalidRoutePlanError, containing="pinned")

    def test_provenance_without_a_selection_is_rejected(self) -> None:
        self.update_plan(first_stop_selected_stop_id=None, first_stop_selection_source="manual_choice")
        self.assert_load_raises(InvalidRoutePlanError, containing="nothing is selected")

    def test_a_pin_flag_that_is_not_a_boolean_is_rejected(self) -> None:
        self.update_plan(first_stop_pinned=2)
        self.assert_load_raises(StoredPlanError, containing="first_stop_pinned")

    def test_the_revoked_auto_mode_cannot_even_be_stored(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.update_plan(first_stop_mode="auto")


if __name__ == "__main__":
    unittest.main()
