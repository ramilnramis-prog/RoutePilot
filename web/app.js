/*
  RoutePilot demo workspace - application script (Stage 4 U15; spec sections 26, 31, 34; D15/D16/
  D19/D32/D39).

  READ AND RENDER ONLY. This unit renders what the U13/U14 API already serves: the plan list, one
  plan, the recommendation (on request), the committed route (on request) and the run-history
  placeholder. The interactive override controls - accept the recommendation, choose another first
  stop, cancel/unpin, disable/restore a stop, change a priority, recalculate - and the full
  run-history view are U16; this file deliberately contains no such control and no code that changes
  server state beyond `POST /api/plans` (the documented "create or open the DEMO plan" step).

  THE ONE RULE THIS FILE EXISTS TO KEEP
  =====================================

  NO BUSINESS FORMULA LIVES HERE. Every number, order, metric, feasibility result, violation,
  fingerprint, duration, saving and rank is rendered from an API payload UNCHANGED. This file may
  format a value for reading (seconds -> "1 h 58 min", an ISO instant -> local wall-clock text, a
  metre count -> "12.3 km") and it may place payload values into a table in the order the payload
  gave them. It must never add, subtract, average, total, rank, re-order, re-score, infer feasibility
  or hash anything, and it must never invent a route, a candidate order, a saving or a fingerprint.

  WHAT IS READ FROM WHERE
  =======================

  | UI surface                              | payload                                     |
  |-----------------------------------------|---------------------------------------------|
  | DEMO/SYNTHETIC and capability honesty    | `GET /api/health` (data_provenance,          |
  |                                         | demo_data, implemented/not_implemented       |
  |                                         | capabilities, route_modes, timezone_data,    |
  |                                         | computation)                                 |
  | map configuration (tiles + library)      | `GET /api/health` -> `map`                   |
  | plan list / one plan / plan summary      | `GET /api/plans`, `GET /api/plans/{id}`,     |
  |                                         | `POST /api/plans` (open-or-create the demo)  |
  | recommendation, alternatives, rejected   | `GET /api/plans/{id}/recommendation`         |
  | order, timeline, metrics, violations     | `GET /api/plans/{id}/route`                  |
  | run history (placeholder)                | `GET /api/plans/{id}/runs`                   |

  The recommendation and the route are only requested when the driver presses the button, because
  each one is a real synchronous engine computation (up to the accepted ~8 s worst case) and the
  measured `computation_seconds` is shown afterwards.

  A recommendation is never presented as applied or selected state, and the plan's first-stop state
  is only ever read: `selection-panel` and `first-stop-state` report what the API says the driver's
  decision currently is, with its provenance and pinning (D4-D11/D32).
*/

(function () {
  "use strict";

  var API = {
    health: "/api/health",
    plans: "/api/plans",
    plan: function (planId) {
      return "/api/plans/" + encodeURIComponent(planId);
    },
    recommendation: function (planId) {
      return "/api/plans/" + encodeURIComponent(planId) + "/recommendation";
    },
    route: function (planId) {
      return "/api/plans/" + encodeURIComponent(planId) + "/route";
    },
    runs: function (planId) {
      return "/api/plans/" + encodeURIComponent(planId) + "/runs";
    }
  };

  var state = {
    health: null,
    planList: [],
    plan: null,
    planId: null,
    recommendation: null,
    route: null,
    stopLabels: {}
  };

  var REQUIRED_IDS = [
    "status-banner", "latency-notice", "plan-select", "create-demo-plan",
    "recommendation-panel", "advisory-banner", "recommended-stop", "alternatives",
    "rejected-candidates", "selection-panel", "first-stop-state", "route-panel", "timeline",
    "summary-panel", "before-after", "map", "map-notice", "history-panel", "loading"
  ];

  // ------------------------------------------------------------------ DOM --
  function byId(id) {
    return document.getElementById(id);
  }

  function clear(node) {
    while (node && node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function el(tag, className, value) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (value !== undefined && value !== null) {
      node.textContent = String(value);
    }
    return node;
  }

  function replace(node, children) {
    clear(node);
    (children || []).forEach(function (child) {
      if (child) {
        node.appendChild(child);
      }
    });
  }

  function paragraph(className, value) {
    return el("p", className, value);
  }

  /** A definition list of `[term, value]` pairs, values rendered as text unless they are nodes. */
  function definitionList(pairs) {
    var list = el("dl", "kv");
    pairs.forEach(function (pair) {
      if (pair[1] === undefined || pair[1] === null || pair[1] === "") {
        return;
      }
      list.appendChild(el("dt", null, pair[0]));
      var dd = el("dd");
      if (pair[1] instanceof Node) {
        dd.appendChild(pair[1]);
      } else {
        dd.textContent = String(pair[1]);
      }
      list.appendChild(dd);
    });
    return list;
  }

  function badge(value, kind) {
    return el("span", "badge badge-" + (kind || String(value)), String(value));
  }

  function badgeFor(value) {
    return badge(value, value ? "true" : "false");
  }

  /**
   * A table. `columns` is `[header, key, options]`; `options.numeric` right-aligns, `options.render`
   * formats the cell. Formatting only - every rendered value is the payload's own value.
   */
  function table(captionText, columns, rows) {
    var wrapper = document.createDocumentFragment();
    if (captionText) {
      wrapper.appendChild(el("caption", null, captionText));
    }
    var head = el("tr");
    columns.forEach(function (column) {
      var cell = el("th", column[2] && column[2].numeric ? "num" : null, column[0]);
      head.appendChild(cell);
    });
    var thead = el("thead");
    thead.appendChild(head);
    var tbody = el("tbody");
    (rows || []).forEach(function (row) {
      var tr = el("tr", row.__className || null);
      columns.forEach(function (column) {
        var options = column[2] || {};
        var raw = row[column[1]];
        var td = el("td", options.numeric ? "num" : null);
        if (options.render) {
          var rendered = options.render(raw, row);
          if (rendered instanceof Node) {
            td.appendChild(rendered);
          } else {
            td.textContent = rendered === null || rendered === undefined ? "" : String(rendered);
          }
        } else {
          td.textContent = raw === null || raw === undefined ? "" : String(raw);
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    var element = el("table");
    element.appendChild(thead);
    element.appendChild(tbody);
    wrapper.appendChild(element);
    var container = el("div");
    container.appendChild(wrapper);
    return container;
  }

  // ---------------------------------------------------------- formatting --
  /** A seconds count as readable text. FORMATTING ONLY: the number itself is the payload's. */
  function duration(seconds) {
    if (seconds === null || seconds === undefined) {
      return "-";
    }
    var value = Number(seconds);
    if (!isFinite(value)) {
      return String(seconds);
    }
    var sign = value < 0 ? "-" : "";
    var total = Math.abs(value);
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    var parts = [];
    if (hours) {
      parts.push(hours + " h");
    }
    if (minutes || hours) {
      parts.push(minutes + " min");
    }
    parts.push(secs + " s");
    return sign + parts.join(" ");
  }

  /** A metre count as readable text. FORMATTING ONLY. */
  function distance(metres) {
    if (metres === null || metres === undefined) {
      return "-";
    }
    var value = Number(metres);
    if (!isFinite(value)) {
      return String(metres);
    }
    return (value / 1000).toFixed(1) + " km";
  }

  /**
   * A UTC ISO-8601 instant as local wall-clock text in the plan's own IANA zone.
   *
   * This is presentation of the API's instant, not a conversion of the plan's time model: the
   * timezone string is the plan's own `timezone` field. When the browser has no data for that zone
   * the zone is reported as unknown instead of silently showing a different zone's clock.
   */
  function instant(isoText, timezoneName) {
    if (!isoText) {
      return "-";
    }
    var when = new Date(isoText);
    if (isNaN(when.getTime())) {
      return String(isoText);
    }
    var options = {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false
    };
    if (timezoneName) {
      options.timeZone = timezoneName;
    }
    try {
      return when.toLocaleString(undefined, options) +
        (timezoneName ? " (" + timezoneName + ")" : " (browser local time)");
    } catch (error) {
      return when.toISOString() + " (UTC; the plan time zone '" + timezoneName +
        "' is unknown to this browser)";
    }
  }

  function text(value) {
    return value === null || value === undefined ? "" : String(value);
  }

  function fingerprint(value) {
    return value ? el("span", "mono", String(value)) : null;
  }

  // ---------------------------------------------------------------- http --
  function ApiError(message, status, code) {
    this.name = "ApiError";
    this.message = message;
    this.status = status;
    this.code = code;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function request(path, options) {
    return fetch(path, options || {}).then(function (response) {
      return response.text().then(function (body) {
        var payload = null;
        try {
          payload = JSON.parse(body);
        } catch (error) {
          payload = null;
        }
        if (!response.ok) {
          var detail = payload && payload.error ? payload.error : null;
          throw new ApiError(
            detail ? detail.message : "HTTP " + response.status + " from " + path,
            response.status,
            detail ? detail.code : "unknown"
          );
        }
        if (payload === null) {
          throw new ApiError("the API answered " + path + " with a body that is not JSON", 0,
            "invalid_body");
        }
        return payload;
      });
    });
  }

  // ---------------------------------------------------------- UI status --
  function setStatus(message, tone) {
    var banner = byId("status-banner");
    banner.textContent = message;
    banner.className = "banner banner-status" + (tone ? " banner-" + tone : "");
  }

  function showError(error) {
    var message = error && error.message ? error.message : String(error);
    var suffix = error && error.code ? " [" + error.code + "]" : "";
    setStatus("Request failed: " + message + suffix, "error");
  }

  /** The computing/loading state of D39(e): shown while a computation is in flight. */
  function setLoading(isLoading, note) {
    var loading = byId("loading");
    var extra = note ? " " + note : "";
    loading.textContent = isLoading
      ? "Computing\u2026 the API is recomputing this request synchronously. This can take several " +
        "seconds at the ~50-stop scale." + extra
      : "";
    loading.hidden = !isLoading;
    ["request-recommendation", "request-route"].forEach(function (id) {
      var button = byId(id);
      if (button) {
        button.disabled = !!isLoading;
      }
    });
  }

  function setMapNotice(message, tone) {
    var notice = byId("map-notice");
    if (!message) {
      notice.textContent = "";
      notice.hidden = true;
      return;
    }
    notice.textContent = String(message);
    notice.className = "notice" + (tone === "info" ? "" : "");
    notice.hidden = false;
  }

  // ------------------------------------------------------------- honesty --
  function renderHealth(health) {
    var data = health || {};
    var demo = data.demo_data || {};
    var computation = data.computation || {};

    setStatus(
      "Workspace ready. Data provenance: " + text(data.data_provenance) +
        " \u2014 " + text(demo.warning) +
        ". Time zone data: " + text((data.timezone_data || {}).source) +
        " (IANA " + text((data.timezone_data || {}).iana_version || "unknown") + ").",
      "ok"
    );

    // The latency notice states the accepted MVP worst case that the API itself reports.
    var latency = byId("latency-notice");
    replace(latency, [
      el("strong", null, "Latency: "),
      document.createTextNode(
        text(computation.accepted_mvp_latency) +
          " Every computation response carries its measured computation_seconds, which is shown " +
          "below after each request. Synchronous: " + text(computation.synchronous) +
          "; background job queue: " + text(computation.background_job_queue) +
          "; per-plan single-flight lock wait bound: " +
          text(computation.lock_wait_bound_seconds) + " s."
      )
    ]);

    renderCapabilities(data);
  }

  /**
   * The "what this build does NOT do" list, built from the API's own capability report.
   *
   * Every entry - the capability name, its status, its explanation and what it would require - comes
   * from `GET /api/health`; this function states no capability of its own, and it has no row that the
   * payload does not carry: a capability this build cannot speak for is left to the API's report
   * rather than asserted here. The unimplemented route modes of `route_modes.not_implemented` are
   * listed for the same reason, so the page can show which modes exist and are *not* implemented
   * (D19) without offering a control for any of them.
   */
  function renderCapabilities(health) {
    var container = byId("capabilities-panel");
    clear(container);

    var notImplemented = health.not_implemented_capabilities || [];
    var list = el("ul", "capabilities");
    notImplemented.forEach(function (entry) {
      var item = el("li");
      item.appendChild(el("span", "capability-name", text(entry.capability)));
      item.appendChild(el("span", "capability-status", text(entry.status)));
      item.appendChild(paragraph(null, text(entry.detail)));
      if (entry.requires) {
        item.appendChild(paragraph("muted", "Would require: " + text(entry.requires)));
      }
      list.appendChild(item);
    });
    container.appendChild(list);

    var modes = health.route_modes || {};
    container.appendChild(definitionList([
      ["Implemented route mode", (modes.implemented || []).join(", ")],
      ["Declared but NOT implemented", (modes.not_implemented || []).join(", ") + " (no control " +
        "for these exists on this page, and no fallback to SMART_ROUTE is ever implied)"]
    ]));

    var implemented = health.implemented_capabilities || [];
    container.appendChild(paragraph(
      "muted",
      "Implemented and reported by the API: " +
        implemented.map(function (entry) { return text(entry.capability); }).join(", ") + "."
    ));
  }

  // -------------------------------------------------- map configuration --
  /**
   * Read the map configuration from `GET /api/health` -> `map` and load the Leaflet library from the
   * configured URL. No tile URL, attribution, max zoom or library URL is written in this file: they
   * are the API's values (`app_settings` keys with their documented defaults).
   */
  function setupMap(health) {
    var configuration = health.map || null;
    RoutePilotMap.setNoticeSink(setMapNotice);

    if (configuration && configuration.defaulted_keys && configuration.defaulted_keys.length) {
      setMapNotice(
        "Map configuration: read over the API. Stored in app_settings: " +
          ((configuration.configured_keys || []).join(", ") || "nothing") +
          ". Using the documented default for: " + configuration.defaulted_keys.join(", ") + ".",
        "info"
      );
    }

    var values = (configuration && configuration.values) || {};
    var libraryUrl = values.map_library_url;
    var stylesheetUrl = values.map_library_css_url;
    if (stylesheetUrl) {
      var link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = stylesheetUrl;
      document.head.appendChild(link);
    }

    loadScript(libraryUrl)
      .then(function () {
        var map = RoutePilotMap.mountMap(configuration, "map");
        if (map) {
          RoutePilotMap.reportTilesAfterGrace(6000);
          drawMap();
        }
      })
      .catch(function () {
        RoutePilotMap.mountMap(null, "map");
      });
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      if (window.L && window.L.map) {
        resolve();
        return;
      }
      if (!url || typeof url !== "string") {
        reject(new Error("no map library URL is configured"));
        return;
      }
      var script = document.createElement("script");
      script.src = url;
      script.async = false;
      script.onload = function () {
        if (window.L && window.L.map) {
          resolve();
        } else {
          reject(new Error("the map library loaded but defines no map object"));
        }
      };
      script.onerror = function () {
        reject(new Error("the map library could not be loaded from the configured URL"));
      };
      document.head.appendChild(script);
    });
  }

  /** Redraw the map from the payloads currently in hand (never from a client-side computation). */
  function drawMap() {
    if (!state.plan) {
      return;
    }
    RoutePilotMap.drawRoute({
      plan: state.plan,
      route: state.route,
      stopLabels: state.stopLabels
    });
  }

  // --------------------------------------------------------------- plans --
  function loadPlans() {
    return request(API.plans).then(function (document_) {
      state.planList = document_.data || [];
      var select = byId("plan-select");
      replace(select, []);
      if (!state.planList.length) {
        select.appendChild(el("option", null, "no stored plan yet"));
        return null;
      }
      state.planList.forEach(function (summary) {
        var option = el("option", null, text(summary.id) + " \u2014 " + text(summary.departure_label) +
          " \u2192 " + text(summary.finish_label));
        option.value = summary.id;
        select.appendChild(option);
      });
      return state.planList[0].id;
    });
  }

  /** `POST /api/plans` - the documented "create or open the deterministic DEMO plan" step. */
  function createDemoPlan() {
    return request(API.plans, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function (document_) {
      return document_.data;
    });
  }

  function openPlan(planId) {
    return request(API.plan(planId)).then(function (document_) {
      var plan = document_.data;
      state.plan = plan;
      state.planId = plan.id;
      state.recommendation = null;
      state.route = null;
      state.stopLabels = {};
      (plan.stops || []).forEach(function (stop) {
        state.stopLabels[text(stop.id)] = text(stop.normalized_address || stop.raw_address ||
          stop.id);
      });
      var select = byId("plan-select");
      if (select && !select.value) {
        select.value = plan.id;
      }
      renderPlan(plan, document_);
      renderRecommendationEmpty();
      renderRouteEmpty();
      renderSummaryEmpty();
      drawMap();
      return plan;
    });
  }

  function renderPlan(plan, document_) {
    var firstStop = plan.first_stop || {};
    var counts = plan.counts || {};
    var summary = byId("plan-summary");
    replace(summary, [
      el("h3", null, "Plan summary"),
      definitionList([
        ["Plan id", plan.id],
        ["DEMO/SYNTHETIC provenance", badge(text(plan.data_provenance), "synthetic")],
        ["Time zone (IANA)", plan.timezone],
        ["Route mode", badge(text(plan.route_mode), "mode")],
        ["Stops", text(counts.stops) + " total, " + text(counts.enabled_stops) + " enabled, " +
          text(counts.disabled_stops) + " disabled"],
        ["Departure (START, plan location)", plan.departure
          ? text(plan.departure.label) + " (" + text(plan.departure.latitude) + ", " +
            text(plan.departure.longitude) + ")"
          : null],
        ["Finish (FINISH, plan location)", plan.finish
          ? text(plan.finish.label) + " (" + text(plan.finish.latitude) + ", " +
            text(plan.finish.longitude) + ")"
          : null],
        ["Departure time", instant(plan.departure_time, plan.timezone)],
        ["Default service duration", duration(plan.default_service_duration_sec)],
        ["Window end policy", plan.window_end_policy],
        ["Inputs fingerprint (from the API)", fingerprint(plan.inputs_fingerprint)],
        ["API payload type", document_ ? text(document_.type) + " / api_version " +
          text(document_.api_version) : null]
      ]),
      el("h3", null, "First-stop state and its provenance (read-only)"),
      // The plan's own first-stop state: the DRIVER's decision, never the engine's recommendation.
      firstStopBlock(firstStop),
      el("p", "hint", "The selection controls that change this state (accept, choose another stop, " +
        "cancel) are the U16 override controls. This page only reports what the API says the " +
        "decision currently is.")
    ]);
    renderStopTable(plan);
  }

  function firstStopBlock(firstStop) {
    return definitionList([
      ["State", badge(text(firstStop.state), "mode")],
      ["Mode", text(firstStop.mode)],
      ["Selected first stop", firstStop.selected_stop_id || "none (awaiting the driver's choice)"],
      ["Selection source (provenance)", firstStop.selection_source || "none"],
      ["Pinned", text(firstStop.pinned)],
      ["Meaning", text(firstStop.description)]
    ]);
  }

  function renderStopTable(plan) {
    var stops = (plan.stops || []).map(function (stop) {
      var window_ = stop.service_window || {};
      return {
        id: stop.id,
        input_position: stop.input_position,
        label: stop.normalized_address || stop.raw_address || "",
        enabled: stop.enabled,
        priority: stop.priority,
        duration: stop.service_duration_sec,
        window: window_.description,
        geocode_status: stop.geocode_status,
        service_status: stop.service_status
      };
    });
    var container = el("div");
    container.appendChild(table(
      "Stops as stored (input_position is the driver's own order; every column is a payload value)",
      [
        ["#", "input_position", { numeric: true }],
        ["Stop id", "id"],
        ["Label", "label"],
        ["Enabled", "enabled", { render: function (value) { return badgeFor(value); } }],
        ["Priority", "priority", { numeric: true }],
        ["Service duration", "duration", { render: duration, numeric: true }],
        ["Service window", "window"],
        ["Geocode status", "geocode_status"],
        ["Service status", "service_status"]
      ],
      stops
    ));
    byId("plan-summary").appendChild(container);
  }

  // ------------------------------------------------------ recommendation --
  function renderRecommendationEmpty() {
    replace(byId("recommendation-panel"), [
      paragraph("muted", "No recommendation requested yet for this plan. Press \u201cCompute the " +
        "recommendation\u201d: the API recomputes it live for that request and writes nothing.")
    ]);
    byId("recommended-stop").textContent = "";
    replace(byId("alternatives"), [
      paragraph("muted", "The ranked alternatives appear here with their complete-route metrics.")
    ]);
    replace(byId("rejected-candidates"), [
      paragraph("muted", "The rejected candidates and their violating stops appear here.")
    ]);
    replace(byId("selection-panel"), [
      el("h3", null, "Selection (the driver's decision - reported, not changed here)"),
      paragraph("muted", "The API's own first-stop state for this plan is shown in the plan " +
        "summary above. Changing it is the U16 override control, not this unit.")
    ]);
    replace(byId("first-stop-state"), []);
  }

  function candidateRows(candidates) {
    return candidates.map(function (candidate) {
      var firstLeg = candidate.first_leg || {};
      var complete = candidate.complete_route || {};
      var row = {
        stop_id: candidate.stop_id,
        rank: candidate.rank,
        feasible: candidate.feasible,
        first_leg_travel: firstLeg.travel_sec,
        first_leg_arrival: firstLeg.estimated_arrival,
        first_leg_window_start: firstLeg.service_window_start,
        first_leg_waiting: firstLeg.waiting_sec,
        first_leg_service_start: firstLeg.estimated_service_start,
        first_leg_lateness: firstLeg.lateness_sec,
        duration_sec: complete.duration_sec,
        travel_sec: complete.travel_sec,
        waiting_sec: complete.waiting_sec,
        service_sec: complete.service_sec,
        finish_arrival: complete.finish_arrival,
        max_lateness_sec: complete.max_lateness_sec,
        violating_stop_ids: (complete.violating_stop_ids || []).join(", ")
      };
      row.__className = candidate.rank === 1 ? "recommended-row" :
        (candidate.feasible ? null : "rejected-row");
      return row;
    });
  }

  function candidateColumns(timezoneName) {
    return [
      ["Rank", "rank", { numeric: true, render: function (value) {
        return value === null || value === undefined ? "rejected" : value;
      } }],
      ["Stop id", "stop_id"],
      ["Complete route", "feasible", { render: function (value) { return badgeFor(value); } }],
      ["First leg travel", "first_leg_travel", { render: duration, numeric: true }],
      ["First-leg ETA", "first_leg_arrival", { render: function (value) {
        return instant(value, timezoneName);
      } }],
      ["Waiting", "first_leg_waiting", { render: duration, numeric: true }],
      ["Service start", "first_leg_service_start", { render: function (value) {
        return instant(value, timezoneName);
      } }],
      ["First-stop lateness", "first_leg_lateness", { render: duration, numeric: true }],
      ["Complete duration", "duration_sec", { render: duration, numeric: true }],
      ["Complete travel", "travel_sec", { render: duration, numeric: true }],
      ["Complete waiting", "waiting_sec", { render: duration, numeric: true }],
      ["Total service", "service_sec", { render: duration, numeric: true }],
      ["FINISH arrival", "finish_arrival", { render: function (value) {
        return instant(value, timezoneName);
      } }],
      ["Max lateness", "max_lateness_sec", { render: duration, numeric: true }],
      ["Violating stops", "violating_stop_ids"]
    ];
  }

  function renderRecommendation(document_) {
    var data = document_.data;
    var timezoneName = state.plan ? state.plan.timezone : null;
    var counts = data.counts || {};
    var policy = data.policy || {};
    var fingerprints = data.fingerprints || {};

    var panel = byId("recommendation-panel");
    replace(panel, [
      definitionList([
        ["Outcome status", badge(text(data.status), data.status === "recommended" ? "true" : "false")],
        ["Advisory", badge(text(data.advisory), "mode")],
        ["Applied decision", text(data.applied_decision)],
        ["Plan state", text(data.as_plan_state)],
        ["Live recompute", text(document_.live_recompute) + (document_.computed_at
          ? " (computed_at " + text(document_.computed_at) + ")" : "")],
        ["Measured computation_seconds", text(data.computation_seconds)],
        ["Candidates evaluated", text(counts.candidates_evaluated) + " (ranked " +
          text(counts.ranked) + ", returned " + text(counts.ranked_returned) + ", rejected " +
          text(counts.rejected) + "; optimizer runs " + text(counts.optimizer_runs) + ")"],
        ["Disabled stops excluded", (data.disabled_stop_ids || []).join(", ") || "none"],
        ["Objective policy", text(policy.name) + (policy.provisional ? " (provisional)" : "") +
          " \u2014 window end policy " + text(policy.window_end_policy)],
        ["Inputs fingerprint (from the API)", fingerprint(fingerprints.inputs_fingerprint)],
        ["Note", text(data.note)]
      ])
    ]);

    var recommended = byId("recommended-stop");
    if (data.recommended_stop_id) {
      replace(recommended, [
        "Recommended first stop: ", el("strong", null, text(data.recommended_stop_id)),
        " \u2014 advisory only; the plan's first stop is unchanged until the driver decides."
      ]);
    } else {
      replace(recommended, [
        "No fully feasible first stop: the engine's outcome is ", el("strong", null, text(data.status)),
        ". This is a valid answer with the diagnostics below, not an error and not a fabricated " +
          "winner (v2 section 14)."
      ]);
    }

    var alternatives = (data.ranked || []);
    replace(byId("alternatives"), [
      paragraph("hint", "Top-K view: " + text(counts.ranked_returned) + " of " +
        text(counts.ranked) + " ranked candidates, in the engine's own rank order. Every metric " +
        "below is the complete route the engine evaluated."),
      alternatives.length
        ? table("Ranked alternatives (complete-route metrics, as returned)",
            candidateColumns(timezoneName), candidateRows(alternatives))
        : paragraph("muted", "The API returned no ranked candidates for this plan.")
    ]);

    var rejected = (data.rejected || []);
    replace(byId("rejected-candidates"), [
      paragraph("hint", "Rejected because the complete route cannot serve a hard window. A rejected " +
        "candidate is never ranked and never presented as a recommendation."),
      rejected.length
        ? table("Rejected candidates (with their violating stops, as returned)",
            candidateColumns(timezoneName), candidateRows(rejected))
        : paragraph("muted", "The API returned no rejected candidates for this plan.")
    ]);

    var diagnostics = (data.diagnostics || []);
    if (diagnostics.length) {
      byId("rejected-candidates").appendChild(table(
        "Diagnostics (the violating stop and the candidate it was rejected for are different ids)",
        [
          ["Violating stop", "stop_id"],
          ["Candidate first stop", "candidate_stop_id"],
          ["Code", "code"],
          ["Violation kind", "violation_kind"],
          ["Message", "message"]
        ],
        diagnostics
      ));
    }

    replace(byId("selection-panel"), [
      el("h3", null, "Selection (the driver's decision - reported, not changed here)"),
      paragraph(null, "This recommendation did NOT change the plan. The stored first-stop state " +
        "still reads: " + firstStopSentence()),
      paragraph("hint", "Accepting the recommendation, choosing another stop or cancelling it are " +
        "the U16 override controls; this unit requests and renders only.")
    ]);
    replace(byId("first-stop-state"), [
      el("h3", null, "First-stop state after this recommendation (unchanged)"),
      firstStopBlock((state.plan && state.plan.first_stop) || {})
    ]);
    byId("first-stop-state").appendChild(paragraph("hint", text(data.note)));
  }

  function firstStopSentence() {
    var firstStop = (state.plan && state.plan.first_stop) || {};
    return text(firstStop.state) + " (mode " + text(firstStop.mode) + ", selected " +
      text(firstStop.selected_stop_id || "none") + ", source " +
      text(firstStop.selection_source || "none") + ", pinned " + text(firstStop.pinned) + ")";
  }

  // --------------------------------------------------------------- route --
  function renderRouteEmpty() {
    replace(byId("route-panel"), [
      paragraph("muted", "No committed route requested yet. Press \u201cCompute the committed " +
        "route\u201d: the API computes the route for the plan's current selection and writes " +
        "nothing.")
    ]);
    replace(byId("timeline"), [
      paragraph("muted", "The route order and per-stop timeline appear here.")
    ]);
  }

  function timelineRows(timeline) {
    return (timeline || []).map(function (row) {
      return {
        stop_id: row.stop_id,
        departure_from_previous: row.departure_from_previous,
        travel_sec: row.travel_sec,
        estimated_arrival: row.estimated_arrival,
        window_kind: row.window_kind,
        service_window_start: row.service_window_start,
        service_window_end: row.service_window_end,
        window_end_policy: row.window_end_policy,
        waiting_sec: row.waiting_sec,
        service_start: row.service_start,
        service_duration_sec: row.service_duration_sec,
        estimated_departure: row.estimated_departure,
        lateness_sec: row.lateness_sec,
        finish_overtime_sec: row.finish_overtime_sec,
        feasibility: row.feasibility,
        flags: (row.flags || []).join(", ")
      };
    });
  }

  function renderRoute(document_) {
    var data = document_.data;
    var timezoneName = state.plan ? state.plan.timezone : null;
    var metrics = data.metrics || {};
    var after = metrics.after || {};
    var selection = data.selection || {};
    var violations = data.violations || [];

    replace(byId("route-panel"), [
      definitionList([
        ["Route status", badge(text(data.status), data.status === "ok" ? "true" : "false")],
        ["Live recompute", text(document_.live_recompute)],
        ["Measured computation_seconds", text(data.computation_seconds)],
        ["Order (as returned)", (data.order || []).join(" \u2192 ") || "empty"],
        ["Committed for the driver's selection", "mode " + text(selection.mode) + ", stop " +
          text(selection.selected_stop_id || "none") + ", source " +
          text(selection.selection_source || "none") + ", pinned " + text(selection.pinned)],
        ["Feasible (complete route, FINISH leg included)", badgeFor(after.feasible)],
        ["Provenance", text(data.provenance)],
        ["tzdata version", text(data.tzdata_version)],
        ["Inputs fingerprint (from the API)", fingerprint((data.fingerprints || {}).inputs_fingerprint)],
        ["Route fingerprint (from the API)", fingerprint((data.fingerprints || {}).route_fingerprint)]
      ]),
      paragraph("hint", "Arrival, waiting, service start, service duration, departure, the local " +
        "service window and the lateness below are the engine's own timeline rows, printed as " +
        "returned. The local clock is the plan's zone (" + text(timezoneName) + ").")
    ]);

    replace(byId("timeline"), [
      table("Route order and timeline (every column is a payload value; times are in the plan's zone)",
        [
          ["Stop", "stop_id"],
          ["Departed previous", "departure_from_previous", { render: function (value) {
            return instant(value, timezoneName);
          } }],
          ["Travel", "travel_sec", { render: duration, numeric: true }],
          ["ETA (arrival)", "estimated_arrival", { render: function (value) {
            return instant(value, timezoneName);
          } }],
          ["Window", "window_kind"],
          ["Local window start", "service_window_start", { render: function (value) {
            return instant(value, timezoneName);
          } }],
          ["Local window end", "service_window_end", { render: function (value) {
            return instant(value, timezoneName);
          } }],
          ["Window end policy", "window_end_policy"],
          ["Waiting", "waiting_sec", { render: duration, numeric: true }],
          ["Service start", "service_start", { render: function (value) {
            return instant(value, timezoneName);
          } }],
          ["Service duration", "service_duration_sec", { render: duration, numeric: true }],
          ["Departure", "estimated_departure", { render: function (value) {
            return instant(value, timezoneName);
          } }],
          ["Lateness", "lateness_sec", { render: duration, numeric: true }],
          ["FINISH overtime", "finish_overtime_sec", { render: duration, numeric: true }],
          ["Feasibility", "feasibility"],
          ["Flags", "flags"]
        ],
        timelineRows(data.timeline)
      ),
      violations.length
        ? table("Violations (explicit, never folded into a metric)",
            [
              ["Stop", "stop_id"],
              ["Kind", "kind"],
              ["Message", "message"],
              ["Service start", "service_start", { render: function (value) {
                return instant(value, timezoneName);
              } }],
              ["Window end", "service_window_end", { render: function (value) {
                return instant(value, timezoneName);
              } }]
            ],
            violations)
        : paragraph("muted", "No violations were reported for this committed route."),
      paragraph("legend map-legend-synthetic", "The map draws this exact order as synthetic " +
        "straight-line geometry - not road routing.")
    ]);

    renderSummary(data);
    drawMap();
  }

  // ------------------------------------------------------------- summary --
  function renderSummaryEmpty() {
    replace(byId("summary-panel"), [
      paragraph("muted", "Compute the committed route to see BEFORE vs AFTER. No saving, distance " +
        "or duration is computed on this page: every figure is the API's.")
    ]);
    replace(byId("before-after"), []);
  }

  function renderSummary(data) {
    var metrics = data.metrics || {};
    var after = metrics.after || {};
    var before = metrics.user_baseline || null;
    var algorithm = metrics.algorithm_baseline || null;

    replace(byId("summary-panel"), [
      definitionList([
        ["AFTER: complete route duration", duration(after.duration_sec)],
        ["AFTER: travel / waiting / service", duration(after.travel_sec) + " / " +
          duration(after.waiting_sec) + " / " + duration(after.service_sec)],
        ["AFTER: distance", distance(after.distance_m)],
        ["AFTER: FINISH arrival", instant(after.finish_arrival,
          state.plan ? state.plan.timezone : null)],
        ["AFTER: feasible", badgeFor(after.feasible)],
        ["BEFORE (the driver's own input order)", before
          ? duration(before.duration_sec) + ", " + distance(before.distance_m) +
            ", feasible " + text(before.feasible) +
            " (" + text(before.baseline_kind) + ")"
          : "not reported by the API for this route"],
        ["Saved duration (reported by the API)", metrics.saved_duration_sec === null ||
          metrics.saved_duration_sec === undefined ? "not reported" :
          duration(metrics.saved_duration_sec)],
        ["Saved distance (reported by the API)", metrics.saved_distance_m === null ||
          metrics.saved_distance_m === undefined ? "not reported" :
          distance(metrics.saved_distance_m)],
        ["Algorithm baseline (internal, never the driver's BEFORE)", algorithm
          ? duration(algorithm.duration_sec) + ", " + distance(algorithm.distance_m) +
            " (" + text(algorithm.baseline_kind) + ")"
          : "not reported"],
        ["Violations", text((data.violations || []).length) + " reported"]
      ])
    ]);

    var timezoneName = state.plan ? state.plan.timezone : null;
    var rows = [];
    if (before) {
      rows.push({
        label: "BEFORE (" + text(before.baseline_kind) + ")",
        duration_sec: before.duration_sec,
        distance_m: before.distance_m,
        feasible: before.feasible,
        finish_arrival: before.finish_arrival
      });
    }
    rows.push({
      label: "AFTER (RoutePilot order)",
      duration_sec: after.duration_sec,
      distance_m: after.distance_m,
      feasible: after.feasible,
      finish_arrival: after.finish_arrival
    });
    if (algorithm) {
      rows.push({
        label: "Algorithm baseline (internal reference, not BEFORE)",
        duration_sec: algorithm.duration_sec,
        distance_m: algorithm.distance_m,
        feasible: algorithm.feasible,
        finish_arrival: algorithm.finish_arrival
      });
    }
    rows.push({
      __className: "recommended-row",
      label: "Saved (as reported by the API)",
      duration_sec: metrics.saved_duration_sec,
      distance_m: metrics.saved_distance_m,
      feasible: null,
      finish_arrival: null
    });

    replace(byId("before-after"), [
      table("BEFORE vs AFTER (every figure is the API's own metric)",
        [
          ["Route", "label"],
          ["Duration", "duration_sec", { render: duration, numeric: true }],
          ["Distance", "distance_m", { render: distance, numeric: true }],
          ["Feasible", "feasible", { render: function (value) {
            return value === null || value === undefined ? "" : badgeFor(value);
          } }],
          ["FINISH arrival", "finish_arrival", { render: function (value) {
            return value ? instant(value, timezoneName) : "";
          } }]
        ],
        rows)
    ]);
  }

  // ------------------------------------------------------------- history --
  function loadRuns() {
    if (!state.planId) {
      return Promise.resolve();
    }
    return request(API.runs(state.planId)).then(function (document_) {
      var runs = document_.data || [];
      var panel = byId("history-panel");
      var children = [
        definitionList([
          ["Runs stored for this plan", text(document_.count)],
          ["Read-only", text(document_.read_only)],
          ["Note (from the API)", text(document_.note)]
        ])
      ];
      if (!runs.length) {
        children.push(paragraph("muted", "No run has been recorded for this plan yet. Only " +
          "POST /api/plans/{id}/optimize appends one, and that recalculation control is U16."));
      } else {
        children.push(table("Stored runs, oldest first",
          [
            ["Run id", "id"],
            ["Kind", "run_kind"],
            ["Status", "status"],
            ["Created at (UTC)", "created_at"],
            ["Algorithm", "algorithm"],
            ["Route fingerprint", "route_fingerprint"],
            ["Inputs fingerprint", "inputs_fingerprint"],
            ["Recorded recommended stop", "recommended_stop_id"]
          ],
          runs.map(function (run) {
            return {
              id: run.id,
              run_kind: run.run_kind,
              status: run.status,
              created_at: run.created_at,
              algorithm: text(run.algorithm) + " " + text(run.algorithm_version),
              route_fingerprint: (run.fingerprints || {}).route_fingerprint,
              inputs_fingerprint: (run.fingerprints || {}).inputs_fingerprint,
              recommended_stop_id: (run.recommendation || {}).recommended_stop_id
            };
          })));
      }
      replace(panel, children);
    });
  }

  // -------------------------------------------------------------- actions --
  function requestRecommendation() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return;
    }
    setLoading(true);
    setStatus("Requesting the recommendation for " + state.planId + " \u2026");
    request(API.recommendation(state.planId)).then(function (document_) {
      state.recommendation = document_;
      renderRecommendation(document_);
      var seconds = (document_.data || {}).computation_seconds;
      setLoading(false);
      setStatus(
        "Recommendation recomputed live for " + state.planId + " in " + text(seconds) +
          " s (measured computation_seconds). It is advisory: nothing was applied and the plan is " +
          "unchanged.",
        "ok"
      );
    }).catch(function (error) {
      setLoading(false);
      showError(error);
    });
  }

  function requestRoute() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return;
    }
    setLoading(true);
    setStatus("Requesting the committed route for " + state.planId + " \u2026");
    request(API.route(state.planId)).then(function (document_) {
      state.route = document_.data;
      renderRoute(document_);
      var seconds = (document_.data || {}).computation_seconds;
      setLoading(false);
      setStatus(
        "Committed route computed for " + state.planId + " in " + text(seconds) +
          " s (measured computation_seconds). The order, timeline and metrics are the engine's " +
          "own.",
        "ok"
      );
      return loadRuns();
    }).catch(function (error) {
      setLoading(false);
      showError(error);
      if (error && error.code === "no_first_stop_selected") {
        replace(byId("route-panel"), [
          paragraph("error-text", "The API refused the route with " + text(error.code) + ": " +
            text(error.message)),
          paragraph("hint", "That refusal is the honest answer while the plan is " +
            "awaiting_first_stop_choice: there is no committed route to show, and no route is " +
            "invented. Choosing a first stop is the U16 override control.")
        ]);
      }
    });
  }

  function createOrOpenDemoPlan() {
    setStatus("Creating or opening the deterministic DEMO plan \u2026");
    createDemoPlan().then(function (plan) {
      return loadPlans().then(function () {
        return openPlan(plan.id);
      });
    }).then(function (plan) {
      setStatus("Opened the DEMO/SYNTHETIC plan " + plan.id + " (provenance " +
        text(plan.data_provenance) + ").", "ok");
      return loadRuns();
    }).catch(showError);
  }

  // ------------------------------------------------------------------ boot --
  function checkRequiredElements() {
    var missing = REQUIRED_IDS.filter(function (id) { return !byId(id); });
    if (missing.length) {
      setStatus("Workspace markup is incomplete; missing element id(s): " + missing.join(", "),
        "error");
    }
    return missing;
  }

  function boot() {
    checkRequiredElements();
    byId("create-demo-plan").addEventListener("click", createOrOpenDemoPlan);
    byId("plan-select").addEventListener("change", function (event) {
      openPlan(event.target.value).then(loadRuns).catch(showError);
    });
    byId("request-recommendation").addEventListener("click", requestRecommendation);
    byId("request-route").addEventListener("click", requestRoute);

    request(API.health).then(function (health) {
      state.health = health;
      renderHealth(health);
      setupMap(health);
      return loadPlans();
    }).then(function (planId) {
      if (planId) {
        return openPlan(planId).then(loadRuns);
      }
      setStatus(
        "No plan is stored yet. Press \u201cCreate / open the DEMO plan\u201d to create the " +
          "deterministic DEMO/SYNTHETIC fixture through POST /api/plans.",
        "ok"
      );
      renderRecommendationEmpty();
      renderRouteEmpty();
      renderSummaryEmpty();
      return null;
    }).catch(showError);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
