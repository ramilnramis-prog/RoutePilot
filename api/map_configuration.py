"""The map configuration the web workspace reads over the API (Stage 4 U15; D15, spec section 34).

Why this module exists
======================

Decisions D15 and D39(c) require the demo map to be **Leaflet with an OSM-compatible tile
provider**, with the tile URL, the attribution and the max zoom coming from **approved
configuration** (the ``app_settings`` keys ``tile_url``, ``tile_attribution`` and
``tile_max_zoom``), visible attribution, and synthetic geometry that is never presented as road
routing. The tile provider therefore has to reach the browser through the API - it may not be
written into ``web/index.html`` or ``web/app.js``, because a hardcoded vendor URL in the UI is
exactly what "tile provider isolated in configuration" forbids.

The ``app_settings`` store owns no key's semantics and invents no default (schema section 6,
``api.services.SettingsService``). This module is the one documented place that decides what the
map configuration **is** when a key has never been stored, and it does so for the *transport*,
not for the store:

* :data:`MAP_SETTING_KEYS` - the five keys, in the order the UI reads them. Three are the approved
  tile keys of D15; ``map_library_url`` and ``map_library_css_url`` are the Leaflet asset locations,
  added here (as the brief allows) so the map library is configuration too instead of a URL frozen
  into the markup. They are ordinary ``app_settings`` keys: storing a value under either one
  overrides the default without a code change, and no new dependency is added - Leaflet is loaded
  by the browser from a URL, never vendored into this repository and never imported by Python.
* :data:`MAP_SETTING_DEFAULTS` - the sensible default of each key. Only three are real values; each
  is a documented, working, OSM-compatible default, and none of them is a RoutePilot business
  figure. ``tile_url`` points at the OpenStreetMap tile service, whose tile usage policy expects
  the visible ``tile_attribution`` default below, and ``tile_max_zoom`` is the depth that service
  actually serves (19). ``map_library_url``/``map_library_css_url`` point at the pinned Leaflet
  distribution the project documents (D15: "no new Leaflet assets may be vendored during this
  stage").
* :func:`map_configuration_payload` - the resolved configuration as JSON-ready data, carrying for
  every key both ``value`` and ``source`` (``configured`` = read from ``app_settings``, ``default``
  = the documented default above). The source travels with the value on purpose: the UI shows the
  driver which map configuration is actually in force instead of implying that a default was
  approved as stored configuration (D16).
* :data:`MAP_UNAVAILABLE_NOTE` - the sentence the UI shows when the tile map cannot be drawn. It
  is served by the API rather than written in the markup so the honesty text has one home.

What is deliberately **not** here
=================================

No map logic, no geometry, no routing and no metric: this module only names configuration and
resolves it. The straight-line synthetic geometry the UI draws is drawn by the browser from the
route payload the engine already produced, and it is labelled synthetic there; nothing in this
module computes a distance, a duration or an order.

Offline environment limitation (recorded, not hidden)
=====================================================

There is no working network on the machine this unit was implemented and tested on, so the tiles
themselves **cannot be verified here**: no test in this repository loads Leaflet, resolves
``tile.openstreetmap.org`` or checks a rendered map. What is verified is that the configuration is
read over the API (never hardcoded in ``web/``), that the UI states which configuration is in force,
and that a Leaflet or tile failure degrades to the honest notice of :data:`MAP_UNAVAILABLE_NOTE`
while the non-map workspace stays fully usable. Browser rendering is confirmed manually - see the
manual visual checklist in ``tests/web/test_web_workspace.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "MAP_LIBRARY_KEYS",
    "MAP_SETTING_DEFAULTS",
    "MAP_SETTING_KEYS",
    "MAP_TILE_KEYS",
    "MAP_UNAVAILABLE_NOTE",
    "MapConfiguration",
    "MapSetting",
    "configured_map_configuration",
    "default_map_configuration",
    "map_configuration_payload",
]

#: The approved tile keys of D15, in reading order. These come from ``app_settings``.
MAP_TILE_KEYS: tuple[str, ...] = ("tile_url", "tile_attribution", "tile_max_zoom")

#: The map-library keys of this unit: the Leaflet JavaScript and stylesheet locations. Configurable
#: for the same reason the tile provider is - a vendor URL belongs in configuration, not in the
#: markup - and defaulted because the demo has to work out of the box.
MAP_LIBRARY_KEYS: tuple[str, ...] = ("map_library_url", "map_library_css_url")

#: Every settings key the map configuration is assembled from, in the order the UI reads them.
MAP_SETTING_KEYS: tuple[str, ...] = MAP_TILE_KEYS + MAP_LIBRARY_KEYS

#: The documented default of each map setting. ``None`` means "this key has no default in this
#: build", which is the honest state of ``map_library_css_url``: the page renders without Leaflet's
#: stylesheet (markers and polylines still appear, the zoom control looks plainer), so a missing
#: stylesheet is a degraded look, not a broken map, and inventing a second stylesheet URL here
#: would add a vendor URL nobody asked for. Every non-``None`` value is a real, working,
#: OSM-compatible default, and no value is a RoutePilot business figure.
MAP_SETTING_DEFAULTS: dict[str, Any] = {
    # OSM-compatible tiles. Visible attribution is supplied by ``tile_attribution`` below and is
    # displayed whenever tiles are shown (spec section 34, D15).
    "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "tile_attribution": "&copy; OpenStreetMap contributors",
    # The depth the OpenStreetMap tile service actually serves.
    "tile_max_zoom": 19,
    # Pinned Leaflet distribution (BSD-2-Clause). Loaded by the browser, never vendored here.
    "map_library_url": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "map_library_css_url": None,
}

#: The honest sentence shown when the tile map cannot be drawn (Leaflet failed to load, tiles could
#: not be reached, or the configuration is unusable). Served by the API so the wording lives with
#: the other capability statements rather than in the markup.
MAP_UNAVAILABLE_NOTE = (
    "The tile map is unavailable (the Leaflet library could not be loaded, the tile service could "
    "not be reached, or tile_url is not a usable {z}/{x}/{y} URL). This is a map-only failure: the "
    "timeline, route summary, recommendation, selection state and rejected candidates below are "
    "computed and rendered by the API and stay fully usable."
)

#: ``source`` value: the value was read from ``app_settings`` (it is configured).
SOURCE_CONFIGURED = "configured"

#: ``source`` value: no value was stored, so the documented default above is in force.
SOURCE_DEFAULT = "default"


@dataclass(frozen=True)
class MapSetting:
    """One resolved map setting: the value in force and where that value came from."""

    key: str
    value: Any
    source: str

    @property
    def configured(self) -> bool:
        """Whether the value was read from ``app_settings`` rather than defaulted."""
        return self.source == SOURCE_CONFIGURED

    def as_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class MapConfiguration:
    """The whole map configuration in force, key by key, with each key's source."""

    settings: tuple[MapSetting, ...]

    def get(self, key: str) -> MapSetting | None:
        for setting in self.settings:
            if setting.key == key:
                return setting
        return None

    def value(self, key: str) -> Any:
        setting = self.get(key)
        return None if setting is None else setting.value

    @property
    def is_usable(self) -> bool:
        """Whether a tile layer can be created from this configuration at all.

        Usable means: a non-empty tile URL template carrying ``{z}``, ``{x}`` and ``{y}``, a
        non-empty attribution (visible attribution is a requirement, not a nicety), and a positive
        whole-number max zoom. Nothing here fetches anything - reachability is the browser's
        question, and a failure to reach the tiles is answered with the honest notice.
        """
        url = self.value("tile_url")
        attribution = self.value("tile_attribution")
        max_zoom = self.value("tile_max_zoom")
        return (
            isinstance(url, str)
            and all(token in url for token in ("{z}", "{x}", "{y}"))
            and isinstance(attribution, str)
            and bool(attribution.strip())
            and isinstance(max_zoom, int)
            and not isinstance(max_zoom, bool)
            and max_zoom > 0
        )

    def as_payload(self) -> dict[str, Any]:
        """The API form: every key with its value and source, plus the two convenience views.

        ``configuration`` is the UI's input (what to render and what to be honest about);
        ``values`` and ``defaulted_keys`` are conveniences so a client never has to walk the list
        to answer "what tile URL is in force" or "which keys are defaults".
        """
        return {
            "settings": [setting.as_payload() for setting in self.settings],
            "values": {setting.key: setting.value for setting in self.settings},
            "configured_keys": [
                setting.key for setting in self.settings if setting.configured
            ],
            "defaulted_keys": [
                setting.key for setting in self.settings if not setting.configured
            ],
            "usable": self.is_usable,
            "unavailable_note": MAP_UNAVAILABLE_NOTE,
        }


def _resolve(key: str, stored_values: dict[str, Any] | None) -> MapSetting:
    stored = None if stored_values is None else stored_values.get(key)
    if stored is not None:
        return MapSetting(key=key, value=stored, source=SOURCE_CONFIGURED)
    return MapSetting(key=key, value=MAP_SETTING_DEFAULTS.get(key), source=SOURCE_DEFAULT)


def default_map_configuration(stored_values: dict[str, Any] | None = None) -> MapConfiguration:
    """The map configuration in force, given the values **actually stored** in ``app_settings``.

    ``stored_values`` maps a key to its stored JSON value; a key that is absent (or stored as JSON
    ``null``, which the settings store cannot distinguish from absent - see
    ``SettingsService.get_setting``) resolves to the documented default with
    ``source="default"``. Nothing is written anywhere: this is a read-only resolution.
    """
    return MapConfiguration(
        settings=tuple(_resolve(key, stored_values) for key in MAP_SETTING_KEYS)
    )


def configured_map_configuration(
    reader: Callable[[str], Any],
) -> MapConfiguration:
    """Resolve the configuration by reading each key through ``reader``.

    ``reader`` is :meth:`api.services.SettingsService.effective_value`, which returns the stored
    value or ``None`` when the key holds none. Passing the reader in (rather than importing the
    service) keeps this module free of any dependency on the service layer, so the payload can be
    produced from a test double without a database.
    """
    return default_map_configuration({key: reader(key) for key in MAP_SETTING_KEYS})


def map_configuration_payload(configuration: MapConfiguration | None = None) -> dict[str, Any]:
    """The health payload's ``map`` block: the configuration in force (D15/D16, spec section 34)."""
    return (configuration or default_map_configuration()).as_payload()
