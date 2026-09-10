"""Deterministic synthetic travel matrix for the demo (spec section 24).

A straight-line chessboard metric where one coordinate degree equals one hour of driving. It is
deterministic, symmetric and satisfies the triangle inequality, and it carries
``DataProvenance.DEMO_SYNTHETIC`` plus empty provider capabilities, so no caller can mistake it
for road routing, traffic or side-of-road information.

This exists so the product can be demonstrated and tested without a paid map API.
"""

from __future__ import annotations

from core.engine.providers import ProviderCapabilities
from core.model.value_objects import DataProvenance, GeoPoint

__all__ = [
    "DEMO_DISTANCE_METERS_PER_DEGREE",
    "DEMO_MATRIX_DISCLAIMER",
    "DEMO_PROVENANCE",
    "DEMO_SECONDS_PER_DEGREE",
    "DemoSyntheticMatrix",
    "demo_matrix",
]

#: Provenance carried by every value this provider produces.
DEMO_PROVENANCE = DataProvenance.DEMO_SYNTHETIC

#: Synthetic scale: one degree of coordinate delta equals one hour of driving.
DEMO_SECONDS_PER_DEGREE = 3600

#: Synthetic scale for distance, for the same degree delta.
DEMO_DISTANCE_METERS_PER_DEGREE = 111_000.0

DEMO_MATRIX_DISCLAIMER = (
    "SYNTHETIC demo travel matrix: straight-line chessboard distance with 1 coordinate degree "
    "= 1 hour of driving. This is NOT road routing, not traffic and not a real road network."
)


class DemoSyntheticMatrix:
    """Straight-line synthetic travel times and distances (demo only)."""

    provenance = DEMO_PROVENANCE
    #: No traffic, no one-way information, no road geometry, no side of road (D16).
    capabilities = ProviderCapabilities()

    @staticmethod
    def _delta(origin: GeoPoint, destination: GeoPoint) -> float:
        return max(
            abs(destination.latitude - origin.latitude),
            abs(destination.longitude - origin.longitude),
        )

    def travel_time_seconds(self, origin: GeoPoint, destination: GeoPoint) -> int:
        return int(round(self._delta(origin, destination) * DEMO_SECONDS_PER_DEGREE))

    def distance_meters(self, origin: GeoPoint, destination: GeoPoint) -> float:
        return self._delta(origin, destination) * DEMO_DISTANCE_METERS_PER_DEGREE

    def describe(self) -> str:
        return DEMO_MATRIX_DISCLAIMER


def demo_matrix() -> DemoSyntheticMatrix:
    """A fresh deterministic demo matrix."""
    return DemoSyntheticMatrix()
