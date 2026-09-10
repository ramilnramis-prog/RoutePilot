"""Provider interfaces (decisions D15 and D16, spec sections 11, 13, 15).

Every external capability enters the domain through one of these Protocols, so no vendor
(Google Maps, Yandex, OSM, a tile server) is ever referenced by product logic. Capabilities are
declared explicitly rather than assumed: a provider that cannot report the side of the road says
so, and the cost policy refuses to score side-of-road components in that case.

Interfaces only - Stage 0 implements no provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from core.model.route_stop import GeocodeStatus
from core.model.value_objects import DataProvenance, GeoPoint

__all__ = [
    "GeocodeResult",
    "GeocodingProvider",
    "MapTileConfig",
    "ProviderCapabilities",
    "RoutingProvider",
    "TravelMatrix",
    "TravelTimeProvider",
]


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can actually do (D16).

    Defaults are all ``False``: an unimplemented or unknown capability is never assumed to work.
    """

    #: Can report which side of the road a stop is on (requires road geometry + direction).
    side_of_road: bool = False
    #: Can report live or historical traffic.
    traffic: bool = False
    #: Understands one-way restrictions.
    one_way: bool = False
    #: Can return a road path, not just a straight line.
    real_road_routing: bool = False
    #: Can return turn-by-turn instructions (hand-off to an external navigator).
    turn_by_turn: bool = False

    def describe(self) -> str:
        enabled = [
            name
            for name in ("side_of_road", "traffic", "one_way", "real_road_routing", "turn_by_turn")
            if getattr(self, name)
        ]
        return ", ".join(enabled) if enabled else "no advanced capabilities"


class TravelTimeProvider(Protocol):
    """Source of travel time between two coordinates."""

    #: Whether this data is real routing or synthetic demo data (spec section 24).
    provenance: DataProvenance
    capabilities: ProviderCapabilities

    def travel_time_seconds(self, origin: GeoPoint, destination: GeoPoint) -> int:
        """Travel time in whole seconds. Must be deterministic for identical inputs."""
        ...


class TravelMatrix(TravelTimeProvider, Protocol):
    """Travel time **and** distance source (spec section 13)."""

    def distance_meters(self, origin: GeoPoint, destination: GeoPoint) -> float:
        """Travel distance in metres."""
        ...


class RoutingProvider(Protocol):
    """Road path provider, requested per leg and in chunks (D18)."""

    provenance: DataProvenance
    capabilities: ProviderCapabilities

    def route_geometry(self, origin: GeoPoint, destination: GeoPoint) -> Sequence[GeoPoint]:
        """Road geometry of a single leg."""
        ...


@dataclass(frozen=True)
class GeocodeResult:
    """Outcome of one geocoding attempt (D20)."""

    status: GeocodeStatus
    normalized_address: str | None = None
    point: GeoPoint | None = None
    provider: str | None = None
    provider_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, GeocodeStatus):
            object.__setattr__(self, "status", GeocodeStatus(self.status))
        if self.status is GeocodeStatus.RESOLVED and self.point is None:
            raise ValueError("a resolved geocode result must carry a point")
        if self.status is GeocodeStatus.AMBIGUOUS and self.normalized_address is None:
            raise ValueError(
                "an ambiguous geocode result must carry what was ambiguous so a human can "
                "correct it (spec section 16)"
            )


class GeocodingProvider(Protocol):
    """Address to coordinates. Ambiguity is reported, never guessed (spec section 16)."""

    name: str
    capabilities: ProviderCapabilities

    def geocode(self, raw_address: str) -> GeocodeResult:
        ...


@dataclass(frozen=True)
class MapTileConfig:
    """Map tile configuration, isolated from the domain (D15).

    The attribution must be non-empty: the demo map uses OpenStreetMap-compatible tiles and the
    attribution has to be visible in the UI.
    """

    url_template: str
    attribution: str
    max_zoom: int = 19
    min_zoom: int = 0

    def __post_init__(self) -> None:
        for placeholder in ("{z}", "{x}", "{y}"):
            if placeholder not in self.url_template:
                raise ValueError(
                    f"tile URL template must contain {placeholder}: {self.url_template!r}"
                )
        if not isinstance(self.attribution, str) or not self.attribution.strip():
            raise ValueError(
                "tile attribution must be non-empty and visible in the UI (spec section 25)"
            )
        if not 0 <= self.min_zoom <= self.max_zoom:
            raise ValueError(
                f"invalid zoom range {self.min_zoom}..{self.max_zoom}"
            )

    def describe_attribution(self) -> str:
        return self.attribution
