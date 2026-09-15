/*
  RoutePilot workspace - application script (spec sections 26, 31, 33, 34; D15/D16/D19/D21/D32/D36/
  D39).

  THE ONE RULE THIS FILE EXISTS TO KEEP
  =====================================

  NO BUSINESS FORMULA LIVES HERE. Every number, order, metric, feasibility result, violation,
  fingerprint, duration, saving and rank is rendered from an API payload UNCHANGED. This file may
  format a value for reading (seconds -> "00h 02m 15s", an ISO instant -> local wall-clock text, a
  metre count -> "12.3 km") and it may place payload values into a table in the order the payload
  gave them. It must never add, subtract, average, total, rank, re-order, re-score, infer feasibility
  or hash anything, and it must never invent a route, a candidate order, a saving or a fingerprint.

  PRESENTATION (this unit)
  ========================

  The page is a dashboard, not a debug console, and this file is where the presented structure lives:

  * the optimization card shows the recommended first stop, the estimated route figures (the
    committed route's own metrics, or the recommended candidate's complete-route metrics when no
    route is committed yet) and the Save figures the API reports, then one primary call to action;
  * the KPI strip renders BEFORE / AFTER / SAVED from the API metrics - no figure is computed here;
  * the route card renders a compact preview of the timeline with a "Show all N stops" toggle. The
    rows live in the DOM in full and CSS hides the overflow, so nothing is dropped;
  * the lower concern-per-card sections (timeline note, run history, override controls, technical
    details) are collapsed `<details>` disclosures, and the technical prose (endpoints, decision ids,
    engine internals, why a read is read-only) lives inside the collapsed technical section;
  * an API failure is surfaced as a product-shaped headline plus a collapsed "Technical details"
    disclosure that still carries the API's own code, type, HTTP status and message verbatim.

  WHAT THIS FILE DELIBERATELY DOES NOT DO
  =======================================

  Endpoint -> method -> body live in the single `ENDPOINTS` table below, so a control cannot drift
  from its documented contract. The override controls are unchanged: a recommendation is never
  applied by this page (the accept control only ever sends the stop the API's own recommendation
  payload named), every action re-reads the affected panels FROM THE SERVER (D39(d)), the run history
  is READ-ONLY, and there is no drag/reorder control (D21), no active-leg behaviour (D24) and no
  route-mode control of any kind (SMART_ROUTE is the only implemented mode; the plan's own
  `route_mode` is displayed from the payload, D19).
*/

(function () {
  "use strict";

  // ------------------------------------------------------------ endpoints --
  /*
    Every call this page makes, in one table: the URL builder, the documented HTTP method and the
    request body. `body` is a function of the caller's arguments, so a control physically cannot
    send a body the API does not document.
  */
  var ENDPOINTS = {
    health: {
      method: "GET",
      url: function () { return "/api/health"; },
      body: function () { return null; }
    },
    plans: {
      method: "GET",
      url: function () { return "/api/plans"; },
      body: function () { return null; }
    },
    createDemoPlan: {
      method: "POST",
      url: function () { return "/api/plans"; },
      body: function () { return {}; }
    },
    plan: {
      method: "GET",
      url: function (planId) { return "/api/plans/" + encodeURIComponent(planId); },
      body: function () { return null; }
    },
    updateStop: {
      method: "PUT",
      url: function (planId) { return "/api/plans/" + encodeURIComponent(planId); },
      body: function (planId, stopId, changes) {
        var entry = { stop_id: stopId };
        if (changes && changes.enabled !== undefined) { entry.enabled = changes.enabled; }
        if (changes && changes.priority !== undefined) { entry.priority = changes.priority; }
        return { stops: [entry] };
      }
    },
    recommendation: {
      method: "GET",
      url: function (planId) {
        return "/api/plans/" + encodeURIComponent(planId) + "/recommendation";
      },
      body: function () { return null; }
    },
    selection: {
      method: "POST",
      url: function (planId) {
        return "/api/plans/" + encodeURIComponent(planId) + "/selection";
      },
      body: function (planId, mode, stopId) { return { mode: mode, stop_id: stopId }; }
    },
    clearSelection: {
      method: "DELETE",
      url: function (planId) {
        return "/api/plans/" + encodeURIComponent(planId) + "/selection";
      },
      body: function () { return null; }
    },
    route: {
      method: "GET",
      url: function (planId) {
        return "/api/plans/" + encodeURIComponent(planId) + "/route";
      },
      body: function () { return null; }
    },
    optimize: {
      method: "POST",
      url: function (planId) {
        return "/api/plans/" + encodeURIComponent(planId) + "/optimize";
      },
      body: function () { return null; }
    },
    runs: {
      method: "GET",
      url: function (planId) {
        return "/api/plans/" + encodeURIComponent(planId) + "/runs";
      },
      body: function () { return null; }
    },
    run: {
      method: "GET",
      url: function (runId) { return "/api/runs/" + encodeURIComponent(runId); },
      body: function () { return null; }
    }
  };

  //: The two documented first-stop request modes (D4/D6). Keeping them here means this file can
  //: express the driver's decision and nothing else.
  var FIRST_STOP_MODES = {
    recommend: "recommend",
    manual: "manual"
  };

  //: How many timeline/stop rows the compact preview shows before the reader opens it. Purely
  //: presentational: the DOM always carries every row and CSS does the hiding.
  var PREVIEW_ROWS = 10;

  //: The neutral placeholder for a figure a payload does not report. It is never a computed value.
  var NOT_REPORTED = "-";

  var state = {
    health: null,
    planList: [],
    plan: null,
    planId: null,
    recommendation: null,
    route: null,
    runs: null,
    runDetail: null,
    stopLabels: {},
    //: True while a synchronous engine computation is in flight, so a render that knows the
    //: recommendation cannot re-enable the accept control behind `setLoading`'s back.
    computing: false,
    //: True when the plan changed after the recommendation in hand was computed: the payload stays
    //: visible (it is what the driver saw) but is labelled stale and can no longer be accepted.
    recommendationIsStale: false
  };

  var REQUIRED_IDS = [
    "status-banner", "latency-notice", "plan-select", "create-demo-plan",
    "get-recommendation", "accept-recommendation", "manual-stop-select", "choose-first-stop",
    "cancel-selection", "stop-list", "recalculate", "error-banner", "run-history", "run-detail",
    "recommendation-panel", "advisory-banner", "recommended-stop", "alternatives",
    "rejected-candidates", "selection-panel", "first-stop-state", "route-panel", "timeline",
    "summary-panel", "before-after", "map", "map-notice", "history-panel", "loading"
  ];

  //: The controls that must be disabled while a synchronous engine computation is in flight. The
  //: recommendation and the recalculation both go through `withComputation`, so these are the
  //: controls that could start a second multi-second request.
  var COMPUTING_CONTROLS = [
    "get-recommendation", "accept-recommendation", "cancel-selection", "choose-first-stop",
    "recalculate", "request-route", "create-demo-plan", "manual-stop-select", "plan-select"
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

  // ------------------------------------------------- presentation helpers --
  /**
   * A compact figure pair from two payload values: `10h 49m` and `93.3 km`. Each value is the
   * payload's own number formatted for reading; a value the payload does not carry renders the
   * neutral placeholder rather than a figure derived here.
   */
  function figurePair(durationSeconds, distanceMetres) {
    var wrapper = el("span", "figure-values");
    wrapper.appendChild(el("span", "figure-duration", duration(durationSeconds)));
    wrapper.appendChild(el("span", "figure-separator", "\u00b7"));
    wrapper.appendChild(el("span", "figure-distance", distance(distanceMetres)));
    return wrapper;
  }

  /** One KPI card: a small label over the figure(s) the API reported for it. */
  function kpiCard(label, valueNodes, tone) {
    var card = el("div", "kpi" + (tone ? " kpi-" + tone : ""));
    card.appendChild(el("span", "kpi-label", label));
    var body = el("span", "kpi-values");
    (valueNodes || []).forEach(function (node) { body.appendChild(node); });
    card.appendChild(body);
    return card;
  }

  /** The BEFORE / AFTER / SAVED strip: one card per figure pair, all values from the payload. */
  function kpiRow(label, durationSeconds, distanceMetres, feasible) {
    var values = [
      el("span", "kpi-duration", duration(durationSeconds)),
      el("span", "kpi-distance", distance(distanceMetres))
    ];
    if (feasible !== undefined && feasible !== null) {
      values.push(badgeFor(feasible));
    }
    return kpiCard(label, values);
  }

  /**
   * Toggle one preview container open and closed, updating the control's own label. The open state
   * is one attribute on the container (`data-preview="open"`), which CSS uses to reveal the rows the
   * compact preview hides - the rows themselves are always in the DOM.
   */
  function togglePreview(containerId, buttonId) {
    var container = byId(containerId);
    var button = byId(buttonId);
    if (!container || !button) {
      return;
    }
    var open = container.getAttribute("data-preview") === "open";
    container.setAttribute("data-preview", open ? "closed" : "open");
    button.textContent = open ? previewLabel(container) : "Show fewer";
    button.setAttribute("aria-expanded", open ? "false" : "true");
  }

  /** The control label for a collapsed preview: "Show all N stops", with the payload's own count. */
  function previewLabel(container) {
    var rows = totalPreviewRows(container);
    var noun = container && container.className.indexOf("stop-list") >= 0 ? "stops" : "timeline rows";
    return "Show all " + rows + " " + noun;
  }

  /**
   * How many rows a preview container really holds. The rows are counted in the rendered DOM (the
   * tables and the stop rows built by the renderers), never derived from a business figure.
   */
  function totalPreviewRows(container) {
    if (!container) {
      return 0;
    }
    var tables = container.getElementsByTagName("table");
    var count = 0;
    var index = 0;
    while (index < tables.length) {
      var body = tables[index].getElementsByTagName("tbody")[0];
      count = count + (body ? body.children.length : 0);
      index = index + 1;
    }
    count = count + container.getElementsByClassName("stop-row").length;
    return count;
  }

  /**
   * Wire a preview container to its toggle: the control appears only when there is something to
   * reveal, and the rows themselves always stay in the DOM (CSS hides the overflow).
   */
  function showPreviewControl(containerId, buttonId) {
    var container = byId(containerId);
    var button = byId(buttonId);
    if (!container || !button) {
      return;
    }
    container.setAttribute("data-preview", "closed");
    var total = totalPreviewRows(container);
    if (total <= PREVIEW_ROWS) {
      button.hidden = true;
      button.textContent = "";
      return;
    }
    button.hidden = false;
    button.textContent = previewLabel(container);
    button.setAttribute("aria-expanded", "false");
  }

  // ---------------------------------------------------------- formatting --
  /** A seconds count as readable text. FORMATTING ONLY: the number itself is the payload's. */
  function duration(seconds) {
    if (seconds === null || seconds === undefined) {
      return NOT_REPORTED;
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
    return sign + pad(hours) + "h " + pad(minutes) + "m " + pad(secs) + "s";
  }

  /** Two-digit display padding. FORMATTING ONLY: it changes no payload value. */
  function pad(value) {
    return value < 10 ? "0" + String(value) : String(value);
  }

  /** A metre count as readable text. FORMATTING ONLY. */
  function distance(metres) {
    if (metres === null || metres === undefined) {
      return NOT_REPORTED;
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
      return NOT_REPORTED;
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

  /** A `{code: guidance}` lookup with no arithmetic and no derivation: a label for a known code. */
  function lookup(map, key) {
    if (key === null || key === undefined) {
      return null;
    }
    var value = map[String(key)];
    return value === undefined ? null : value;
  }

  /** The display label for one stop: the payload's own text, with the id as the last resort. */
  function stopLabel(stop) {
    return text(stop.normalized_address || stop.raw_address || stop.id);
  }

  // ---------------------------------------------------------------- http --
  function ApiError(message, status, code, type, guidance) {
    this.name = "ApiError";
    this.message = message;
    this.status = status;
    this.code = code;
    this.type = type;
    this.guidance = guidance || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  /*
    The documented API error envelopes this page knows how to add guidance for. The code, the type
    and the message always come from the API's own envelope (`{"error": {code, type, message}}`);
    the guidance line is this page's own text and is labelled as such. Several undocumented-in-this-
    page codes (404/405/503/500) are still surfaced verbatim without any guidance at all.
  */
  var ERROR_GUIDANCE = {
    no_first_stop_selected:
      "Refusal code 409 no_first_stop_selected: the plan is awaiting_first_stop_choice, so there is " +
      "no committed route and this page shows none. Choose a first stop (accept the recommendation " +
      "or use the manual picker) and try again - no route is invented in the meantime.",
    plan_busy:
      "Refusal code 409 plan_busy: this plan's synchronous single-flight computation lock did not " +
      "become free inside the documented bound. Retry guidance: wait for the in-flight computation " +
      "to finish and press the control again; this API has no background job queue, so waiting and " +
      "retrying is the documented behaviour and no partial or fabricated result is returned.",
    invalid_input:
      "Refusal code 422 invalid_input: the request was understood and violates a domain rule (for " +
      "example a disabled stop chosen as the first stop, or a malformed stop update). The API's own " +
      "message above is the reason; this page changed nothing.",
    unsupported_capability:
      "Refusal code 501 unsupported_capability: the request asks for a capability this build does " +
      "not implement (this build implements the SMART_ROUTE route mode only, D16/D19). Nothing is " +
      "faked and no fallback to SMART_ROUTE is applied silently.",
    illegal_state:
      "Refusal code 409 illegal_state: the requested change is not legal in the plan's current " +
      "state (for example cancelling when nothing is selected). The plan is unchanged.",
    unknown_plan:
      "Refusal code 404 unknown_plan: no stored plan has that id. Re-read the plan list and open a " +
      "plan again.",
    unknown_stop:
      "Refusal code 404 unknown_stop: the plan has no stop with that id. Re-read the plan and " +
      "choose from the stops the API reports.",
    unknown_run:
      "Refusal code 404 unknown_run: no stored optimization run has that id. Re-read the run " +
      "history and open a run from the list.",
    unknown_setting:
      "Refusal code 404 unknown_setting: no value is stored for that settings key.",
    invalid_body:
      "Refusal code 400 invalid_body: the request body was not a valid JSON object. This page sends " +
      "only the documented bodies; the API's own message above says what arrived.",
    method_not_allowed:
      "Refusal code 405 method_not_allowed: the path exists but does not accept this HTTP method.",
    unknown_path:
      "Refusal code 404 unknown_path: the requested path is not part of this API.",
    timezone_data_unavailable:
      "Refusal code 503 timezone_data_unavailable: no IANA time zone database is reachable, so local " +
      "wall-clock times cannot be resolved. See the API's install command above; the strict IANA / " +
      "DST model is not weakened for this.",
    storage_error:
      "Refusal code 500 storage_error: stored state is unreadable or the database refused the " +
      "operation. Nothing was changed by this page.",
    internal_error:
      "Refusal code 500 internal_error: the request failed for a reason the API does not classify. " +
      "The API's own message above is the whole explanation available."
  };

  function request(endpoint, args) {
    var options = { method: endpoint.method };
    var body = endpoint.body.apply(null, args || []);
    if (body !== null && body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    return fetch(endpoint.url.apply(null, args || []), options).then(function (response) {
      return response.text().then(function (raw) {
        var payload = null;
        try {
          payload = JSON.parse(raw);
        } catch (error) {
          payload = null;
        }
        if (!response.ok) {
          var detail = payload && payload.error ? payload.error : null;
          var code = detail ? detail.code : "unknown";
          throw new ApiError(
            detail ? detail.message : "HTTP " + response.status + " from " + endpoint.url(),
            response.status,
            code,
            detail ? detail.type : "unknown",
            lookup(ERROR_GUIDANCE, code)
          );
        }
        if (payload === null) {
          throw new ApiError(
            "the API answered " + endpoint.url() + " with a body that is not JSON",
            0,
            "invalid_body",
            "NotJSON",
            null
          );
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

  /**
   * Surface an API failure as a product-shaped message: a concise headline for the reader plus a
   * collapsed "Technical details" disclosure that still carries the API's own code, type, HTTP
   * status and message verbatim. Nothing is swallowed, retried silently or replaced with a guess.
   */
  function showError(error, context) {
    var banner = byId("error-banner");
    var message = error && error.message ? error.message : String(error);
    var code = error && error.code ? String(error.code) : "unknown";
    var type = error && error.type ? String(error.type) : "unknown";
    var status = error && error.status ? String(error.status) : "no HTTP status";
    var headline = error && error.status === 409 && code === "no_first_stop_selected"
      ? "No committed route to show yet"
      : "Unable to load route data" + (context ? " (" + context + ")" : "");
    var details = el("details", "disclosure disclosure-error");
    details.appendChild(el("summary", null, "Technical details"));
    var body = el("div", "error-body");
    body.appendChild(paragraph(null, "The API said: " + message));
    body.appendChild(definitionList([
      ["Code", code],
      ["Type", type],
      ["HTTP status", status]
    ]));
    if (error && error.guidance) {
      body.appendChild(paragraph("hint", "This page's own note (not the API's message): " +
        error.guidance));
    }
    details.appendChild(body);
    replace(banner, [
      el("strong", null, headline),
      details
    ]);
    banner.hidden = false;
  }

  /** Clear the error banner: called when an action succeeds, so it never shows a stale refusal. */
  function clearError() {
    var banner = byId("error-banner");
    banner.textContent = "";
    banner.hidden = true;
  }

  /** The computing/loading state of D39(e): shown while a computation is in flight. */
  function setLoading(isLoading, note) {
    var loading = byId("loading");
    var extra = note ? " " + note : "";
    state.computing = !!isLoading;
    loading.textContent = isLoading
      ? "Computing\u2026 the API is recomputing this request synchronously. This can take several " +
        "seconds at the ~50-stop scale." + extra
      : "";
    loading.hidden = !isLoading;
    COMPUTING_CONTROLS.forEach(function (id) {
      var control = byId(id);
      if (control) {
        control.disabled = !!isLoading;
      }
    });
    // The accept control has its own condition (a recommendation must name a stop), so it is
    // re-evaluated here rather than blindly re-enabled.
    updateAcceptControl();
  }

  /**
   * Run one request that can take seconds (the recommendation and the recalculation), with the
   * `#loading` computing state shown for the whole flight and the measured `computation_seconds`
   * reported afterwards. The state is always cleared, including on failure.
   *
   * The computing state also covers the follow-up work `report` starts, and is cleared only once
   * that whole chain has settled: a recalculation re-reads the route (`GET /api/plans/{id}/route`,
   * measured in seconds at the ~50-stop scale) and the run history, and leaving the computing state
   * early would claim the page is idle and re-enable the plan chooser while those reads are still in
   * flight (D39(d)/D39(e), D26/D32).
   */
  function withComputation(label, endpoint, args, report) {
    setLoading(true, label ? label + "\u2026" : null);
    setStatus(label ? label + " \u2026" : "Working \u2026");
    return request(endpoint, args).then(function (document_) {
      // The report runs first and its follow-up chain is awaited, so `setLoading(false)` is reached
      // only after everything the action re-reads from the server has come back.
      return Promise.resolve(report(document_)).then(function (result) {
        setLoading(false);
        return result;
      });
    }).catch(function (error) {
      setLoading(false);
      throw error;
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
  /**
   * The compact status line and the technical provenance block.
   *
   * The status line stays short (it is a status, not a paragraph); the API's own prose - the data
   * provenance warning, the timezone source and the accepted latency wording - is printed in full in
   * the collapsed technical section and the latency notice, so nothing is hidden by the shortening.
   */
  function renderHealth(health) {
    var data = health || {};
    var demo = data.demo_data || {};
    var computation = data.computation || {};
    var timezoneData = data.timezone_data || {};

    setStatus(
      "Workspace ready \u2014 " + text(data.data_provenance || "DEMO/SYNTHETIC") +
        " data; time zone data: " + text(timezoneData.source || "unknown") + ".",
      "ok"
    );

    // The API's own provenance warning, printed verbatim into #provenance-note (the container in the
    // collapsed technical section) rather than restated by the page.
    var provenance = byId("provenance-note");
    if (provenance) {
      replace(provenance, [
        paragraph(null, text(demo.warning) + "."),
        paragraph("hint", "Time zone data: " + text(timezoneData.source) +
          " (IANA " + text(timezoneData.iana_version || "unknown") + "). " +
          text(timezoneData.install_command || ""))
      ]);
    }

    // The latency notice states the accepted MVP worst case that the API itself reports.
    var latency = byId("latency-notice");
    replace(latency, [
      el("strong", null, "Latency: "),
      document.createTextNode(
        text(computation.accepted_mvp_latency) +
          " Every computation response carries its measured computation_seconds, which is shown " +
          "after each request. Synchronous: " + text(computation.synchronous) +
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
    // Leaflet's stylesheet is NOT decoration: the library positions its panes and its tile images
    // absolutely, and without those rules a 256x256 tile falls back to normal document flow and
    // escapes the map container into the rest of the page (confirmed in a real browser; Stage 4
    // hotfix). The URL comes from the API like every other vendor URL; when only the script URL is
    // configured, the sibling leaflet.css of the same distribution is derived from it. The <link>
    // is appended BEFORE the script so the rules are already in place when Leaflet initializes.
    var stylesheetUrl = values.map_library_css_url || deriveStylesheetUrl(libraryUrl);
    if (libraryUrl && stylesheetUrl) {
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

  /**
   * The sibling stylesheet of a configured Leaflet script URL - an asset location, never a business
   * value, and never a hardcoded vendor URL: `.../leaflet.js`, `.../leaflet.min.js` and
   * `.../leaflet-src.js` all resolve to `leaflet.css` in the same directory (Leaflet ships exactly
   * one stylesheet name). Anything that is not a Leaflet script URL returns null rather than a
   * guess, and the configured `map_library_css_url` always wins over this derivation.
   */
  function deriveStylesheetUrl(libraryUrl) {
    if (typeof libraryUrl !== "string" || !libraryUrl) {
      return null;
    }
    var match = /^(.*\/)?leaflet(?:-src|\.min)?\.js(\?.*)?$/i.exec(libraryUrl);
    if (!match) {
      return null;
    }
    return (match[1] || "") + "leaflet.css" + (match[2] || "");
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

  /**
   * Redraw the map from the payloads currently in hand (never from a client-side computation).
   *
   * The map is presentation only: it draws START, FINISH and the stops, and it presents the route
   * order as a 1-based number badge on each stop marker. No line is drawn between the stops (real
   * road routing is not implemented). The recommended first stop (the recommendation payload's own
   * `recommended_stop_id`) and the driver's own selection (`plan.first_stop.selected_stop_id`) are
   * passed through unchanged so the map can style them differently - the recommendation stays
   * advisory and is never applied by this page.
   */
  function drawMap() {
    if (!state.plan) {
      return;
    }
    var recommendation = (state.recommendation && state.recommendation.data) || null;
    var firstStop = state.plan.first_stop || {};
    RoutePilotMap.drawRoute({
      plan: state.plan,
      route: state.route,
      stopLabels: state.stopLabels,
      recommendedStopId: recommendation ? recommendation.recommended_stop_id : null,
      selectedStopId: firstStop.selected_stop_id || null
    });
  }

  // --------------------------------------------------------------- plans --
  function loadPlans() {
    return request(ENDPOINTS.plans).then(function (document_) {
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
      select.value = state.planId || state.planList[0].id;
      return state.planId || state.planList[0].id;
    });
  }

  /** `POST /api/plans` - the documented "create or open the deterministic DEMO plan" step. */
  function createDemoPlan() {
    return request(ENDPOINTS.createDemoPlan).then(function (document_) {
      return document_.data;
    });
  }

  function openPlan(planId) {
    return request(ENDPOINTS.plan, [planId]).then(function (document_) {
      var plan = document_.data;
      state.plan = plan;
      state.planId = plan.id;
      state.recommendation = null;
      state.recommendationIsStale = false;
      state.route = null;
      state.runDetail = null;
      state.stopLabels = {};
      (plan.stops || []).forEach(function (stop) {
        state.stopLabels[text(stop.id)] = stopLabel(stop);
      });
      var select = byId("plan-select");
      if (select && !select.value) {
        select.value = plan.id;
      }
      renderPlan(plan, document_);
      refreshManualStopChoices(plan);
      renderRecommendationUnavailable(
        "No recommendation has been read for this plan yet. Press \u201cGet recommendation\u201d: " +
          "the API recomputes it live for that request and writes nothing."
      );
      renderRouteEmpty();
      renderSummaryEmpty();
      renderRunDetailEmpty();
      drawMap();
      // The plan loaded successfully, so any earlier refusal shown in the banner is stale. This is
      // the load path boot's auto-open uses, so a healthy workspace can never keep an old error.
      clearError();
      return plan;
    });
  }

  /**
   * Re-read the plan from the server and refresh every panel that reports the driver's decision.
   *
   * This is the "never optimistic" step of D39(d): after a selection change, a stop edit or a
   * recalculation the plan summary, the first-stop state, the route and the run history are all
   * rebuilt from server responses, never from a local edit.
   */
  function refreshFromServer(planId) {
    var identifier = planId || state.planId;
    if (!identifier) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve(null);
    }
    return request(ENDPOINTS.plan, [identifier]).then(function (document_) {
      var plan = document_.data;
      state.plan = plan;
      state.planId = plan.id;
      state.stopLabels = {};
      (plan.stops || []).forEach(function (stop) {
        state.stopLabels[text(stop.id)] = stopLabel(stop);
      });
      renderPlan(plan, document_);
      // D39(d): the first-stop state is part of what every selection change must re-read. It is
      // rebuilt here, on the refresh path itself, so accept, manual choose and cancel/unpin all
      // re-render it from the server response - not only the recommendation render paths, which a
      // manual change (and a cancel, which never reaches them) would otherwise leave stale.
      renderFirstStopState();
      refreshManualStopChoices(plan);
      return plan;
    });
  }

  /**
   * The plan's own state, rendered into the collapsed technical section. The first-stop state and
   * its provenance are the DRIVER's decision, never the engine's recommendation.
   */
  function renderPlan(plan, document_) {
    var firstStop = plan.first_stop || {};
    var counts = plan.counts || {};
    var summary = byId("plan-summary");
    replace(summary, [
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
      el("h3", null, "First-stop state and its provenance (the driver's decision, from the API)"),
      firstStopBlock(firstStop)
    ]);
    renderStopList(plan);
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

  /** The manual first-stop picker: only the stops the API reports as enabled are offered. */
  function refreshManualStopChoices(plan) {
    var select = byId("manual-stop-select");
    if (!select) {
      return;
    }
    replace(select, []);
    var enabled = (plan.stops || []).filter(function (stop) {
      return stop.enabled === true;
    });
    if (!enabled.length) {
      select.appendChild(el("option", null, "no enabled stop in this plan"));
      return;
    }
    enabled.forEach(function (stop) {
      var option = el("option", null, text(stop.id) + " \u2014 " + stopLabel(stop));
      option.value = stop.id;
      select.appendChild(option);
    });
  }

  /**
   * The per-stop controls of D39(d): disable, restore and priority, each one `PUT /api/plans/{id}`
   * carrying exactly one change. Deliberately absent: any drag/reorder control (D21/D39(d)) and any
   * control that could change the first-stop choice through a stop attribute.
   */
  function renderStopList(plan) {
    var container = byId("stop-list");
    clear(container);
    (plan.stops || []).forEach(function (stop) {
      container.appendChild(stopRow(stop));
    });
    if (!container.firstChild) {
      container.appendChild(paragraph("muted", "This plan holds no stop."));
    }
    showPreviewControl("stop-list", "stop-list-toggle");
  }

  function stopRow(stop) {
    var row = el("div", "stop-row" + (stop.enabled ? "" : " stop-row-disabled"));
    var head = el("div", "stop-row-head");
    head.appendChild(el("span", "stop-row-id", text(stop.id)));
    head.appendChild(el("span", "stop-row-label", stopLabel(stop)));
    head.appendChild(badgeFor(stop.enabled));
    head.appendChild(el("span", null, "priority " + text(stop.priority)));
    row.appendChild(head);

    var controls = el("div", "stop-row-controls");
    if (stop.enabled) {
      var disable = el("button", null, "Disable this stop");
      disable.type = "button";
      disable.addEventListener("click", function () {
        updateStop(stop.id, { enabled: false });
      });
      controls.appendChild(disable);
    } else {
      var restore = el("button", null, "Restore this stop");
      restore.type = "button";
      restore.addEventListener("click", function () {
        updateStop(stop.id, { enabled: true });
      });
      controls.appendChild(restore);
    }

    var priorityLabel = el("label", null, "New priority");
    priorityLabel.setAttribute("for", "priority-" + text(stop.id));
    controls.appendChild(priorityLabel);
    var input = el("input", "priority-input");
    input.type = "number";
    input.id = "priority-" + text(stop.id);
    input.value = text(stop.priority);
    input.setAttribute("aria-label", "New priority for stop " + text(stop.id));
    controls.appendChild(input);
    var apply = el("button", null, "Change priority");
    apply.type = "button";
    apply.addEventListener("click", function () {
      updateStop(stop.id, { priority: Number(input.value) });
    });
    controls.appendChild(apply);
    row.appendChild(controls);
    return row;
  }

  // ------------------------------------------------------ recommendation --
  /**
   * The recommendation panel's "nothing current" state. A recommendation is derived and
   * recomputable: after the plan changed (a selection, a stop edit or a recalculation) the previous
   * payload is no longer this plan's current inputs, so it is marked stale (or cleared when a new
   * plan is opened) instead of being presented as if it still applied.
   */
  function renderRecommendationUnavailable(message) {
    replace(byId("recommendation-panel"), [paragraph("muted", message)]);
    byId("recommended-stop").textContent = "";
    resetFigures();
    setDecisionCardState(false);
    replace(byId("alternatives"), [
      paragraph("muted", "The ranked alternatives appear here with their complete-route metrics.")
    ]);
    replace(byId("rejected-candidates"), [
      paragraph("muted", "The rejected candidates and their violating stops appear here.")
    ]);
    renderSelectionPanel(null);
    renderFirstStopState();
    updateAcceptControl();
  }

  /**
   * The "Estimated route" / "Save" figure pairs of the optimization card.
   *
   * FIGURES COME FROM THE API ONLY. When a committed route exists, the pair is that route's own
   * `metrics.after` duration and distance and the saving is `saved_duration_sec` /
   * `saved_distance_m` as recorded. When no route is committed yet, the pair is the recommended
   * candidate's own complete-route metrics from the recommendation payload. A value a payload does
   * not carry renders the neutral placeholder - never a number derived here.
   */
  function renderFigures() {
    var after = routeAfterMetrics() ||
      ((recommendedCandidate() || {}).complete_route || null);
    var metrics = (state.route && state.route.metrics) || {};
    var savedMetrics = state.route ? metrics : {};
    setFigurePair("estimated-route-figures", after ? after.duration_sec : null,
      after ? after.distance_m : null);
    setFigurePair("saved-figures", savedMetrics.saved_duration_sec, savedMetrics.saved_distance_m);
  }

  function setFigurePair(id, durationSeconds, distanceMetres) {
    var container = byId(id);
    if (!container) {
      return;
    }
    replace(container, [figurePair(durationSeconds, distanceMetres)]);
  }

  function resetFigures() {
    setFigurePair("estimated-route-figures", null, null);
    setFigurePair("saved-figures", null, null);
  }

  /** The committed route's own after-route metrics, or null when no route payload is in hand. */
  function routeAfterMetrics() {
    var metrics = (state.route && state.route.metrics) || null;
    return metrics ? (metrics.after || null) : null;
  }

  /** The candidate the API's own recommendation payload named as the recommendation, or null. */
  function recommendedCandidate() {
    var data = (state.recommendation && state.recommendation.data) || null;
    if (!data || !data.recommended_stop_id) {
      return null;
    }
    var ranked = data.ranked || [];
    var index = 0;
    while (index < ranked.length) {
      if (ranked[index].stop_id === data.recommended_stop_id) {
        return ranked[index];
      }
      index = index + 1;
    }
    return null;
  }

  /**
   * Mark the recommendation in hand as stale: the plan changed after it was computed, so it is no
   * longer this plan's current inputs. The payload that was displayed stays visible (it is what the
   * driver acted on) but it is never presented as current, and accepting it is no longer offered.
   */
  function markRecommendationStale(reason) {
    state.recommendationIsStale = true;
    // A stale recommendation can no longer be accepted, so the card drops back to the default
    // emphasis (Get recommendation primary) whether or not a payload is still on screen.
    setDecisionCardState(false);
    if (!state.recommendation) {
      return;
    }
    renderRecommendation(state.recommendation);
    var panel = byId("recommendation-panel");
    panel.insertBefore(
      paragraph("error-text", "STALE: this recommendation was computed before " + text(reason) +
        ". It is no longer this plan's current inputs, it is NOT the plan's state, and it is not " +
        "applied. Press \u201cGet recommendation\u201d to recompute it live."),
      panel.firstChild
    );
  }

  /**
   * The selection panel: what the API says the driver's decision currently is, plus the controls'
   * own honesty note. It never states a recommendation as applied state (D32/I5).
   */
  function renderSelectionPanel(selection) {
    var children = [el("h3", null, "Selection (the driver's decision, from the API)")];
    children.push(paragraph(null, "The stored first-stop state of this plan reads: " +
      firstStopSentence()));
    if (selection) {
      children.push(paragraph(null, "The API's answer to this selection change says: " +
        text(selection.note)));
      children.push(definitionList([
        ["Endpoint state after the change", badge(text(selection.state), "mode")],
        ["Mode", text(selection.mode)],
        ["Selected first stop", selection.selected_stop_id || "none"],
        ["Selection source (provenance)", selection.selection_source || "none"],
        ["Pinned", text(selection.pinned)]
      ]));
    }
    children.push(paragraph("hint", "The controls change this state through the documented " +
      "endpoints and then re-read the plan from the server. A recommendation is never applied by " +
      "this page, and a stored run's recommendation is history, not this state."));
    replace(byId("selection-panel"), children);
  }

  /**
   * The first-stop decision state, with the distinction this product exists to keep unmistakable:
   * a stop the engine recommended (`Recommended by RoutePilot`) is never the same thing as a stop
   * the driver selected (`Selected by driver`), even when it is the same stop id.
   */
  function renderFirstStopState() {
    var firstStop = (state.plan && state.plan.first_stop) || {};
    var selected = firstStop.selected_stop_id || null;
    var provenance = text(firstStop.selection_source || "none");
    var children = [
      el("h3", null, "First-stop decision state (from the server)"),
      definitionList([
        ["Decision", selected
          ? badge("Selected by driver", "true")
          : badge("Awaiting the driver's choice", "mode")],
        ["Selected by driver", selected
          ? text(selected) + " (pinned " + text(firstStop.pinned) + ", source " + provenance + ")"
          : "none yet"],
        ["Recommended by RoutePilot", recommendedStopLabel()],
        ["Mode", text(firstStop.mode)],
        ["State", badge(text(firstStop.state), "mode")],
        ["Meaning", text(firstStop.description)]
      ]),
      paragraph("hint", selected
        ? "The stop above is the driver's own decision (provenance " + provenance + "). It became " +
          "plan state only because the driver selected it: RoutePilot's recommendation on its own " +
          "is advice and is never applied."
        : "No stop is selected yet. RoutePilot's recommendation is advice; the plan stays " +
          "awaiting_first_stop_choice until the driver decides, and no stop is substituted.")
    ];
    replace(byId("first-stop-state"), children);
  }

  /** The recommendation's own stop id, labelled as advice rather than as plan state. */
  function recommendedStopLabel() {
    var data = (state.recommendation && state.recommendation.data) || null;
    if (!data || !data.recommended_stop_id) {
      return "none read yet (advisory only)";
    }
    return text(data.recommended_stop_id) + " (advisory only, not applied)";
  }

  function firstStopSentence() {
    var firstStop = (state.plan && state.plan.first_stop) || {};
    return text(firstStop.state) + " (mode " + text(firstStop.mode) + ", selected " +
      text(firstStop.selected_stop_id || "none") + ", source " +
      text(firstStop.selection_source || "none") + ", pinned " + text(firstStop.pinned) + ")";
  }

  /**
   * The decision card's PRESENTATION state, driven by whether a recommendation is in hand and still
   * current. It is a state class only: it changes which button carries the primary emphasis (before
   * a recommendation, "Get recommendation" is the next action and the disabled accept control does
   * not compete with it; afterwards the accept control is primary), and it changes no action
   * semantics, no wiring and no disabled logic.
   */
  function setDecisionCardState(hasRecommendation) {
    var card = byId("decision-card");
    if (!card) {
      return;
    }
    card.classList.toggle("has-recommendation", !!hasRecommendation);
  }

  /** The accept control is only enabled when the API's own recommendation names a stop. */
  function updateAcceptControl() {
    var button = byId("accept-recommendation");
    if (!button) {
      return;
    }
    var data = (state.recommendation && state.recommendation.data) || null;
    var recommended = data ? data.recommended_stop_id : null;
    button.disabled = state.computing || state.recommendationIsStale || !recommended;
    button.setAttribute(
      "title",
      recommended && !state.recommendationIsStale
        ? "Send POST /api/plans/{id}/selection with mode=recommend and stop_id=" + text(recommended)
        : "Read a current recommendation first: accepting needs the stop the API recommended"
    );
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

    // Both branches build DOM nodes: `replace` appends its children, and `appendChild` refuses a
    // plain string in a real DOM, so a bare string here would throw and take the whole render with
    // it (the recommendation, the top-K/rejected tables, the selection panel and the first-stop
    // state are all built after this point).
    var recommended = byId("recommended-stop");
    if (data.recommended_stop_id) {
      replace(recommended, [
        document.createTextNode("Recommended first stop: "),
        el("strong", null, text(data.recommended_stop_id)),
        document.createTextNode(" \u2014 advisory only; the plan's first stop is unchanged until " +
          "the driver decides.")
      ]);
    } else {
      replace(recommended, [
        document.createTextNode("No fully feasible first stop: the engine's outcome is "),
        el("strong", null, text(data.status)),
        document.createTextNode(". This is a valid answer with the diagnostics below, not an error " +
          "and not a fabricated winner (v2 section 14).")
      ]);
    }

    var alternatives = (data.ranked || []);
    replace(byId("alternatives"), [
      paragraph("hint", "Top-K view: " + text(counts.ranked_returned) + " of " +
        text(counts.ranked) + " ranked candidates, in the engine's own rank order. Every metric " +
        "below is the complete route the engine evaluated. Choose one with the manual picker to " +
        "make it the driver's own choice."),
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

    renderSelectionPanel(null);
    renderFirstStopState();
    renderFigures();
    setDecisionCardState(!!data.recommended_stop_id && !state.recommendationIsStale);
    updateAcceptControl();
  }

  // --------------------------------------------------------------- route --
  function renderRouteEmpty() {
    replace(byId("route-panel"), [
      paragraph("muted", "No route calculated yet.")
    ]);
    replace(byId("timeline"), [
      paragraph("muted", "The route order and per-stop timeline appear here.")
    ]);
    showPreviewControl("timeline", "timeline-toggle");
    renderFigures();
  }

  /** The honest no-route state: the documented 409 refusal instead of an invented route (D9/I4). */
  function renderRouteRefused(error) {
    replace(byId("route-panel"), [
      paragraph("error-text", "No committed route: the API refused the route with " +
        text(error.code) + ": " + text(error.message)),
      paragraph("hint", "That refusal is the honest answer while the plan is " +
        "awaiting_first_stop_choice: there is no committed route to show, and no route is " +
        "invented. Choose a first stop with the controls and try again.")
    ]);
    replace(byId("timeline"), [
      paragraph("muted", "No timeline is shown, because there is no committed route.")
    ]);
    showPreviewControl("timeline", "timeline-toggle");
    renderSummaryEmpty();
    renderFigures();
    drawMap();
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
    var order = data.order || [];

    replace(byId("route-panel"), [
      definitionList([
        ["Route status", badge(text(data.status), data.status === "ok" ? "true" : "false")],
        ["Stops served", text(order.length)],
        ["Committed for the driver's selection", "mode " + text(selection.mode) + ", stop " +
          text(selection.selected_stop_id || "none") + ", source " +
          text(selection.selection_source || "none") + ", pinned " + text(selection.pinned)],
        ["Feasible (complete route, FINISH leg included)", badgeFor(after.feasible)],
        ["Order (as returned)", order.join(" \u2192 ") || "empty"],
        ["Live recompute", text(document_.live_recompute)],
        ["Measured computation_seconds", text(data.computation_seconds)],
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
      paragraph("legend map-legend-synthetic", "The map shows this exact order as a 1-based number " +
        "badge on each stop marker and draws no line between the stops - not road routing.")
    ]);
    showPreviewControl("timeline", "timeline-toggle");

    renderSummary(data);
    renderFigures();
    drawMap();
  }

  // ------------------------------------------------------------- summary --
  function renderSummaryEmpty() {
    replace(byId("summary-panel"), [
      paragraph("muted", "Run optimization to see BEFORE / AFTER / SAVED.")
    ]);
    replace(byId("before-after"), []);
  }

  /**
   * The KPI strip of D39(f): BEFORE, AFTER and SAVED, one card each, every value the API's own.
   *
   * BEFORE is `metrics.user_baseline` (the driver's own input order) when the API reports it, AFTER
   * is the committed `metrics.after` route, and SAVED is the recorded `saved_duration_sec` /
   * `saved_distance_m`. The internal algorithm baseline is deliberately not a KPI: it is an internal
   * reference and never the driver's BEFORE (it stays in the run history).
   */
  function renderSummary(data) {
    var metrics = data.metrics || {};
    var after = metrics.after || {};
    var before = metrics.user_baseline || null;

    replace(byId("summary-panel"), [
      paragraph("hint", "Every figure below is the API's own metric for this committed route" +
        (before ? "; BEFORE is the driver's own input order (" + text(before.baseline_kind) + ")."
          : "."))
    ]);

    var cards = [];
    if (before) {
      cards.push(kpiRow("BEFORE", before.duration_sec, before.distance_m, before.feasible));
    } else {
      cards.push(kpiRow("BEFORE", null, null, null));
    }
    cards.push(kpiRow("AFTER", after.duration_sec, after.distance_m, after.feasible));
    cards.push(kpiCard("SAVED", [
      el("span", "kpi-duration", duration(metrics.saved_duration_sec)),
      el("span", "kpi-distance", distance(metrics.saved_distance_m))
    ], "saved"));

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
    rows.push({
      __className: "recommended-row",
      label: "Saved (as reported by the API)",
      duration_sec: metrics.saved_duration_sec,
      distance_m: metrics.saved_distance_m,
      feasible: null,
      finish_arrival: null
    });

    cards.push(el("p", "hint kpi-note", "FINISH arrival (AFTER): " +
      instant(after.finish_arrival, timezoneName) + " \u00b7 violations reported: " +
      text((data.violations || []).length) + "."));

    replace(byId("before-after"), cards);
  }

  // ------------------------------------------------------------- history --
  /**
   * The run-history view: `GET /api/plans/{id}/runs`, oldest first, exactly as the API orders it.
   *
   * READ-ONLY. The only control on a row reads one stored run back through `GET /api/runs/{id}`;
   * nothing here offers to edit, reorder or delete a run, and no run's recorded recommendation is
   * ever presented as the plan's current first-stop decision. The list is a readable history row per
   * run (id, kind, status, when and the before/after/saved figures); the detailed payload fields live
   * inside the run detail below.
   */
  function loadRuns() {
    if (!state.planId) {
      return Promise.resolve();
    }
    return request(ENDPOINTS.runs, [state.planId]).then(function (document_) {
      state.runs = document_.data || [];
      renderRunHistory(document_);
      return state.runs;
    });
  }

  function renderRunHistory(document_) {
    var runs = document_.data || [];
    var children = [
      paragraph("hint", text(document_.count) + " run(s) stored for this plan \u00b7 read-only " +
        "history reported as " + text(document_.read_only) + " by the API. " + text(document_.note) +
        " A run's recorded recommendation is the audit of what that run showed, not the plan's " +
        "current decision.")
    ];
    if (!runs.length) {
      children.push(paragraph("muted", "No run has been recorded for this plan yet. Only " +
        "POST /api/plans/{id}/optimize (the Recalculate control) appends one, and this view never " +
        "writes history."));
    } else {
      children.push(table("Stored runs, oldest first (read-only; every column is a payload value)",
        [
          ["Run", "id"],
          ["Kind", "run_kind"],
          ["Status", "status"],
          ["When (as stored)", "created_at"],
          ["BEFORE", "before_summary"],
          ["AFTER", "after_summary", { numeric: true }],
          ["Saved", "saved_summary", { numeric: true }],
          ["Route fingerprint", "route_fingerprint"],
          ["Committed route", "has_committed_route", { render: function (value) {
            return badgeFor(value);
          } }],
          ["Detail", "detail", { render: function (value, row) {
            var button = el("button", "run-row-button", "Show run detail (read-only)");
            button.type = "button";
            button.addEventListener("click", function () {
              showRunDetail(row.id);
            });
            return button;
          } }]
        ],
        runs.map(function (run) {
          var metrics = run.metrics || {};
          var fingerprints = run.fingerprints || {};
          var after = metrics.after || {};
          var before = metrics.user_baseline || null;
          return {
            id: run.id,
            run_kind: run.run_kind,
            status: run.status,
            created_at: run.created_at,
            before_summary: before
              ? duration(before.duration_sec) + ", " + distance(before.distance_m)
              : "not reported by that run",
            after_summary: duration(after.duration_sec) + ", " + distance(after.distance_m),
            saved_summary: duration(metrics.saved_duration_sec) + ", " +
              distance(metrics.saved_distance_m),
            route_fingerprint: fingerprints.route_fingerprint,
            has_committed_route: run.has_committed_route,
            detail: run.id
          };
        })));
      children.push(paragraph("hint", "Open a run for its full audit: algorithm and version, " +
        "tzdata version, cost policy, both fingerprints, the recorded order, the stored top-K " +
        "candidates and the recommendation that execution produced - all as stored."));
    }
    replace(byId("run-history"), children);
  }

  function renderRunDetailEmpty() {
    replace(byId("run-detail"), [
      paragraph("muted", "No run is open. A stored run is immutable history: pressing \u201cShow run " +
        "detail (read-only)\u201d reads it back with GET /api/runs/{run_id} and changes nothing.")
    ]);
  }

  /** `GET /api/runs/{run_id}`: the audit of one execution, rendered as returned. */
  function showRunDetail(runId) {
    clearError();
    setStatus("Reading stored run " + text(runId) + " \u2026");
    return request(ENDPOINTS.run, [runId]).then(function (document_) {
      state.runDetail = document_.data;
      renderRunDetail(document_);
      setStatus("Stored run " + text(runId) + " read back from the API (read-only: this page never " +
        "writes history).", "ok");
    }).catch(function (error) {
      showError(error, "reading stored run " + text(runId));
    });
  }

  function renderRunDetail(document_) {
    var run = document_.data || {};
    var metrics = run.metrics || {};
    var after = metrics.after || {};
    var before = metrics.user_baseline || {};
    var algorithm = metrics.algorithm_baseline || {};
    var recommendation = run.recommendation || {};
    var timezoneName = state.plan ? state.plan.timezone : null;

    var detail = el("div", "run-detail");
    replace(byId("run-detail"), [
      detail,
      el("p", "hint", "Read-only audit of one stored run. The recorded recommendation below is " +
        "what that run showed at the time - it is history and is never the plan's current decision.")
    ]);

    detail.appendChild(el("h4", null, "Identity and provenance (as stored)"));
    detail.appendChild(definitionList([
      ["Run id", text(run.id)],
      ["Plan id", text(run.plan_id)],
      ["Kind", badge(text(run.run_kind), "mode")],
      ["Status", badge(text(run.status), run.status === "ok" ? "true" : "false")],
      ["Created at (UTC)", text(run.created_at)],
      ["Algorithm", text(run.algorithm) + " " + text(run.algorithm_version)],
      ["tzdata version", text(run.tzdata_version)],
      ["Cost policy used", text((run.cost_policy || {}).name) +
        ((run.cost_policy || {}).provisional ? " (provisional)" : "")],
      ["Data provenance", text(run.data_provenance)],
      ["Inputs fingerprint", fingerprint((run.fingerprints || {}).inputs_fingerprint)],
      ["Route fingerprint", fingerprint((run.fingerprints || {}).route_fingerprint)],
      ["Committed route recorded", text(run.has_committed_route)],
      ["Order (as recorded)", (run.order || []).join(" \u2192 ") || "empty"]
    ]));

    detail.appendChild(el("h4", null, "Metrics with both baselines (the stored figures)"));
    detail.appendChild(definitionList([
      ["AFTER: complete route duration", duration(after.duration_sec)],
      ["AFTER: travel / waiting / service", duration(after.travel_sec) + " / " +
        duration(after.waiting_sec) + " / " + duration(after.service_sec)],
      ["AFTER: distance", distance(after.distance_m)],
      ["AFTER: FINISH arrival", instant(after.finish_arrival, timezoneName)],
      ["AFTER: feasible", badgeFor(after.feasible)],
      ["BEFORE (user baseline, as stored)", duration(before.duration_sec) + ", " +
        distance(before.distance_m) + ", " + text(before.baseline_kind)],
      ["Algorithm baseline (internal, as stored)", duration(algorithm.duration_sec) + ", " +
        distance(algorithm.distance_m) + ", " + text(algorithm.baseline_kind)],
      ["Saved duration (stored)", duration(metrics.saved_duration_sec)],
      ["Saved distance (stored)", distance(metrics.saved_distance_m)]
    ]));

    var violations = run.violations || [];
    detail.appendChild(el("h4", null, "Violations of that execution"));
    if (violations.length) {
      detail.appendChild(table("Violations (as stored)",
        [
          ["Stop", "stop_id"],
          ["Kind", "kind"],
          ["Message", "message"]
        ],
        violations));
    } else {
      detail.appendChild(paragraph("muted", "That run recorded no violation."));
    }

    detail.appendChild(el("h4", null, "Recorded recommendation of that run (history, never the " +
      "plan's decision)"));
    detail.appendChild(definitionList([
      ["Recorded outcome status", text(recommendation.status)],
      ["Recorded recommended stop", recommendation.recommended_stop_id ||
        "none recorded by that run"],
      ["Recorded ranked stop ids", (recommendation.ranked_stop_ids || []).join(", ") || "none"],
      ["Recorded resolved_at", instant(recommendation.resolved_at, timezoneName)],
      ["Recorded inputs fingerprint", fingerprint(recommendation.inputs_fingerprint)],
      ["Recorded as plan state", text(recommendation.as_plan_state)]
    ]));
    var diagnostics = recommendation.diagnostics || [];
    if (diagnostics.length) {
      detail.appendChild(table("Recorded diagnostics",
        [
          ["Violating stop", "stop_id"],
          ["Candidate first stop", "candidate_stop_id"],
          ["Code", "code"],
          ["Message", "message"]
        ],
        diagnostics));
    }

    var topK = run.top_k;
    detail.appendChild(el("h4", null, "Recorded top-K candidates of that run"));
    if (topK === null || topK === undefined) {
      detail.appendChild(paragraph("muted", "That run stored no candidate detail. \u201cNothing " +
        "stored\u201d and \u201cno candidate existed\u201d are different facts, and this page does " +
        "not guess which one it was."));
    } else if (!topK.length) {
      detail.appendChild(paragraph("muted", "That run recorded an empty candidate list."));
    } else {
      detail.appendChild(table("Recorded top-K (complete-route metrics, as stored)",
        candidateColumns(timezoneName), candidateRows(topK)));
    }
  }

  // -------------------------------------------------------------- actions --
  /** `GET /api/plans/{id}/recommendation` - the one advisory read of this page. */
  function getRecommendation() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    clearError();
    return withComputation(
      "Get recommendation for " + state.planId,
      ENDPOINTS.recommendation,
      [state.planId],
      function (document_) {
        state.recommendation = document_;
        state.recommendationIsStale = false;
        renderRecommendation(document_);
        var seconds = (document_.data || {}).computation_seconds;
        setStatus(
          "Recommendation recomputed live for " + state.planId + " in " + text(seconds) +
            " s (measured computation_seconds). It is advisory: nothing was applied and the plan is " +
            "unchanged.",
          "ok"
        );
      }
    ).catch(function (error) {
      showError(error, "getting the recommendation for " + state.planId);
    });
  }

  /**
   * `POST /api/plans/{id}/selection` with `mode=recommend` and the stop the API recommended.
   *
   * The stop id is read from the recommendation payload this page displayed - never chosen here -
   * and the API independently refuses the request unless that stop really is the current
   * recommendation, so a stale or absent recommendation cannot commit a stop (D4/D32).
   */
  function acceptRecommendation() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    var data = (state.recommendation && state.recommendation.data) || null;
    var recommended = data ? data.recommended_stop_id : null;
    if (!recommended) {
      showError({
        code: "no_recommendation_read",
        type: "ClientPrecondition",
        status: 0,
        message: "no recommendation is currently displayed, so there is no stop to accept. This " +
          "page will not invent one: press \u201cGet recommendation\u201d first."
      }, "accepting the recommendation");
      return Promise.resolve();
    }
    clearError();
    setStatus("Accepting the recommended first stop " + text(recommended) + " for " +
      state.planId + " \u2026");
    return request(ENDPOINTS.selection, [
      state.planId, FIRST_STOP_MODES.recommend, recommended
    ]).then(function (document_) {
      return applySelection(document_, "The recommendation was accepted as the driver's own " +
        "decision");
    }).catch(function (error) {
      showError(error, "accepting the recommendation for " + state.planId);
    });
  }

  /** `POST /api/plans/{id}/selection` with `mode=manual` and the chosen ENABLED stop. */
  function chooseFirstStop() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    var select = byId("manual-stop-select");
    var chosen = select ? select.value : "";
    if (!chosen) {
      showError({
        code: "no_stop_chosen",
        type: "ClientPrecondition",
        status: 0,
        message: "no enabled stop is chosen in the manual picker, so there is nothing to send. " +
          "This page will not choose a stop on the driver's behalf."
      }, "choosing a first stop");
      return Promise.resolve();
    }
    clearError();
    setStatus("Choosing " + text(chosen) + " as the driver's first stop for " + state.planId +
      " \u2026");
    return request(ENDPOINTS.selection, [
      state.planId, FIRST_STOP_MODES.manual, chosen
    ]).then(function (document_) {
      return applySelection(document_, "The stop was chosen manually by the driver");
    }).catch(function (error) {
      showError(error, "choosing a first stop for " + state.planId);
    });
  }

  /** `DELETE /api/plans/{id}/selection` - back to awaiting_first_stop_choice (D8). */
  function cancelSelection() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    clearError();
    setStatus("Cancelling the first-stop selection for " + state.planId + " \u2026");
    return request(ENDPOINTS.clearSelection, [state.planId]).then(function (document_) {
      return applySelection(document_, "The selection was cancelled");
    }).catch(function (error) {
      showError(error, "cancelling the selection for " + state.planId);
    });
  }

  /**
   * Shared follow-up of every selection change: refresh the plan (state, mode, provenance, pinned),
   * the manual picker, the route, the summary and the run history FROM THE SERVER.
   */
  function applySelection(document_, label) {
    var selection = document_.data || {};
    return refreshFromServer(state.planId).then(function () {
      renderSelectionPanel(selection);
      var firstStop = (state.plan && state.plan.first_stop) || {};
      setStatus(
        label + " for " + state.planId + ". The API now reports state " +
          text(firstStop.state) + ", mode " + text(firstStop.mode) + ", selected " +
          text(firstStop.selected_stop_id || "none") + ", source " +
          text(firstStop.selection_source || "none") + ", pinned " + text(firstStop.pinned) +
          ". " + text(selection.note),
        "ok"
      );
      return reloadRouteFromServer("the new selection");
    });
  }

  /**
   * Re-read the committed route from the server for the plan's current selection.
   *
   * The route is never carried over from a previous selection and never computed here: the
   * documented `409 no_first_stop_selected` refusal is shown as the honest answer when the plan is
   * awaiting a choice.
   */
  function reloadRouteFromServer(reason) {
    if (!state.planId) {
      return Promise.resolve();
    }
    return request(ENDPOINTS.route, [state.planId]).then(function (document_) {
      state.route = document_.data;
      renderRoute(document_);
      markRecommendationStale(reason);
      return loadRuns();
    }).catch(function (error) {
      if (error && error.code === "no_first_stop_selected") {
        state.route = null;
        renderRouteRefused(error);
        renderSummaryEmpty();
        showError(error, "reading the committed route after " + text(reason));
        return loadRuns();
      }
      showError(error, "reading the committed route after " + text(reason));
    });
  }

  /** `PUT /api/plans/{id}` with exactly one stop change: `enabled` or `priority`. */
  function updateStop(stopId, changes) {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    clearError();
    var what = changes.enabled === undefined
      ? "priority " + text(changes.priority)
      : (changes.enabled ? "restore" : "disable");
    setStatus("Sending " + what + " for stop " + text(stopId) + " \u2026");
    return request(ENDPOINTS.updateStop, [state.planId, stopId, changes])
      .then(function (document_) {
        var plan = document_.data || {};
        var counts = plan.counts || {};
        return refreshFromServer(state.planId).then(function () {
          setStatus(
            "PUT /api/plans/" + text(state.planId) + " applied " + what + " for stop " +
              text(stopId) + ". The server now reports " + text(counts.stops) + " stops, " +
              text(counts.enabled_stops) + " enabled and " + text(counts.disabled_stops) +
              " disabled. Recalculate to record a run for this change.",
            "ok"
          );
          return reloadRouteFromServer("the stop change");
        });
      })
      .catch(function (error) {
        showError(error, "changing stop " + text(stopId) + " of " + state.planId +
          " (PUT /api/plans/{id} with " + JSON.stringify(changes) + ")");
      });
  }

  /**
   * `POST /api/plans/{id}/optimize` - recalculate, append exactly one run row, then re-read the
   * route, the plan (selection state) and the run history FROM THE SERVER.
   */
  function recalculate() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    clearError();
    return withComputation(
      "Recalculate " + state.planId,
      ENDPOINTS.optimize,
      [state.planId],
      function (document_) {
        var run = document_.data || {};
        var computation = document_.computation || {};
        return refreshFromServer(state.planId).then(function () {
          return loadRuns();
        }).then(function () {
          return reloadRouteFromServer("the recalculation");
        }).then(function () {
          setStatus(
            "Recalculated " + state.planId + " in " + text(computation.computation_seconds) +
              " s (measured computation_seconds) and appended run " + text(run.id) + " (" +
              text(run.run_kind) + ", " + text(run.status) + "). The route, the selection state and " +
              "the run history were re-read from the server.",
            "ok"
          );
        });
      }
    ).catch(function (error) {
      showError(error, "recalculating " + state.planId);
    });
  }

  /** `GET /api/plans/{id}/route` - read the committed route, no write, no run row. */
  function requestRoute() {
    if (!state.planId) {
      setStatus("Open a plan first.", "error");
      return Promise.resolve();
    }
    clearError();
    return withComputation(
      "Read the committed route for " + state.planId,
      ENDPOINTS.route,
      [state.planId],
      function (document_) {
        state.route = document_.data;
        renderRoute(document_);
        var seconds = (document_.data || {}).computation_seconds;
        setStatus(
          "Committed route computed for " + state.planId + " in " + text(seconds) +
            " s (measured computation_seconds). The order, timeline and metrics are the engine's " +
            "own.",
          "ok"
        );
        return loadRuns();
      }
    ).catch(function (error) {
      if (error && error.code === "no_first_stop_selected") {
        state.route = null;
        renderRouteRefused(error);
        renderSummaryEmpty();
      }
      showError(error, "reading the committed route for " + state.planId);
    });
  }

  function createOrOpenDemoPlan() {
    clearError();
    setStatus("Creating or opening the deterministic DEMO plan \u2026");
    createDemoPlan().then(function (plan) {
      return loadPlans().then(function () {
        return openPlan(plan.id);
      });
    }).then(function (plan) {
      setStatus("Opened the DEMO/SYNTHETIC plan " + plan.id + " (provenance " +
        text(plan.data_provenance) + ").", "ok");
      return loadRuns();
    }).catch(function (error) {
      showError(error, "creating or opening the DEMO plan");
    });
  }

  function changePlan(event) {
    var planId = event.target.value;
    clearError();
    openPlan(planId).then(function () {
      return loadRuns();
    }).catch(function (error) {
      showError(error, "opening plan " + text(planId));
    });
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

  function bind(id, handler) {
    var node = byId(id);
    if (node) {
      node.addEventListener("click", handler);
    }
    return node;
  }

  function boot() {
    checkRequiredElements();
    bind("create-demo-plan", createOrOpenDemoPlan);
    bind("get-recommendation", getRecommendation);
    bind("accept-recommendation", acceptRecommendation);
    bind("choose-first-stop", chooseFirstStop);
    bind("cancel-selection", cancelSelection);
    bind("recalculate", recalculate);
    bind("request-route", requestRoute);
    bind("timeline-toggle", function () {
      togglePreview("timeline", "timeline-toggle");
    });
    bind("stop-list-toggle", function () {
      togglePreview("stop-list", "stop-list-toggle");
    });
    var planSelect = byId("plan-select");
    if (planSelect) {
      planSelect.addEventListener("change", changePlan);
    }

    renderRecommendationUnavailable(
      "No recommendation has been read yet. Press \u201cGet recommendation\u201d: the API " +
        "recomputes it live for that request and writes nothing."
    );
    renderRouteEmpty();
    renderSummaryEmpty();
    renderRunDetailEmpty();
    replace(byId("run-history"), [
      paragraph("muted", "Open or create a plan to read its immutable run history.")
    ]);
    renderStopList({ stops: [] });
    updateAcceptControl();

    request(ENDPOINTS.health).then(function (health) {
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
      return null;
    }).catch(function (error) {
      showError(error, "loading the workspace");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
