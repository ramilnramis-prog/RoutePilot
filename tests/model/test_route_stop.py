"""RouteStop model rules (spec section 17, D20)."""

from __future__ import annotations

import unittest
from datetime import time

from core.model.route_stop import GeocodeStatus, RouteStop, ServiceStatus
from core.model.service_window import ServiceWindow
from core.validation.errors import InvalidRouteStopError
from tests.support import stop


class RouteStopTests(unittest.TestCase):
    def test_resolved_address_requires_coordinates(self) -> None:
        with self.assertRaises(InvalidRouteStopError):
            RouteStop(
                id="s1",
                raw_address="Main street 1",
                service_window=ServiceWindow.unrestricted(),
                geocode_status=GeocodeStatus.RESOLVED,
            )

    def test_unresolved_states_must_not_carry_coordinates(self) -> None:
        for status in (GeocodeStatus.PENDING, GeocodeStatus.FAILED):
            with self.assertRaises(InvalidRouteStopError):
                RouteStop(
                    id="s1",
                    raw_address="Main street 1",
                    service_window=ServiceWindow.unrestricted(),
                    latitude=55.75,
                    longitude=37.62,
                    geocode_status=status,
                )

    def test_ambiguous_status_may_carry_a_tentative_point(self) -> None:
        ambiguous = RouteStop(
            id="s1",
            raw_address="Main street",
            service_window=ServiceWindow.unrestricted(),
            latitude=55.75,
            longitude=37.62,
            geocode_status=GeocodeStatus.AMBIGUOUS,
        )
        self.assertIsNotNone(ambiguous.location)
        self.assertIs(ambiguous.geocode_status, GeocodeStatus.AMBIGUOUS)

    def test_coordinates_must_be_paired(self) -> None:
        with self.assertRaises(InvalidRouteStopError):
            RouteStop(
                id="s1",
                raw_address="Main street 1",
                service_window=ServiceWindow.unrestricted(),
                latitude=55.75,
                geocode_status=GeocodeStatus.RESOLVED,
            )

    def test_out_of_range_coordinates_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            stop("s1", 155.0, 37.62)

    def test_fixed_window_requires_a_located_customer(self) -> None:
        with self.assertRaises(InvalidRouteStopError) as context:
            RouteStop(
                id="s1",
                raw_address="Main street 1",
                service_window=ServiceWindow.fixed(time(8, 0), time(18, 0)),
            )
        self.assertIn("fixed service window", str(context.exception))

    def test_disabled_stop_is_independent_of_statuses(self) -> None:
        disabled = stop(
            "s1",
            55.75,
            37.62,
            enabled=False,
            service_status=ServiceStatus.SKIPPED,
        )
        self.assertFalse(disabled.is_active)
        self.assertIs(disabled.service_status, ServiceStatus.SKIPPED)
        self.assertIs(disabled.geocode_status, GeocodeStatus.RESOLVED)
        self.assertFalse(disabled.enabled)

    def test_service_duration_must_be_positive(self) -> None:
        with self.assertRaises(InvalidRouteStopError):
            stop("s1", 55.75, 37.62, service_duration=0)
        with self.assertRaises(InvalidRouteStopError):
            stop("s1", 55.75, 37.62, service_duration=-60)

    def test_unknown_service_duration_is_allowed_and_not_invented(self) -> None:
        without_duration = stop("s1", 55.75, 37.62, service_duration=None)
        self.assertIsNone(without_duration.service_duration)

    def test_priority_must_not_be_negative(self) -> None:
        with self.assertRaises(InvalidRouteStopError):
            stop("s1", 55.75, 37.62, priority=-1)

    def test_empty_address_is_rejected(self) -> None:
        with self.assertRaises(InvalidRouteStopError):
            RouteStop(
                id="s1",
                raw_address="   ",
                service_window=ServiceWindow.unrestricted(),
                latitude=55.75,
                longitude=37.62,
                geocode_status=GeocodeStatus.RESOLVED,
            )

    def test_statuses_are_coerced_from_strings(self) -> None:
        coerced = RouteStop(
            id="s1",
            raw_address="Main street 1",
            service_window=ServiceWindow.unrestricted(),
            latitude=55.75,
            longitude=37.62,
            geocode_status="resolved",
            service_status="in_progress",
        )
        self.assertIs(coerced.geocode_status, GeocodeStatus.RESOLVED)
        self.assertIs(coerced.service_status, ServiceStatus.IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()
