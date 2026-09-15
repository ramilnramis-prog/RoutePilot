"""U16 fix cycle: the two recovery-review defects, guarded by executing the served script.

What this module guards, and why it is a separate module
=======================================================

The U16 recovery review found two defects that **no byte-level test can see**, because both are
runtime facts of the served ``web/app.js``:

1. ``renderRecommendation`` passed bare string literals into ``replace(byId("recommended-stop"),
   [...])``. The file's own ``replace`` helper calls ``node.appendChild(child)``, and a real DOM
   throws ``TypeError`` for a string, so the recommended stop - and every element built after it in
   the same render (the top-K table, the rejected table, the selection panel and the first-stop
   state) - never rendered, and the failure surfaced as a page-level ``TypeError`` instead of the
   API's own error.
2. ``renderFirstStopState`` was only called from the recommendation render paths, so after a manual
   selection change or a cancel ``#first-stop-state`` could still show ``awaiting_first_stop_choice``
   while the rest of the page (and the persisted plan) said ``first_stop_selected``.

Three guards, each able to fail:

* :func:`replace_call_arrays` scans the **served** ``web/app.js`` for ``replace(node, [...])`` calls
  and reports any top-level array item that is a bare string literal. It is a static, deterministic
  scan of the delivered bytes; a reintroduced literal is reported with its line.
* the refresh-path call-site assertion proves ``renderFirstStopState()`` is reached from
  ``refreshFromServer`` (which every selection change goes through), not only from the
  recommendation renderers.
* :class:`ServedScriptInStrictDomTests` **executes** the served ``web/app.js`` inside a minimal,
  dependency-free strict DOM stub whose ``appendChild``/``insertBefore`` reject a non-``Node`` the
  way a real DOM does. It is driven with payloads recorded from the real API over the real
  transport (never hand-written), boots the real page, clicks the real controls and asserts on the
  elements the page built - the only offline way to catch a rendering exception.

No browser, no jsdom, no npm, no new dependency: the stub is JavaScript that is handed to the
``node`` binary when one is installed, and every test skips cleanly when it is not (the suite must
not depend on node). The recorded payloads come from an in-process ``127.0.0.1`` server this module
starts itself, exactly as ``tests/web/test_web_workspace.py`` does.

The stub is deliberately strict rather than permissive: it refuses a bare string, refuses a
selection body other than the one the API documents, and fails when a control it was told to click
is disabled - so a regression that would quietly degrade in a browser fails here instead.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from api.http_server import create_server
from api.services import ApiServices
from tests.api.support import (
    build_infeasible_demo_plan,
    cleanup_scratch_root,
    new_database_file,
    new_scratch_directory,
)

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "web"
APP_JS = WEB_ROOT / "app.js"
MAP_JS = WEB_ROOT / "map.js"
INDEX_HTML = WEB_ROOT / "index.html"

#: The constructed plan of ``tests/api/support.build_infeasible_demo_plan``: every enabled stop
#: closes before the driver departs, so the API's own recommendation outcome is
#: ``no_fully_feasible_route`` with no winner. It is a **second world** because the storage schema
#: keys stops by id, so the demo plan and this copy cannot share one database.
INFEASIBLE_PLAN_ID = "demo-unreachable"

#: The ``node`` binary, when this machine has one. ``None`` skips the executed-DOM tests cleanly.
NODE_BINARY = shutil.which("node")

#: A top-level array item that begins with a quote is a string literal, not a DOM node.
BARE_STRING_ITEM = re.compile(r"""^["'`]""")


# --------------------------------------------------------------------------- #
# Guard 1: the static scan of the served script
# --------------------------------------------------------------------------- #
def mask_javascript(script: str) -> str:
    """The script with comments and string literals blanked, character positions preserved.

    The scan has to find *code* parentheses and commas, and a comma inside ``"a, b"`` or inside a
    prose comment must not split an argument list. Comments and strings are replaced by spaces one
    character at a time (newlines kept), so every index in the mask is the same index in the script
    and a reported line number points at the real byte.
    """
    output: list[str] = []
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        following = script[index + 1] if index + 1 < length else ""
        if char == "/" and following == "*":
            end = script.find("*/", index + 2)
            end = length if end == -1 else end + 2
            output.append(re.sub(r"[^\n]", " ", script[index:end]))
            index = end
            continue
        if char == "/" and following == "/":
            end = script.find("\n", index)
            end = length if end == -1 else end
            output.append(" " * (end - index))
            index = end
            continue
        if char in ("'", '"', "`"):
            end = index + 1
            while end < length and script[end] != char:
                end += 2 if script[end] == "\\" else 1
            end = min(end + 1, length)
            output.append(re.sub(r"[^\n]", " ", script[index:end]))
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _matching(masked: str, start: int, opener: str, closer: str) -> int:
    """The index of the bracket that closes the one at ``start``, or ``-1``."""
    depth = 0
    for index in range(start, len(masked)):
        if masked[index] == opener:
            depth += 1
        elif masked[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _top_level_ranges(
    masked: str, original: str, start: int, end: int
) -> list[tuple[int, int]]:
    """Split ``masked[start:end]`` on the commas that are not inside any bracket pair.

    The emptiness test is made against ``original``, never against the mask: a string-only argument
    is *entirely* blank in the mask, and dropping it would hide exactly the item this scan exists to
    find.
    """
    ranges: list[tuple[int, int]] = []
    depth = 0
    begin = start
    for index in range(start, end):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            ranges.append((begin, index))
            begin = index + 1
    ranges.append((begin, end))
    return [(begin, stop) for begin, stop in ranges if original[begin:stop].strip()]


def replace_call_arrays(script: str) -> list[dict]:
    """Every ``replace(<node>, [ ... ])`` call in the script, with its top-level array items.

    Only calls whose second argument is an **array literal** are returned: ``replace(node, children)``
    (an array the caller assembled elsewhere) is not a literal this scan can judge, and the executed
    DOM guard is what covers those call sites. ``String.prototype.replace`` is excluded, so a
    ``.replace(/x/g, "y")`` in another asset can never be mistaken for a DOM call.

    A literal scan cannot see a *variable* that happens to hold a string, which is exactly why the
    executed-DOM guard below exists as well: the two guards are complementary.

    Returns one dict per call: ``{"line", "items": [(item_source, line), ...], "source"}``.
    """
    masked = mask_javascript(script)
    calls: list[dict] = []
    for match in re.finditer(r"(?<![.\w$])replace\s*\(", masked):
        open_paren = masked.index("(", match.start())
        close_paren = _matching(masked, open_paren, "(", ")")
        if close_paren == -1:
            continue
        arguments = _top_level_ranges(masked, script, open_paren + 1, close_paren)
        if len(arguments) < 2:
            continue
        begin, end = arguments[1]
        if not (masked[begin:end].strip().startswith("[") and masked[begin:end].strip().endswith("]")):
            continue
        inner_begin = begin + masked[begin:end].index("[") + 1
        inner_end = end - 1
        items = [
            (script[start:stop].strip(), script[:start].count("\n") + 1)
            for start, stop in _top_level_ranges(masked, script, inner_begin, inner_end)
        ]
        calls.append(
            {
                "line": script[:begin].count("\n") + 1,
                "items": items,
                "source": script[begin:end].strip(),
            }
        )
    return calls


def bare_string_replace_items(script: str) -> list[tuple[int, str]]:
    """``(line, item)`` for every bare string literal in a ``replace(node, [...])`` argument array."""
    return [
        (line, item)
        for call in replace_call_arrays(script)
        for item, line in call["items"]
        if BARE_STRING_ITEM.match(item)
    ]


def function_body(script: str, name: str) -> str:
    """The ``{...}`` body of the function ``name`` in the served script, as text."""
    declaration = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", script)
    if declaration is None:
        raise AssertionError(f"function {name} was not found in the served script")
    depth = 0
    opened = declaration.end() - 1
    while opened < len(script):
        if script[opened] == "(":
            depth += 1
        elif script[opened] == ")":
            depth -= 1
            if depth == 0:
                break
        opened += 1
    start = script.index("{", opened)
    depth = 0
    for end in range(start, len(script)):
        if script[end] == "{":
            depth += 1
        elif script[end] == "}":
            depth -= 1
            if depth == 0:
                return script[start : end + 1]
    raise AssertionError(f"function {name} has no closing brace")


# --------------------------------------------------------------------------- #
# Guard 3: the strict, dependency-free DOM stub executed by the node binary
# --------------------------------------------------------------------------- #
#: The stub plus the scenarios. It is handed to the ``node`` binary **on stdin** together with its
#: configuration (the recorded payloads and the world under test), so nothing is written to disk and
#: no long script has to survive a Windows command line. It prints one JSON report on stdout. Every
#: document it serves was recorded from the real API over the real transport; the stub never invents
#: a payload.
#:
#: :data:`HARNESS_BOOTSTRAP_JS` is the whole command line: it reads the stdin envelope, publishes
#: the configuration as a global and evaluates :data:`HARNESS_JS` in the same scope.
HARNESS_BOOTSTRAP_JS = (
    'var payload = JSON.parse(require("fs").readFileSync(0, "utf8"));'
    "globalThis.__ROUTEPILOT_DOM_HARNESS__ = payload.config;"
    "eval(payload.script);"
)

HARNESS_JS = r"""
"use strict";

var fs = require("fs");
var vm = require("vm");

var config = globalThis.__ROUTEPILOT_DOM_HARNESS__;
var documents = config.documents || {};
var appPath = config.app_path;
var mapPath = config.map_path;
var markupPath = config.markup_path;

var report = { loaded: false, loadError: null, observations: [], calls: [], errors: [] };

function recordError(error) {
  report.errors.push(String((error && error.stack) || error));
}

process.on("uncaughtException", recordError);
process.on("unhandledRejection", recordError);

// ----------------------------------------------------------- strict DOM stub --
function describe(value) {
  if (value === null) { return "null"; }
  if (value === undefined) { return "undefined"; }
  if (typeof value === "string") { return "the string " + JSON.stringify(value); }
  return typeof value + " " + JSON.stringify(value);
}

class Node {}

class Fragment extends Node {
  constructor() {
    super();
    this.nodeType = 11;
    this.childNodes = [];
  }
  appendChild(child) {
    if (!(child instanceof Node)) {
      throw new TypeError("appendChild(" + describe(child) + ") on a DocumentFragment: a real DOM " +
        "accepts only a Node");
    }
    if (child instanceof Fragment) {
      var moving = child.childNodes.slice();
      child.childNodes = [];
      for (var index = 0; index < moving.length; index += 1) { this.appendChild(moving[index]); }
      return child;
    }
    this.childNodes.push(child);
    return child;
  }
  get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }
  get textContent() {
    return this.childNodes.map(function (child) { return child.textContent; }).join("");
  }
}

class Text extends Node {
  constructor(value) {
    super();
    this.nodeType = 3;
    this.data = value === null || value === undefined ? "" : String(value);
  }
  get textContent() { return this.data; }
  set textContent(value) { this.data = value === null || value === undefined ? "" : String(value); }
  get firstChild() { return null; }
}

class Element extends Node {
  constructor(tagName) {
    super();
    this.nodeType = 1;
    this.tagName = String(tagName).toLowerCase();
    this.childNodes = [];
    this.attributes = {};
    this.listeners = {};
    this.parentNode = null;
    this.className = "";
    this.id = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
  }
  appendChild(child) {
    if (!(child instanceof Node)) {
      throw new TypeError("appendChild(" + describe(child) + ") on <" + this.tagName + ">: a real " +
        "DOM accepts a Node, never a bare string");
    }
    if (child instanceof Fragment) {
      var moving = child.childNodes.slice();
      child.childNodes = [];
      for (var index = 0; index < moving.length; index += 1) { this.appendChild(moving[index]); }
      return child;
    }
    if (child.parentNode) { child.parentNode.removeChild(child); }
    this.childNodes.push(child);
    child.parentNode = this;
    return child;
  }
  insertBefore(child, reference) {
    if (!(child instanceof Node)) {
      throw new TypeError("insertBefore(" + describe(child) + "): a real DOM accepts only a Node");
    }
    if (reference === null || reference === undefined) { return this.appendChild(child); }
    var position = this.childNodes.indexOf(reference);
    if (position < 0) {
      throw new Error("insertBefore: the reference node is not a child of <" + this.tagName + ">");
    }
    this.childNodes.splice(position, 0, child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    var position = this.childNodes.indexOf(child);
    if (position < 0) {
      throw new Error("removeChild: the node is not a child of <" + this.tagName + ">");
    }
    this.childNodes.splice(position, 1);
    child.parentNode = null;
    return child;
  }
  get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }
  get textContent() {
    return this.childNodes.map(function (child) { return child.textContent; }).join("");
  }
  set textContent(value) {
    this.childNodes.forEach(function (child) { child.parentNode = null; });
    this.childNodes = [];
    var text = value === null || value === undefined ? "" : String(value);
    if (text !== "") { this.appendChild(new Text(text)); }
  }
  setAttribute(name, value) { this.attributes[String(name)] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, String(name))
      ? this.attributes[String(name)] : null;
  }
  addEventListener(type, handler) {
    if (!this.listeners[type]) { this.listeners[type] = []; }
    this.listeners[type].push(handler);
  }
  dispatch(type) {
    var event = { type: type, target: this, currentTarget: this };
    (this.listeners[type] || []).slice().forEach(function (handler) { handler(event); });
    return event;
  }
}

// Every id the served markup declares, so the page's own completeness check sees a whole workspace.
// The markup's own `hidden` attribute is honoured too, so the initial state is the document's.
var elements = {};
var markup = fs.readFileSync(markupPath, "utf8");
var tagPattern = /<(\w+)([^>]*)id="([^"]+)"([^>]*)>/g;
var tagMatch;
while ((tagMatch = tagPattern.exec(markup)) !== null) {
  var elementId = tagMatch[3];
  if (elements[elementId]) { continue; }
  var attributes = tagMatch[2] + " " + tagMatch[4];
  var element = /select$/.test(elementId) ? new Element("select") : new Element("div");
  element.id = elementId;
  element.hidden = /(^|\s)hidden(\s|=|$)/.test(attributes);
  elements[elementId] = element;
}

var document = {
  readyState: "complete",
  head: new Element("head"),
  getElementById: function (id) {
    return Object.prototype.hasOwnProperty.call(elements, id) ? elements[id] : null;
  },
  createElement: function (tagName) { return new Element(tagName); },
  createTextNode: function (value) { return new Text(value); },
  createDocumentFragment: function () { return new Fragment(); },
  addEventListener: function () {}
};

// ------------------------------------------------------- the replayed server --
// A tiny state machine over the recorded documents: the plan the page last read, and the selection
// state it implies. It refuses anything the API does not document.
var server = { plan: documents.plan_awaiting };

function ok(doc) {
  return Promise.resolve({
    ok: true,
    status: 200,
    text: function () { return Promise.resolve(JSON.stringify(doc)); }
  });
}

function refusal(doc, status) {
  return Promise.resolve({
    ok: false,
    status: status,
    text: function () { return Promise.resolve(JSON.stringify(doc)); }
  });
}

function fetchStub(url, options) {
  var method = (options && options.method) || "GET";
  report.calls.push(method + " " + url);
  var body = null;
  if (options && options.body) {
    try { body = JSON.parse(options.body); } catch (error) { body = null; }
  }
  if (url === "/api/health" && method === "GET") { return ok(documents.health); }
  if (url === "/api/plans" && method === "GET") { return ok(documents.plans); }
  if (/^\/api\/plans\/[^/]+$/.test(url) && method === "GET") { return ok(server.plan); }
  if (/\/recommendation$/.test(url) && method === "GET") { return ok(documents.recommendation); }
  if (/\/runs$/.test(url) && method === "GET") { return ok(documents.runs); }
  if (/\/route$/.test(url) && method === "GET") {
    if (server.plan.data.first_stop.state === "awaiting_first_stop_choice") {
      return refusal(documents.route_refused, 409);
    }
    return ok(server.plan.data.first_stop.selection_source === "accepted_recommendation"
      ? documents.route_selected_recommend : documents.route_selected_manual);
  }
  if (/\/selection$/.test(url) && method === "POST") {
    if (body && body.mode === "manual" && body.stop_id === config.manual_stop_id) {
      server.plan = documents.plan_selected_manual;
      return ok(documents.selection_manual);
    }
    if (body && body.mode === "recommend" && body.stop_id === config.recommended_stop_id) {
      server.plan = documents.plan_selected_recommend;
      return ok(documents.selection_recommend);
    }
    throw new Error("the page sent a selection body the API does not document: " +
      JSON.stringify(body));
  }
  if (/\/selection$/.test(url) && method === "DELETE") {
    server.plan = documents.plan_awaiting;
    return ok(documents.selection_cleared);
  }
  throw new Error("the strict stub has no recorded document for " + method + " " + url);
}

// ------------------------------------------------------------------ drivers --
var OBSERVED = [
  "status-banner", "error-banner", "loading", "plan-summary", "recommendation-panel",
  "recommended-stop", "alternatives", "rejected-candidates", "selection-panel",
  "first-stop-state", "route-panel", "timeline", "summary-panel", "run-history", "run-detail"
];

function optionsOf(id) {
  var node = elements[id];
  if (!node) { return []; }
  return node.childNodes.filter(function (child) { return child.tagName === "option"; })
    .map(function (child) { return child.value; });
}

function observe(name) {
  var observation = { name: name, elements: {}, hidden: {}, manualOptions: [], planOptions: [] };
  OBSERVED.forEach(function (id) {
    var node = elements[id];
    observation.elements[id] = node ? node.textContent : null;
    observation.hidden[id] = node ? !!node.hidden : null;
  });
  observation.manualOptions = optionsOf("manual-stop-select");
  observation.planOptions = optionsOf("plan-select");
  return observation;
}

function settle(rounds) {
  var chain = Promise.resolve();
  for (var index = 0; index < (rounds || 20); index += 1) {
    chain = chain.then(function () {
      return new Promise(function (resolve) { setTimeout(resolve, 0); });
    });
  }
  return chain;
}

async function click(id, label) {
  var node = elements[id];
  if (!node) { throw new Error("the stub has no #" + id); }
  if (node.disabled) {
    throw new Error("#" + id + " is disabled: a real browser would not fire this click");
  }
  node.dispatch("click");
  await settle();
  report.observations.push(observe(label));
}

async function run() {
  if (config.scenario === "stub_self_check") {
    report.loaded = true;
    var probe = new Element("p");
    try {
      probe.appendChild("bare string");
      report.stubRejectsBareString = false;
    } catch (error) {
      report.stubRejectsBareString = error instanceof TypeError;
    }
    return report;
  }
  try {
    var context = vm.createContext({
      document: document,
      Node: Node,
      fetch: fetchStub,
      console: console
    });
    vm.runInContext("globalThis.window = globalThis;", context);
    vm.runInContext(fs.readFileSync(mapPath, "utf8"), context, { filename: mapPath });
    vm.runInContext(fs.readFileSync(appPath, "utf8"), context, { filename: appPath });
    report.loaded = true;
  } catch (error) {
    report.loadError = String((error && error.stack) || error);
    return report;
  }

  await settle();
  report.observations.push(observe("boot"));

  if (config.scenario === "recommendation") {
    await click("get-recommendation", "after-recommendation");
  } else if (config.scenario === "manual_then_cancel") {
    elements["manual-stop-select"].value = config.manual_stop_id;
    await click("choose-first-stop", "after-manual-choice");
    await click("cancel-selection", "after-cancel");
  } else if (config.scenario === "accept") {
    await click("get-recommendation", "after-recommendation");
    await click("accept-recommendation", "after-accept");
  } else {
    throw new Error("unknown scenario " + config.scenario);
  }
  return report;
}

run().then(function (result) {
  result.ok = true;
  process.stdout.write(JSON.stringify(result));
}, function (error) {
  recordError(error);
  report.ok = false;
  process.stdout.write(JSON.stringify(report));
});
"""


# --------------------------------------------------------------------------- #
# Recording the payloads the stub replays (real API, real transport)
# --------------------------------------------------------------------------- #
def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _request_json(base_url: str, method: str, path: str, body=None):
    """One request against the in-process server: ``(status, decoded payload)``."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base_url + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class _RecordedServer:
    """An in-process ``127.0.0.1`` server over a throwaway database in the scratch tree."""

    def __init__(self, database: str, static_root: Path) -> None:
        self.services = ApiServices(database)
        self.server = create_server(
            self.services, host="127.0.0.1", port=0, static_root=static_root, quiet=True
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="routepilot-u16-dom-record"
        )
        self.thread.start()
        host, port = self.server.server_address[0], self.server.server_address[1]
        self.base_url = f"http://{host}:{port}"

    def call(self, method: str, path: str, body=None):
        return _request_json(self.base_url, method, path, body)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.services.close()
        self.thread.join(5.0)


def record_demo_world(scratch: Path) -> dict:
    """Record every document the executed page reads, for the DEMO/SYNTHETIC plan.

    Nothing here is hand-written: each document is the API's own response over the real transport,
    so the replayed page sees exactly the bytes a browser would receive. The recorded sequence also
    *proves* the two states the guards assert on really exist in the API: the awaiting state after a
    cancel, and ``manual_choice`` / ``accepted_recommendation`` provenance after a selection.
    """
    recorded = _RecordedServer(new_database_file(scratch), scratch / "no-web")
    try:
        status, created = recorded.call("POST", "/api/plans", {})
        _expect(status == 201, f"POST /api/plans answered {status}: {created}")
        plan_id = created["data"]["id"]

        _, health = recorded.call("GET", "/api/health")
        _, plans = recorded.call("GET", "/api/plans")
        _, plan_awaiting = recorded.call("GET", f"/api/plans/{plan_id}")
        status, route_refused = recorded.call("GET", f"/api/plans/{plan_id}/route")
        _expect(
            status == 409 and route_refused["error"]["code"] == "no_first_stop_selected",
            f"an awaiting plan must refuse the route with 409 no_first_stop_selected, got {status}",
        )
        status, recommendation = recorded.call("GET", f"/api/plans/{plan_id}/recommendation")
        _expect(status == 200, f"GET recommendation answered {status}: {recommendation}")
        recommended_stop_id = recommendation["data"]["recommended_stop_id"]
        _expect(
            bool(recommended_stop_id),
            "the DEMO plan's recommendation is expected to name a stop; this fixture is what the "
            "executed page renders in its recommended branch",
        )
        _expect(
            recommendation["data"]["ranked"] and recommendation["data"]["rejected"],
            "the DEMO recommendation is expected to carry both a ranked and a rejected table",
        )
        _expect(
            bool(recommendation["data"]["diagnostics"]),
            "the DEMO recommendation is expected to carry the rejection diagnostics the page "
            "appends after the rejected table",
        )
        _, runs = recorded.call("GET", f"/api/plans/{plan_id}/runs")

        enabled = [
            stop["id"] for stop in plan_awaiting["data"]["stops"] if stop["enabled"] is True
        ]
        _expect(bool(enabled), "the DEMO plan must have at least one enabled stop")
        manual_stop_id = enabled[0]

        status, selection_manual = recorded.call(
            "POST",
            f"/api/plans/{plan_id}/selection",
            {"mode": "manual", "stop_id": manual_stop_id},
        )
        _expect(status == 200, f"the manual selection answered {status}: {selection_manual}")
        _expect(
            selection_manual["data"]["selection_source"] == "manual_choice"
            and selection_manual["data"]["pinned"] is True,
            "the manual selection must report manual_choice and pinned",
        )
        _, plan_selected_manual = recorded.call("GET", f"/api/plans/{plan_id}")
        _expect(
            plan_selected_manual["data"]["first_stop"]["state"] == "first_stop_selected",
            "the refreshed plan must report first_stop_selected after a manual choice",
        )
        status, route_selected_manual = recorded.call("GET", f"/api/plans/{plan_id}/route")
        _expect(status == 200, f"the committed route answered {status}: {route_selected_manual}")

        status, selection_cleared = recorded.call(
            "DELETE", f"/api/plans/{plan_id}/selection"
        )
        _expect(status == 200, f"the cancel answered {status}: {selection_cleared}")
        _expect(
            selection_cleared["data"]["state"] == "awaiting_first_stop_choice",
            "the cancel must return the awaiting_first_stop_choice state",
        )
        _, plan_awaiting_again = recorded.call("GET", f"/api/plans/{plan_id}")
        _expect(
            plan_awaiting_again["data"]["first_stop"]["state"] == "awaiting_first_stop_choice"
            and plan_awaiting_again["data"]["first_stop"]["selected_stop_id"] is None,
            "the refreshed plan must report awaiting_first_stop_choice after a cancel",
        )

        status, selection_recommend = recorded.call(
            "POST",
            f"/api/plans/{plan_id}/selection",
            {"mode": "recommend", "stop_id": recommended_stop_id},
        )
        _expect(status == 200, f"the accept answered {status}: {selection_recommend}")
        _expect(
            selection_recommend["data"]["selection_source"] == "accepted_recommendation",
            "accepting the recommendation must report accepted_recommendation provenance",
        )
        _, plan_selected_recommend = recorded.call("GET", f"/api/plans/{plan_id}")
        status, route_selected_recommend = recorded.call("GET", f"/api/plans/{plan_id}/route")
        _expect(status == 200, f"the committed route answered {status}: {route_selected_recommend}")
        recorded.call("DELETE", f"/api/plans/{plan_id}/selection")
    finally:
        recorded.close()

    return {
        "plan_id": plan_id,
        "manual_stop_id": manual_stop_id,
        "recommended_stop_id": recommended_stop_id,
        "first_ranked_stop_id": recommendation["data"]["ranked"][0]["stop_id"],
        "first_rejected_stop_id": recommendation["data"]["rejected"][0]["stop_id"],
        "documents": {
            "health": health,
            "plans": plans,
            "plan_awaiting": plan_awaiting,
            "plan_selected_manual": plan_selected_manual,
            "plan_selected_recommend": plan_selected_recommend,
            "recommendation": recommendation,
            "runs": runs,
            "route_refused": route_refused,
            "route_selected_manual": route_selected_manual,
            "route_selected_recommend": route_selected_recommend,
            "selection_manual": selection_manual,
            "selection_cleared": selection_cleared,
            "selection_recommend": selection_recommend,
        },
    }


def record_infeasible_world(scratch: Path) -> dict:
    """Record the API's own ``no_fully_feasible_route`` world: the second render branch.

    The plan is the constructed fixture of ``tests/api/support.build_infeasible_demo_plan``, saved
    through the same repository the API always uses, so the engine - not this test - produces the
    outcome. It needs its own database because the storage schema keys stops by id.
    """
    plan, _window_stop_id = build_infeasible_demo_plan(plan_id=INFEASIBLE_PLAN_ID)
    recorded = _RecordedServer(new_database_file(scratch), scratch / "no-web")
    try:
        with recorded.services.state.connection() as connection:
            recorded.services.state.plan_repository(connection).save(plan)
        plan_id = plan.id
        _, health = recorded.call("GET", "/api/health")
        _, plans = recorded.call("GET", "/api/plans")
        _, plan_awaiting = recorded.call("GET", f"/api/plans/{plan_id}")
        status, recommendation = recorded.call("GET", f"/api/plans/{plan_id}/recommendation")
        _expect(status == 200, f"GET recommendation answered {status}: {recommendation}")
        data = recommendation["data"]
        _expect(
            data["status"] == "no_fully_feasible_route"
            and data["recommended_stop_id"] is None
            and data["ranked"] == []
            and bool(data["rejected"]),
            "the infeasible fixture must produce the API's no-winner outcome with diagnostics",
        )
        _, runs = recorded.call("GET", f"/api/plans/{plan_id}/runs")
    finally:
        recorded.close()

    return {
        "plan_id": plan_id,
        "manual_stop_id": None,
        "recommended_stop_id": None,
        "documents": {
            "health": health,
            "plans": plans,
            "plan_awaiting": plan_awaiting,
            "recommendation": recommendation,
            "runs": runs,
        },
    }


def observation(report: dict, name: str) -> dict:
    """One recorded observation from the harness report, by its step name."""
    for entry in report["observations"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(
        f"the harness recorded no observation named {name!r}: "
        f"{[entry['name'] for entry in report['observations']]}"
    )


def element_text(entry: dict, element_id: str) -> str:
    return entry["elements"].get(element_id) or ""


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #
class ReplaceArgumentGuardTests(unittest.TestCase):
    """Guard 1: no ``replace(node, [...])`` array in the served script holds a bare string."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = APP_JS.read_text(encoding="utf-8")

    def test_the_scan_examines_the_delivered_replace_call_sites(self) -> None:
        """The guard is not vacuous: it really parses the served ``replace`` call sites."""
        calls = replace_call_arrays(self.script)
        self.assertGreaterEqual(
            len(calls),
            20,
            "the scan found too few replace(node, [...]) call sites in web/app.js; the guard would "
            "pass without examining the file",
        )

    def test_no_replace_argument_array_contains_a_bare_string(self) -> None:
        """``replace`` appends its children, so every top-level array item must be a DOM node.

        A bare string here is the defect the recovery review found by executing the script: a real
        DOM throws ``TypeError`` from ``appendChild`` and the rest of that render never happens.
        """
        offenders = bare_string_replace_items(self.script)
        self.assertEqual(
            offenders,
            [],
            "web/app.js passes a bare string into replace(<node>, [...]) at line(s) "
            + ", ".join(f"{line}: {item}" for line, item in offenders)
            + " - build the content from DOM nodes (document.createTextNode(...) or el(...))",
        )

    def test_the_guard_reports_a_reintroduced_bare_string(self) -> None:
        """The guard can fail: reintroducing the reported defect *in memory* is detected.

        The defect is rebuilt from the served script itself: the first ``replace`` array item that is
        a single-line ``document.createTextNode("...")`` is turned back into the bare string that
        caused it. The served file is never modified.
        """
        text_node_item = re.compile(r'^document\.createTextNode\("(?P<text>[^"\\]*)"\)$')
        anchor = None
        for call in replace_call_arrays(self.script):
            for item, line in call["items"]:
                match = text_node_item.match(item)
                if match:
                    anchor = (item, f'"{match.group("text")}"', line)
                    break
            if anchor:
                break
        self.assertIsNotNone(
            anchor,
            "no replace(node, [...]) argument array item in web/app.js is a single-line "
            'document.createTextNode("..."): the scan self-check cannot rebuild the defect, so this '
            "guard must be updated rather than left unable to fail",
        )
        item, bare, line = anchor
        broken = self.script.replace(item, bare, 1)
        self.assertNotEqual(broken, self.script)
        offenders = bare_string_replace_items(broken)
        self.assertTrue(
            offenders,
            "the scan did not report a bare string reintroduced into a replace(node, [...]) array",
        )
        self.assertIn(line, [reported_line for reported_line, _ in offenders])
        self.assertEqual(offenders[0][1], bare)


class FirstStopStateRefreshTests(unittest.TestCase):
    """Guard 2: the refresh path itself rebuilds ``#first-stop-state`` from the server response."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = APP_JS.read_text(encoding="utf-8")

    def test_the_refresh_path_rebuilds_the_first_stop_state(self) -> None:
        refresh = function_body(self.script, "refreshFromServer")
        self.assertIn("request(ENDPOINTS.plan", refresh)
        self.assertIn("renderPlan(plan, document_)", refresh)
        self.assertIn(
            "renderFirstStopState()",
            refresh,
            "refreshFromServer no longer rebuilds #first-stop-state from the refreshed plan, so a "
            "manual choice or a cancel can leave the panel contradicting the server",
        )
        self.assertLess(
            refresh.index("renderPlan(plan, document_)"),
            refresh.index("renderFirstStopState()"),
            "the first-stop state must be rebuilt from the plan this refresh just read",
        )

    def test_every_selection_change_reaches_the_refresh_path(self) -> None:
        """Accept, manual choose and cancel all go through ``applySelection`` -> refresh."""
        apply_selection = function_body(self.script, "applySelection")
        self.assertIn("refreshFromServer", apply_selection)
        for action in ("acceptRecommendation", "chooseFirstStop", "cancelSelection"):
            with self.subTest(action=action):
                self.assertIn(
                    "applySelection(",
                    function_body(self.script, action),
                    f"{action} no longer applies its change through the shared refresh path",
                )


@unittest.skipUnless(NODE_BINARY, "node is not installed on this machine")
class ServedScriptInStrictDomTests(unittest.TestCase):
    """Guard 3: the served ``web/app.js`` executed in a strict, dependency-free DOM stub.

    The stub rejects a bare string in ``appendChild`` exactly as a browser does, so this is the one
    offline check that can catch a rendering exception. Every payload is a recorded API response.
    """

    world: dict = {}
    infeasible: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = new_scratch_directory("web-dom")
        cls.world = record_demo_world(cls.scratch)
        cls.infeasible = record_infeasible_world(cls.scratch)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def run_scenario(self, world: dict, scenario: str) -> dict:
        """Execute the served script through one scenario and return the harness report."""
        configuration = dict(world)
        configuration["scenario"] = scenario
        configuration["app_path"] = str(APP_JS)
        configuration["map_path"] = str(MAP_JS)
        configuration["markup_path"] = str(INDEX_HTML)
        completed = subprocess.run(
            [NODE_BINARY, "-e", HARNESS_BOOTSTRAP_JS],
            input=json.dumps({"config": configuration, "script": HARNESS_JS}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"the DOM harness failed for scenario {scenario!r}:\n{completed.stdout}\n"
            f"{completed.stderr}",
        )
        self.assertTrue(
            completed.stdout.strip(),
            f"the DOM harness produced no report for scenario {scenario!r}:\n{completed.stderr}",
        )
        report = json.loads(completed.stdout)
        if not report.get("loaded"):
            self.skipTest(
                "the served web/app.js could not be loaded into the DOM stub: "
                f"{report.get('loadError')}"
            )
        self.assertEqual(
            report["errors"],
            [],
            "the executed page raised an uncaught error: " + "; ".join(report["errors"]),
        )
        return report

    def test_the_page_renders_the_advisory_recommendation_without_throwing(self) -> None:
        """The recommended branch: the defect-1 crash site, and every panel built after it."""
        report = self.run_scenario(self.world, "recommendation")
        after = observation(report, "after-recommendation")
        plan_id = self.world["plan_id"]

        self.assertIn(f"GET /api/plans/{plan_id}/recommendation", report["calls"])
        # The recommended stop, built from DOM nodes, is on the page.
        self.assertIn(self.world["recommended_stop_id"], element_text(after, "recommended-stop"))
        self.assertIn("Recommended first stop", element_text(after, "recommended-stop"))
        self.assertIn("advisory only", element_text(after, "recommended-stop"))
        # The render continued: the top-K table, the rejected table and the diagnostics are there.
        self.assertIn(self.world["first_ranked_stop_id"], element_text(after, "alternatives"))
        self.assertIn("Ranked alternatives", element_text(after, "alternatives"))
        self.assertIn(self.world["first_rejected_stop_id"], element_text(after, "rejected-candidates"))
        self.assertIn("Rejected candidates", element_text(after, "rejected-candidates"))
        self.assertIn("Diagnostics", element_text(after, "rejected-candidates"))
        # The selection panel and the first-stop state, the last things that render, are populated.
        self.assertIn("Candidates evaluated", element_text(after, "recommendation-panel"))
        self.assertIn("Advisory", element_text(after, "recommendation-panel"))
        self.assertIn("awaiting_first_stop_choice", element_text(after, "selection-panel"))
        self.assertIn("awaiting_first_stop_choice", element_text(after, "first-stop-state"))
        # Nothing threw: the page's own error banner is empty and hidden, so the outcome the user
        # sees is the API's, not a page-level TypeError.
        self.assertEqual(element_text(after, "error-banner"), "")
        self.assertTrue(after["hidden"]["error-banner"])

    def test_the_page_renders_the_no_feasible_outcome_without_throwing(self) -> None:
        """The other defect-1 branch: no winner is still a valid answer, not an error."""
        report = self.run_scenario(self.infeasible, "recommendation")
        after = observation(report, "after-recommendation")

        self.assertIn("No fully feasible first stop", element_text(after, "recommended-stop"))
        self.assertIn("no ranked candidates", element_text(after, "alternatives"))
        self.assertIn("Rejected candidates", element_text(after, "rejected-candidates"))
        self.assertIn("awaiting_first_stop_choice", element_text(after, "first-stop-state"))
        self.assertEqual(element_text(after, "error-banner"), "")
        self.assertTrue(after["hidden"]["error-banner"])

    def test_a_manual_choice_and_a_cancel_re_render_the_first_stop_state(self) -> None:
        """Defect 2, executed: the panel follows the server through a choice and a cancel."""
        report = self.run_scenario(self.world, "manual_then_cancel")
        boot = observation(report, "boot")
        manual = observation(report, "after-manual-choice")
        cancel = observation(report, "after-cancel")
        plan_id = self.world["plan_id"]
        manual_stop_id = self.world["manual_stop_id"]

        # The picker really offered the stop this scenario chooses (never invented client-side).
        self.assertIn(manual_stop_id, boot["manualOptions"])
        # Before the choice the panel reported the server's awaiting state.
        self.assertIn("awaiting_first_stop_choice", element_text(boot, "first-stop-state"))
        # After the manual choice the panel is rebuilt from the refreshed plan, not left stale...
        self.assertIn("first_stop_selected", element_text(manual, "first-stop-state"))
        self.assertIn("manual_choice", element_text(manual, "first-stop-state"))
        self.assertIn(manual_stop_id, element_text(manual, "first-stop-state"))
        self.assertNotIn("awaiting_first_stop_choice", element_text(manual, "first-stop-state"))
        self.assertIn("manual_choice", element_text(manual, "plan-summary"))
        # ...and the rest of the refresh really ran: the committed route for the new selection is
        # rendered and nothing failed.
        self.assertIn("ok", element_text(manual, "route-panel"))
        self.assertIn("Route order and timeline", element_text(manual, "timeline"))
        self.assertEqual(element_text(manual, "error-banner"), "")
        # ...and the cancel returns the awaiting state in the same panel.
        self.assertIn("awaiting_first_stop_choice", element_text(cancel, "first-stop-state"))
        self.assertNotIn("first_stop_selected", element_text(cancel, "first-stop-state"))
        self.assertIn("awaiting_first_stop_choice", element_text(cancel, "selection-panel"))
        # The cancel also returns the honest refusal for the route, not an invented one.
        self.assertIn("no committed route", element_text(cancel, "route-panel"))
        # The only error is the API's own documented refusal for an unselected route.
        self.assertIn("no_first_stop_selected", element_text(cancel, "error-banner"))
        self.assertNotIn("TypeError", element_text(cancel, "error-banner"))
        # The flow the page really issued: one POST, one DELETE and three plan reads (boot + the
        # refresh after each selection change).
        self.assertIn(f"POST /api/plans/{plan_id}/selection", report["calls"])
        self.assertIn(f"DELETE /api/plans/{plan_id}/selection", report["calls"])
        self.assertEqual(report["calls"].count(f"GET /api/plans/{plan_id}"), 3)

    def test_accepting_the_recommendation_re_renders_the_first_stop_state(self) -> None:
        """The third selection change: accept sends the payload's stop and refreshes the same panel."""
        report = self.run_scenario(self.world, "accept")
        accepted = observation(report, "after-accept")
        recommended_stop_id = self.world["recommended_stop_id"]

        self.assertIn("first_stop_selected", element_text(accepted, "first-stop-state"))
        self.assertIn("accepted_recommendation", element_text(accepted, "first-stop-state"))
        self.assertIn(recommended_stop_id, element_text(accepted, "first-stop-state"))
        self.assertNotIn("awaiting_first_stop_choice", element_text(accepted, "first-stop-state"))
        self.assertIn("accepted_recommendation", element_text(accepted, "plan-summary"))
        self.assertIn(
            f"POST /api/plans/{self.world['plan_id']}/selection", report["calls"]
        )
        # The refresh after the accept really ran to the end: the committed route rendered and no
        # error of any kind reached the banner.
        self.assertIn("ok", element_text(accepted, "route-panel"))
        self.assertEqual(element_text(accepted, "error-banner"), "")
        self.assertTrue(accepted["hidden"]["error-banner"])

    def test_the_stub_itself_refuses_a_bare_string(self) -> None:
        """A sanity check on the stub: its ``appendChild`` is as strict as a browser's.

        Without this, a permissive stub could make the executed-DOM guard vacuous - the very failure
        mode the recovery review found. The check asks the same stub class the scenarios use to
        append a string, and requires a ``TypeError``.
        """
        report = self.run_scenario(self.world, "stub_self_check")
        self.assertTrue(
            report.get("stubRejectsBareString"),
            "the DOM stub accepted a bare string in appendChild, so the executed-DOM guard would "
            "not have caught the defect it exists for",
        )
