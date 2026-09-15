/*
  RoutePilot demo workspace - map controller (Stage 4 U15; spec section 34, D15/D39(c)).
  Map presentation hotfix: the fabricated line between stops was removed and the route order is now
  shown through numbered stop markers.

  What this file does
  ===================

  Draws the departure (START), the finish (FINISH) and the service stops on a Leaflet map with an
  OSM-compatible tile layer. NO LINE, arc or any other geometry is drawn between the stops: the
  route order is presented as a 1-based number badge on each stop marker, and each stop marker's
  popup states the stop's label, its `position N of M` and what that stop is (recommended first
  stop, selected by driver, or both).

  With no committed route in hand, the plan's enabled stops are drawn as plain (unnumbered) markers
  instead of nothing, so the relevant points stay visible. A stop the API recommends or the driver
  has selected is marked as such even before a route exists.

  What this file must NEVER do
  ============================

  * It computes no route and no metric. The order it draws is `order`, exactly as
    `GET /api/plans/{id}/route` returned it; the coordinates are the stop/plan coordinates exactly as
    the API returned them. The number badge is the index inside that payload array (1-based) and
    nothing else - no distance, duration, saving, rank, feasibility or fingerprint is derived here
    (D39(f)).
  * It draws no line between stops: real road routing is not implemented, and no fabricated geometry
    stands in for it. Stop locations, their route order and the recommendation/selection state are
    the whole of what is shown.
  * It does not decide the tile provider. `tile_url`, `tile_attribution`, `tile_max_zoom` and the
    Leaflet library URL all arrive from `GET /api/health` -> `map.configuration.values`, which
    resolves the approved `app_settings` keys with their documented defaults
    (`api/map_configuration.py`). No vendor URL is written into this file, and no map asset is
    vendored into this repository.
  * It never presents anything here as road routing: the disclosure text below says so next to the
    map and in the collapsed technical details.

  Honest degradation (D39(c))
  ===========================

  If the Leaflet library cannot be loaded (offline, blocked CDN, a bad configured
  `map_library_url`), or the tile service cannot be reached, or the tile configuration is unusable,
  this file reports it through the caller's notice callback and leaves the workspace alone: the
  timeline, route summary, recommendation, selection state and rejected candidates are rendered from
  API payloads by `app.js` and stay fully usable without a map.

  OFFLINE ENVIRONMENT LIMITATION (recorded, not hidden)
  ====================================================

  This unit was implemented and tested on a machine with no working network, so the tile layer
  cannot be visually verified here and no test in this repository loads Leaflet or fetches a tile.
  What is verified is that the configuration is read over the API (never hardcoded), that the notice
  is shown when the library or the tiles fail, and that the rest of the workspace is unaffected.
  Browser rendering is confirmed manually - see the manual visual checklist in
  `tests/web/test_web_workspace.py`.

  MANUAL VISUAL CHECKLIST (to confirm in a real browser once a network is available)
  =================================================================================

  1. The map draws the START, the FINISH and the stops with NO line at all between them - not even a
     dashed one. Only marker positions and their number badges carry the order.
  2. Each stop marker shows its 1-based position from the API's `order` array, and its popup reads
     `position N of M`. Before any route is read the markers are unnumbered (plain enabled stops).
  3. The recommended first stop and the driver's own selected first stop are styled differently and
     labelled differently; when they are the same stop the marker says the driver accepted the
     recommendation, and never implies a recommendation applied itself.
  4. START, FINISH and every drawn stop stay inside the viewport (fitBounds over these points only).
  5. With tiles unreachable, the markers and the notice still render and the rest of the workspace
     stays usable.
*/

(function () {
  "use strict";

  //: Zoom used when the drawn features fit in no better way (Leaflet picks a tighter one itself).
  var FALLBACK_ZOOM = 6;

  //: How many tile requests may fail before the layer is called unreachable. One bad tile in the
  //: middle of a working map must not be reported as "the map is unavailable".
  var TILE_FAILURE_THRESHOLD = 4;

  //: The honest disclosure this module carries for the reader. It is repeated verbatim in
  //: `web/index.html` (next to the map and in the collapsed technical details), so the page and the
  //: module that draws the map can never disagree about what is shown. No line is drawn between
  //: stops, and real road routing is not implemented.
  var NO_ROUTE_GEOMETRY_DISCLOSURE =
    "Real road routing is not implemented: no line is drawn between the stops. Only the stop " +
    "locations, their route order and the recommended or driver-selected first stop are shown.";

  var state = {
    map: null,
    tileLayer: null,
    layer: null,
    tilesLoaded: 0,
    tilesFailed: 0,
    tilesReported: false,
    notice: function () {}
  };

  /** Plain text, never markup: every string here comes from an API payload. */
  function text(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value);
  }

  function setNotice(message, tone) {
    state.notice(text(message), tone || "warning");
  }

  /**
   * Point the `#map-notice` element at a sink in app.js. The notice is how a map failure stays
   * honest, so it must exist even when Leaflet never loads.
   */
  function setNoticeSink(sink) {
    state.notice = typeof sink === "function" ? sink : function () {};
  }

  /**
   * Create the Leaflet map once the library is available.
   *
   * Returns `null` and reports through the notice sink when Leaflet is not available, so the caller
   * can keep the workspace usable. The returned map carries no data yet: `drawRoute` adds it.
   */
  function mountMap(configuration, elementId) {
    if (!window.L || !window.L.map) {
      setNotice(
        "The map library (Leaflet) could not be loaded, so no tile map is shown. The configured " +
          "library URL is reported by GET /api/health; the route timeline, summary, recommendation " +
          "and selection state below are unaffected and stay usable.",
        "warning"
      );
      return null;
    }
    if (!configuration || !configuration.usable) {
      setNotice(text(configuration && configuration.unavailable_note), "warning");
      return null;
    }

    var container = document.getElementById(elementId);
    if (!container) {
      return null;
    }
    if (state.map) {
      state.map.remove();
      state.map = null;
    }

    var map = window.L.map(container, { zoomControl: true, attributionControl: true });
    map.setView([0, 0], FALLBACK_ZOOM);

    // The tile URL, the attribution and the max zoom are the API's values, untouched. The
    // attribution stays visible whenever tiles are shown (spec section 34, D15).
    var values = configuration.values || {};
    var tileLayer = window.L.tileLayer(text(values.tile_url), {
      attribution: text(values.tile_attribution),
      maxZoom: values.tile_max_zoom,
      noWrap: false
    });
    tileLayer.on("tileload", function () {
      state.tilesLoaded += 1;
    });
    tileLayer.on("tileerror", function () {
      state.tilesFailed += 1;
      if (!state.tilesReported && state.tilesFailed >= TILE_FAILURE_THRESHOLD) {
        state.tilesReported = true;
        setNotice(
          "The tile service could not be reached, so the map shows no map imagery. The stop " +
            "markers and their route-order numbers are still drawn; the timeline, summary, " +
            "recommendation and selection state below are unaffected.",
          "warning"
        );
      }
    });
    tileLayer.addTo(map);

    state.map = map;
    state.tileLayer = tileLayer;
    state.layer = window.L.layerGroup().addTo(map);
    state.tilesLoaded = 0;
    state.tilesFailed = 0;
    state.tilesReported = false;
    return map;
  }

  function escapeHtml(value) {
    return text(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /**
   * How this stop is presented, in the words the page uses elsewhere: the API's recommendation is
   * advice, the driver's own selection is a decision, and a stop that is both is the driver having
   * accepted the recommendation - never a recommendation that applied itself (D4/D32).
   */
  function stopRoleText(isRecommended, isSelected) {
    if (isRecommended && isSelected) {
      return "the driver's selection, which accepted the RoutePilot recommendation";
    }
    if (isRecommended) {
      return "the recommended first stop (advisory only, not applied)";
    }
    return "the driver's selection (their own decision)";
  }

  /** The route order position of a stop: the 1-based index in the payload's own `order` array. */
  function orderPosition(order, stopId) {
    var index = order.indexOf(stopId);
    return index < 0 ? null : index + 1;
  }

  /**
   * One marker whose div icon carries a compact label badge and, for a stop, its 1-based position
   * from the payload order (the `marker-number` span). `classes` only ever adds presentation classes
   * this module owns; the label text is escaped because it comes from a payload.
   */
  function marker(latlng, label, classes, badge) {
    var hasBadge = badge !== null && badge !== undefined && text(badge) !== "";
    var html = hasBadge
      ? '<span class="marker-number">' + escapeHtml(badge) + "</span>" +
        '<span class="' + classes + '">' + escapeHtml(label) + "</span>"
      : '<span class="' + classes + '">' + escapeHtml(label) + "</span>";
    var icon = window.L.divIcon({
      className: "routepilot-marker",
      html: html,
      iconSize: hasBadge ? [42, 24] : [24, 24],
      iconAnchor: hasBadge ? [21, 12] : [12, 12]
    });
    return window.L.marker(latlng, { icon: icon, title: label });
  }

  /** The START / FINISH marker: a plan location, never a service stop and never numbered. */
  function locationMarker(latlng, name, classes, containerLabel) {
    return marker(latlng, name, classes, null).bindPopup(
      "<strong>" + escapeHtml(name) + "</strong> - " + escapeHtml(containerLabel) +
        " (plan location)"
    );
  }

  /** One stop marker: the payload order number, the payload label and the stop's own role(s). */
  function stopMarker(stopId, latlng, label, position, orderLength, roles) {
    var badge = position === null ? null : position;
    var classes = "marker-stop" + (roles.length ? " " + roles.join(" ") : "");
    var isRecommended = roles.indexOf("marker-recommended") >= 0;
    var isSelected = roles.indexOf("marker-selected") >= 0;
    var positionText = position === null
      ? "Position in the route order: not read yet (no committed route in hand)."
      : "Route order position " + escapeHtml(position) + " of " + escapeHtml(orderLength) + ".";
    var roleText = isRecommended || isSelected
      ? "<br>" + escapeHtml("This stop is " + stopRoleText(isRecommended, isSelected) + ".")
      : "";
    var popup = "<strong>" + escapeHtml(label) + "</strong><br>" + positionText + roleText;
    return marker(latlng, text(stopId), classes, badge).bindPopup(popup);
  }

  /**
   * Draw the plan locations, the stops and the committed route order.
   *
   * `payload` is exactly what the API returned:
   *   { plan: <plan payload>, route: <committed-route payload>|null, stopLabels: {id: label},
   *     recommendedStopId: <the recommendation payload's own stop id>|null,
   *     selectedStopId: <plan.first_stop.selected_stop_id>|null }
   * Every coordinate and every id is used as given. `order` comes from the route payload, so the
   * drawn sequence is the engine's sequence, never a client-side sort.
   */
  function drawRoute(payload) {
    if (!state.map || !window.L) {
      return false;
    }
    var plan = payload.plan || {};
    var route = payload.route || null;
    var labels = payload.stopLabels || {};
    var recommendedStopId = text(payload.recommendedStopId) || null;
    var selectedStopId = text(payload.selectedStopId) || null;
    var points = [];
    var byId = {};

    (plan.stops || []).forEach(function (stop) {
      byId[text(stop.id)] = stop;
    });

    function stopLatLng(stopId) {
      var stop = byId[text(stopId)];
      if (!stop) {
        return null;
      }
      return [stop.latitude, stop.longitude];
    }

    state.layer.clearLayers();

    var startPoint = plan.departure
      ? [plan.departure.latitude, plan.departure.longitude]
      : null;
    var finishPoint = plan.finish ? [plan.finish.latitude, plan.finish.longitude] : null;

    if (startPoint) {
      points.push(startPoint);
      state.layer.addLayer(
        locationMarker(startPoint, "START", "marker-start", plan.departure.label)
      );
    }

    // The route order is a payload value. With no route in hand the plan's ENABLED stops are drawn
    // instead, unnumbered: coordinates still come from the payload, and no point is ever invented.
    var order = route && route.order ? route.order : [];
    var enabledStopIds = [];
    (plan.stops || []).forEach(function (stop) {
      if (stop.enabled === true) {
        enabledStopIds.push(text(stop.id));
      }
    });
    var drawn = order.length ? order : enabledStopIds;

    drawn.forEach(function (stopId) {
      var latlng = stopLatLng(stopId);
      if (!latlng) {
        return;
      }
      points.push(latlng);
      var label = labels[text(stopId)] || text(stopId);
      var isRecommended = recommendedStopId !== null && text(stopId) === recommendedStopId;
      var isSelected = selectedStopId !== null && text(stopId) === selectedStopId;
      var roles = [];
      if (isRecommended) {
        roles.push("marker-recommended");
      }
      if (isSelected) {
        roles.push("marker-selected");
      }
      state.layer.addLayer(stopMarker(
        stopId, latlng, label, orderPosition(order, text(stopId)), order.length, roles
      ));
    });

    if (finishPoint) {
      points.push(finishPoint);
      state.layer.addLayer(
        locationMarker(finishPoint, "FINISH", "marker-finish", plan.finish.label)
      );
    }

    // NO line, arc or other geometry is drawn between the stops: the route order is carried by the
    // number badge on each stop marker alone (real road routing is not implemented).

    if (points.length) {
      state.map.fitBounds(window.L.latLngBounds(points).pad(0.15));
    }
    return true;
  }

  /**
   * Make the tile state visible after the first tiles have had a chance to arrive.
   *
   * A tile layer reports no single "everything failed" event, so the honest rule is: after the
   * bounded grace period, no loaded tile at all means the tiles are unreachable. This is only ever
   * a map notice; nothing else in the workspace is affected.
   */
  function reportTilesAfterGrace(graceMs) {
    if (!state.tileLayer || typeof window.setTimeout !== "function") {
      return;
    }
    window.setTimeout(function () {
      if (state.tilesLoaded === 0 && !state.tilesReported) {
        state.tilesReported = true;
        setNotice(
          "No map tiles could be loaded (the tile service is unreachable from this machine, or " +
            "this environment is offline). The stop markers and their route-order numbers are " +
            "still drawn; the timeline, summary, recommendation and selection state are " +
            "unaffected and stay usable.",
          "warning"
        );
      }
    }, graceMs);
  }

  window.RoutePilotMap = {
    NO_ROUTE_GEOMETRY_DISCLOSURE: NO_ROUTE_GEOMETRY_DISCLOSURE,
    drawRoute: drawRoute,
    mountMap: mountMap,
    reportTilesAfterGrace: reportTilesAfterGrace,
    setNoticeSink: setNoticeSink,
    text: text
  };
})();
