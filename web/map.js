/*
  RoutePilot demo workspace - map controller (Stage 4 U15; spec section 34, D15/D39(c)).

  What this file does
  ===================

  Draws the departure, the finish and the stops on a Leaflet map with an OSM-compatible tile layer,
  and joins the route order with straight-line segments.

  What this file must NEVER do
  ============================

  * It computes no route and no metric. The order it draws is `order`, exactly as
    `GET /api/plans/{id}/route` returned it; the coordinates are the stop/plan coordinates exactly as
    the API returned them. No distance, duration, saving, rank, feasibility or fingerprint is
    derived here (D39(f)).
  * It does not decide the tile provider. `tile_url`, `tile_attribution`, `tile_max_zoom` and the
    Leaflet library URL all arrive from `GET /api/health` -> `map.configuration.values`, which
    resolves the approved `app_settings` keys with their documented defaults
    (`api/map_configuration.py`). No vendor URL is written into this file, and no map asset is
    vendored into this repository.
  * It never presents the drawn line as road routing. Every segment is a straight line between two
    payload coordinates and is labelled synthetic / straight-line wherever it is described.

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
*/

(function () {
  "use strict";

  //: Zoom used when the drawn features fit in no better way (Leaflet picks a tighter one itself).
  var FALLBACK_ZOOM = 6;

  //: How many tile requests may fail before the layer is called unreachable. One bad tile in the
  //: middle of a working map must not be reported as "the map is unavailable".
  var TILE_FAILURE_THRESHOLD = 4;

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
            "markers and the synthetic straight-line route order are still drawn; the timeline, " +
            "summary, recommendation and selection state below are unaffected.",
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

  function marker(latlng, label, cssClass) {
    var icon = window.L.divIcon({
      className: "routepilot-marker",
      html: '<span class="' + cssClass + '">' + escapeHtml(label) + "</span>",
      iconSize: [24, 24],
      iconAnchor: [12, 12]
    });
    return window.L.marker(latlng, { icon: icon, title: label });
  }

  function escapeHtml(value) {
    return text(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /**
   * Draw the plan locations, the stops and the committed route order.
   *
   * `payload` is exactly what the API returned:
   *   { plan: <plan payload>, route: <committed-route payload>|null, stopLabels: {id: label} }
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
        marker(startPoint, "START", "marker-start").bindPopup(
          "<strong>START</strong> - " + escapeHtml(plan.departure.label) + " (plan location)"
        )
      );
    }

    var order = route && route.order ? route.order : [];
    order.forEach(function (stopId) {
      var latlng = stopLatLng(stopId);
      if (!latlng) {
        return;
      }
      points.push(latlng);
      var label = labels[text(stopId)] || text(stopId);
      state.layer.addLayer(
        marker(latlng, text(stopId), "marker-stop").bindPopup(
          "<strong>" + escapeHtml(label) + "</strong><br>" +
            "Route order position " + (order.indexOf(stopId) + 1) + " of " + order.length + "."
        )
      );
    });

    if (finishPoint) {
      points.push(finishPoint);
      state.layer.addLayer(
        marker(finishPoint, "FINISH", "marker-finish").bindPopup(
          "<strong>FINISH</strong> - " + escapeHtml(plan.finish.label) + " (plan location)"
        )
      );
    }

    if (points.length > 1) {
      // SYNTHETIC STRAIGHT-LINE GEOMETRY: one straight segment per consecutive pair of the route
      // order above. This is not road geometry, carries no road distance and is never presented as
      // road routing (spec section 34, D15).
      var line = window.L.polyline(points, {
        color: "#6b4bb5",
        weight: 3,
        opacity: 0.85,
        dashArray: "6 6"
      });
      line.bindPopup(
        "<strong>Synthetic straight-line geometry</strong><br>" +
          "The straight segments joining the route order returned by the API. This is NOT road " +
          "routing: no roads, no turn instructions, no traffic and no road distance."
      );
      line.bindTooltip("synthetic straight-line geometry (not road routing)", { sticky: true });
      state.layer.addLayer(line);
    }

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
            "this environment is offline). The stop markers and the synthetic straight-line route " +
            "order are still drawn; the timeline, summary, recommendation and selection state are " +
            "unaffected and stay usable.",
          "warning"
        );
      }
    }, graceMs);
  }

  window.RoutePilotMap = {
    drawRoute: drawRoute,
    mountMap: mountMap,
    reportTilesAfterGrace: reportTilesAfterGrace,
    setNoticeSink: setNoticeSink,
    text: text
  };
})();
