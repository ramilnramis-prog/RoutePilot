"""Deterministic ~100-stop benchmark fixture (Stage 2 unit U3; v2 sections 19, 20, 21, D18).

**DEMO / SYNTHETIC DATA - A BENCHMARK FIXTURE, NOT THE PRODUCT DEMO DATASET.**

This is not the demo plan of :mod:`demo.dataset` and it is not a customer scenario. It exists to
answer one engineering question of v2 section 20: *is exhaustive complete-route optimization over
every first-stop candidate affordable at approximately 100 service stops?* Everything here is
invented and deterministic: fixed date, fixed time zone, fixed departure time, synthetic
coordinates, synthetic windows.

The plan is built to be representative of the shape an ENTERPRISE plan has at that scale
(D18: dozens -> ~100 -> 100+ stops):

* mixed fixed service windows (wide and narrow), plus ``unrestricted`` and ``unknown`` hours, so
  the optimizer sees real waiting and real hard-window risk (D28/D29);
* varied service durations, including stops that fall back to the plan's default duration;
* varied priorities, including stops with none;
* a few disabled stops, which optimization must exclude without renumbering the rest (D20/D33);
* every enabled stop geocoded with deterministic synthetic coordinates, never real addresses.

Determinism is the whole point and is tested: coordinates come from a fixed integer sequence, not
from ``random``, so **the same arguments always produce the same plan, the same input order and
the same** ``inputs_fingerprint()`` - across calls, across processes and across hash seeds. The
travel times derived from these coordinates by :mod:`demo.synthetic_matrix` are straight-line
synthetic figures, never road routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from core.model.cost_policy import RouteCostPolicy, demo_provisional_policy
from core.model.first_stop import FirstStopIntent
from core.model.ids import PlanId
from core.model.route_plan import RoutePlan
from core.model.route_stop import GeocodeStatus, RouteStop
from core.model.service_window import (
    DEFAULT_WINDOW_END_POLICY,
    ServiceWindow,
    WindowEndPolicy,
)
from core.model.value_objects import GeoPoint, PlaceRef

__all__ = [
    "SCALE_DEFAULT_SERVICE_DURATION",
    "SCALE_DEFAULT_STOP_COUNT",
    "SCALE_DEPARTURE_TIME",
    "SCALE_PLAN_ID",
    "SCALE_SERVICE_DATE",
    "SCALE_TIMEZONE",
    "SCALE_WARNING",
    "FINISH_POINT",
    "WAREHOUSE_POINT",
    "build_scale_plan",
    "scale_warning_text",
]

SCALE_TIMEZONE = "Europe/Moscow"
SCALE_SERVICE_DATE = date(2026, 9, 11)

#: 04:00 Moscow on the scale service date, stored the way the domain stores time: UTC.
SCALE_DEPARTURE_TIME = datetime(2026, 9, 11, 1, 0, tzinfo=timezone.utc)

SCALE_PLAN_ID = "scale-benchmark-01"
SCALE_DEFAULT_STOP_COUNT = 100
SCALE_DEFAULT_SERVICE_DURATION = 900

SCALE_WARNING = (
    "DEMO / SYNTHETIC BENCHMARK FIXTURE - invented stops, synthetic coordinates, "
    "not real addresses, not real opening hours, not real routing"
)

#: START and FINISH of the benchmark plan. The warehouse is the fixture's own point, chosen so
#: that the ~100 stops lie between 2 and ~50 minutes of synthetic travel from it.
WAREHOUSE_POINT = GeoPoint(55.75, 37.62)
FINISH_POINT = GeoPoint(55.70, 37.55)

WAREHOUSE_LABEL = "Scale benchmark warehouse (synthetic)"
FINISH_LABEL = "Scale benchmark depot (synthetic, fixed)"

#: The synthetic service area, as an offset in coordinate degrees from the warehouse. One degree
#: is one hour of synthetic travel (:mod:`demo.synthetic_matrix`), so these are 2..45 minute legs.
_LATITUDE_SPAN = 0.44
_LONGITUDE_SPAN = 0.56
_LATITUDE_ORIGIN = 0.10
_LONGITUDE_ORIGIN = 0.08

#: Moduli of the deterministic coordinate sequence. Coprime, so the two sequences do not march in
#: step and the stops spread over the area instead of falling on one diagonal.
_LATITUDE_MULTIPLIER = 7919
_LONGITUDE_MULTIPLIER = 6271
_SEQUENCE_MODULUS = 65521

# Shared immutable window definitions.
_W_0800_1800 = ServiceWindow.fixed(time(8, 0), time(18, 0))
_W_0800_1600 = ServiceWindow.fixed(time(8, 0), time(16, 0))
_W_0830_1700 = ServiceWindow.fixed(time(8, 30), time(17, 0))
_W_0900_1800 = ServiceWindow.fixed(time(9, 0), time(18, 0))
_W_1000_1400 = ServiceWindow.fixed(time(10, 0), time(14, 0))
_W_1330_1430 = ServiceWindow.fixed(time(13, 30), time(14, 30))
_UNRESTRICTED = ServiceWindow.unrestricted()
_UNKNOWN = ServiceWindow.unknown()

#: ``(window, service_duration in seconds or None for the plan default, priority or None)``.
#: Cycled by stop index so every combination the fixture needs is guaranteed to occur.
_ROTATION: tuple[tuple[ServiceWindow, int | None, int | None], ...] = (
    (_W_0800_1800, 600, None),
    (_W_0800_1600, 900, 2),
    (_UNRESTRICTED, 900, None),
    (_W_0830_1700, 1200, 1),
    (_W_0900_1800, 600, None),
    (_W_1330_1430, 1200, 3),
    (_UNKNOWN, 900, None),
    (_W_1000_1400, 1800, None),
    (_W_0800_1800, None, 1),
    (_W_0900_1800, 2400, 2),
)

#: Every ``DISABLED_EVERY``-th stop is disabled, so the plan has a few excluded stops at any scale.
_DISABLED_EVERY = 27


def _sequence_value(index: int, multiplier: int) -> int:
    """The ``index``-th value of a fixed integer sequence - the fixture's only "randomness".

    A Lehmer generator over a fixed modulus with a fixed multiplier: no ``random`` module, no
    wall-clock, no seed from the environment, so the same index always gives the same value in
    every process and under every hash seed.
    """
    return (index * multiplier) % _SEQUENCE_MODULUS


def _point_for_index(index: int) -> GeoPoint:
    """Synthetic coordinates of one stop: deterministic, unique and spread over the area."""
    latitude_ratio = _sequence_value(index, _LATITUDE_MULTIPLIER) / _SEQUENCE_MODULUS
    longitude_ratio = _sequence_value(index, _LONGITUDE_MULTIPLIER) / _SEQUENCE_MODULUS
    return GeoPoint(
        WAREHOUSE_POINT.latitude + _LATITUDE_ORIGIN + latitude_ratio * _LATITUDE_SPAN,
        WAREHOUSE_POINT.longitude + _LONGITUDE_ORIGIN + longitude_ratio * _LONGITUDE_SPAN,
    )


def _is_disabled(index: int) -> bool:
    """A small, deterministic share of disabled stops (never the first one)."""
    return index > 0 and index % _DISABLED_EVERY == 0


@dataclass(frozen=True)
class ScaleStopSpec:
    """One generated stop, exposed so a test can assert the fixture's shape without guessing."""

    stop_id: str
    label: str
    point: GeoPoint
    window: ServiceWindow
    service_duration: int | None
    priority: int | None
    enabled: bool
    input_position: int


def build_scale_specs(stop_count: int = SCALE_DEFAULT_STOP_COUNT) -> tuple[ScaleStopSpec, ...]:
    """The deterministic stop list of the fixture, in input order (v2 section 30, D33)."""
    if isinstance(stop_count, bool) or not isinstance(stop_count, int) or stop_count < 2:
        raise ValueError(
            "the scale benchmark needs a whole stop count of at least 2 (START plus FINISH "
            f"are not service stops), got {stop_count!r}"
        )
    specs: list[ScaleStopSpec] = []
    for index in range(stop_count):
        window, duration, priority = _ROTATION[index % len(_ROTATION)]
        specs.append(
            ScaleStopSpec(
                stop_id=f"K{index:03d}",
                label=f"Synthetic benchmark stop {index:03d}",
                point=_point_for_index(index),
                window=window,
                service_duration=duration,
                priority=priority,
                enabled=not _is_disabled(index),
                input_position=index,
            )
        )
    return tuple(specs)


def _build_stop(spec: ScaleStopSpec) -> RouteStop:
    return RouteStop(
        id=spec.stop_id,
        raw_address=f"{spec.label}, synthetic benchmark district",
        normalized_address=f"{spec.label}, synthetic benchmark area, invented coordinates",
        latitude=spec.point.latitude,
        longitude=spec.point.longitude,
        geocode_status=GeocodeStatus.RESOLVED,
        service_window=spec.window,
        service_duration=spec.service_duration,
        priority=spec.priority,
        enabled=spec.enabled,
        input_position=spec.input_position,
    )


def build_scale_plan(
    stop_count: int = SCALE_DEFAULT_STOP_COUNT,
    *,
    plan_id: str = SCALE_PLAN_ID,
    departure_time: datetime | None = None,
    timezone_name: str = SCALE_TIMEZONE,
    window_end_policy: WindowEndPolicy = DEFAULT_WINDOW_END_POLICY,
    cost_policy: RouteCostPolicy | None = None,
    default_service_duration: int | None = SCALE_DEFAULT_SERVICE_DURATION,
) -> RoutePlan:
    """Build the deterministic synthetic benchmark plan.

    Deterministic by construction: every argument has a fixed default, the coordinates come from a
    fixed integer sequence and the stops are built in input order, so identical arguments always
    produce an identical plan and an identical :meth:`RoutePlan.inputs_fingerprint`.

    The plan is in RECOMMEND mode with nothing selected, exactly like the demo plan: the engine
    recommends, the driver decides (I4/D32). A caller that wants to optimize has to make an
    explicit first-stop choice, which is what :mod:`tools.benchmark_optimizer` does.
    """
    return RoutePlan(
        id=PlanId(plan_id),
        timezone=timezone_name,
        departure=PlaceRef(WAREHOUSE_LABEL, WAREHOUSE_POINT),
        departure_time=departure_time if departure_time is not None else SCALE_DEPARTURE_TIME,
        finish=PlaceRef(FINISH_LABEL, FINISH_POINT),
        stops=tuple(_build_stop(spec) for spec in build_scale_specs(stop_count)),
        cost_policy=cost_policy if cost_policy is not None else demo_provisional_policy(),
        window_end_policy=window_end_policy,
        default_service_duration=default_service_duration,
        first_service_stop=FirstStopIntent.recommend(),
    )


def scale_warning_text() -> str:
    """One-line provenance warning for reports and benchmarks."""
    return SCALE_WARNING
