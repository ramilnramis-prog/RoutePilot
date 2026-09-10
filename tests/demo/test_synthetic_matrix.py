"""Synthetic demo travel matrix (spec section 24).

The provider is deterministic and honest about what it is: straight-line synthetic data, no
traffic, no one-way information, no road geometry and no side of road.
"""

from __future__ import annotations

import unittest

from core.model.value_objects import DataProvenance, GeoPoint
from demo.dataset import HEADLINE_STOP_IDS, build_demo_plan
from demo.synthetic_matrix import (
    DEMO_MATRIX_DISCLAIMER,
    DEMO_SECONDS_PER_DEGREE,
    DemoSyntheticMatrix,
    demo_matrix,
)

T = 3600
M = 60


class DemoSyntheticMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = demo_matrix()
        self.plan = build_demo_plan()

    def test_provenance_is_demo_synthetic(self) -> None:
        self.assertIs(self.matrix.provenance, DataProvenance.DEMO_SYNTHETIC)

    def test_no_advanced_capabilities_are_claimed(self) -> None:
        capabilities = self.matrix.capabilities
        self.assertFalse(capabilities.side_of_road)
        self.assertFalse(capabilities.one_way)
        self.assertFalse(capabilities.traffic)
        self.assertFalse(capabilities.real_road_routing)
        self.assertEqual(capabilities.describe(), "no advanced capabilities")

    def test_disclaimer_never_presents_data_as_road_routing(self) -> None:
        self.assertIn("SYNTHETIC", DEMO_MATRIX_DISCLAIMER)
        self.assertIn("NOT road routing", DEMO_MATRIX_DISCLAIMER)
        self.assertEqual(self.matrix.describe(), DEMO_MATRIX_DISCLAIMER)

    def test_distance_to_the_same_point_is_zero(self) -> None:
        point = GeoPoint(55.75, 37.62)
        self.assertEqual(self.matrix.travel_time_seconds(point, point), 0)
        self.assertEqual(self.matrix.distance_meters(point, point), 0.0)

    def test_travel_time_is_symmetric(self) -> None:
        origin = self.plan.departure_point
        for stop in self.plan.stops:
            with self.subTest(stop=stop.id):
                assert stop.location is not None
                self.assertEqual(
                    self.matrix.travel_time_seconds(origin, stop.location),
                    self.matrix.travel_time_seconds(stop.location, origin),
                )

    def test_triangle_inequality_holds(self) -> None:
        a = self.plan.departure_point
        b = self.plan.stop_by_id(HEADLINE_STOP_IDS["mid"]).location
        c = self.plan.stop_by_id(HEADLINE_STOP_IDS["farthest"]).location
        assert b is not None and c is not None
        direct = self.matrix.travel_time_seconds(a, c)
        via_b = self.matrix.travel_time_seconds(a, b) + self.matrix.travel_time_seconds(b, c)
        self.assertLessEqual(direct, via_b)

    def test_headline_travel_times_are_exact(self) -> None:
        origin = self.plan.departure_point
        expected = {
            HEADLINE_STOP_IDS["nearest"]: 20 * M,
            HEADLINE_STOP_IDS["near_second"]: 45 * M,
            HEADLINE_STOP_IDS["far_before_opening"]: 3 * T + 55 * M,
            HEADLINE_STOP_IDS["farthest"]: 5 * T,
            HEADLINE_STOP_IDS["on_opening"]: 4 * T,
            HEADLINE_STOP_IDS["edge_window"]: 6 * T + 5 * M,
        }
        for stop_id, seconds in expected.items():
            with self.subTest(stop=stop_id):
                location = self.plan.stop_by_id(stop_id).location
                assert location is not None
                self.assertEqual(
                    self.matrix.travel_time_seconds(origin, location),
                    seconds,
                )

    def test_distance_is_positive_and_scaled_by_the_same_delta(self) -> None:
        origin = self.plan.departure_point
        location = self.plan.stop_by_id(HEADLINE_STOP_IDS["farthest"]).location
        assert location is not None
        distance = self.matrix.distance_meters(origin, location)
        self.assertGreater(distance, 0)
        expected = (
            self.matrix.travel_time_seconds(origin, location)
            / DEMO_SECONDS_PER_DEGREE
            * 111_000.0
        )
        self.assertAlmostEqual(distance, expected)

    def test_instances_agree(self) -> None:
        other = DemoSyntheticMatrix()
        origin = self.plan.departure_point
        location = self.plan.stop_by_id(HEADLINE_STOP_IDS["mid"]).location
        assert location is not None
        self.assertEqual(
            self.matrix.travel_time_seconds(origin, location),
            other.travel_time_seconds(origin, location),
        )


if __name__ == "__main__":
    unittest.main()
