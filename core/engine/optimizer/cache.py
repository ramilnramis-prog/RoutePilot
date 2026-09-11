"""Deterministic in-memory leg cache (Stage 2 unit U2; v2 sections 13, 20, 21).

The optimizer asks the travel matrix the same leg question thousands of times: a greedy seed and
a local improvement both re-price the same origins and destinations while they try alternatives.
:class:`LegCache` wraps any :class:`~core.engine.providers.TravelMatrix` and memoizes the two
questions the engine asks - ``travel_time_seconds`` and ``distance_meters`` - per origin and
destination.

Three properties matter and are deliberately visible:

* it is **deterministic**: the same sequence of questions always produces the same answers,
  the same hit/miss counts and the same entry count; nothing here depends on wall-clock time,
  on insertion order of a hash table or on randomness;
* it is **transparent**: a cached value is exactly the value the wrapped matrix returned, so
  wrapping a matrix can never change a route;
* it is **measured**: :class:`CacheStats` reports hits, misses and entries so the demo, the
  benchmark and a test can show that the reuse is real instead of asserting it in prose
  (v2 section 20: measure the bottleneck before approximating anything).

Legs are keyed on the **exact** coordinates of the two points, and the wrapped matrix is asked with
those same original points: the key and the question can never disagree, so two distinct but nearby
points never share a slot. Rounding the key (while forwarding the raw point on a miss) would let the
second point silently receive the first one's answer, which would change a result - the one thing a
cache must never do.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.engine.providers import TravelMatrix
from core.model.value_objects import DataProvenance, GeoPoint

__all__ = ["CacheStats", "LegCache", "leg_key"]


def leg_key(origin: GeoPoint, destination: GeoPoint) -> tuple[GeoPoint, GeoPoint]:
    """The deterministic cache key of one directional leg (origin -> destination).

    The key holds the exact ``GeoPoint`` values that were asked about - no rounding, no
    quantization - so keying can never map two different points onto one answer.
    """
    if not isinstance(origin, GeoPoint) or not isinstance(destination, GeoPoint):
        raise TypeError("a leg key needs two GeoPoint values")
    return (origin, destination)


@dataclass(frozen=True)
class CacheStats:
    """How much work the cache saved: hits, misses and memoized legs."""

    hits: int
    misses: int
    entries: int

    def __post_init__(self) -> None:
        for field_name in ("hits", "misses", "entries"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative whole number")

    @property
    def lookups(self) -> int:
        """Total leg questions asked (each answer costs either a hit or a miss)."""
        return self.hits + self.misses

    def describe(self) -> str:
        """One-line report for the demo and the benchmark."""
        return (
            f"leg cache: {self.hits} hits, {self.misses} misses, {self.entries} entries "
            f"({self.lookups} lookups)"
        )


class LegCache:
    """Memoizing wrapper around a :class:`~core.engine.providers.TravelMatrix`.

    The wrapped matrix stays the single source of truth: this class never invents a travel time
    and never interpolates. It only remembers what the matrix said.
    """

    __slots__ = ("_matrix", "_travel", "_distance", "_hits", "_misses")

    def __init__(self, matrix: TravelMatrix) -> None:
        self._matrix = matrix
        self._travel: dict[tuple[GeoPoint, GeoPoint], int] = {}
        self._distance: dict[tuple[GeoPoint, GeoPoint], float] = {}
        self._hits = 0
        self._misses = 0

    # ---- the wrapped matrix -------------------------------------------- #
    @property
    def matrix(self) -> TravelMatrix:
        return self._matrix

    @property
    def provenance(self) -> DataProvenance:
        """Provenance travels with the wrapper, so a synthetic leg is never relabelled."""
        return self._matrix.provenance

    @property
    def capabilities(self):
        return self._matrix.capabilities

    # ---- memoized questions -------------------------------------------- #
    def travel_time_seconds(self, origin: GeoPoint, destination: GeoPoint) -> int:
        """Travel time in whole seconds, from the cache when it is already known."""
        key = leg_key(origin, destination)
        known = self._travel.get(key)
        if known is not None:
            self._hits += 1
            return known
        self._misses += 1
        value = self._matrix.travel_time_seconds(origin, destination)
        self._travel[key] = value
        return value

    def distance_meters(self, origin: GeoPoint, destination: GeoPoint) -> float:
        """Travel distance in metres, from the cache when it is already known."""
        key = leg_key(origin, destination)
        known = self._distance.get(key)
        if known is not None:
            self._hits += 1
            return known
        self._misses += 1
        value = float(self._matrix.distance_meters(origin, destination))
        self._distance[key] = value
        return value

    # ---- bookkeeping ---------------------------------------------------- #
    @property
    def stats(self) -> CacheStats:
        """Hits, misses and entries for the questions asked so far."""
        return CacheStats(hits=self._hits, misses=self._misses, entries=self.entry_count)

    @property
    def entry_count(self) -> int:
        """Distinct legs memoized, travel and distance counted per leg (not per question)."""
        return len(set(self._travel) | set(self._distance))

    @property
    def distance_entry_count(self) -> int:
        return len(self._distance)

    def clear(self) -> None:
        """Forget every memoized leg and reset the counters (deterministic, explicit)."""
        self._travel.clear()
        self._distance.clear()
        self._hits = 0
        self._misses = 0

    def reset_stats(self) -> None:
        """Keep every memoized leg but restart the hit and miss counters.

        This is how a benchmark measures *one phase* of a warm run (v2 section 20 step 1: measure
        the bottleneck) without throwing away the warmed legs: the entries stay, so the phase still
        gets the reuse the real caller gets, while its hits and misses describe that phase alone.
        Deterministic: the counters are whole numbers that only ever move by a recorded question.
        """
        self._hits = 0
        self._misses = 0

    def describe(self) -> str:
        return self.stats.describe()
