"""The U15/U16 web workspace: the static assets, the controls, the honesty strings and the gate.

What this module verifies, and what it deliberately cannot
==========================================================

**Verified here (offline, in process, on an ephemeral loopback port; no browser, no network):**

* the transport serves ``web/`` now that the directory exists: ``GET /`` returns the workspace HTML
  through the documented entry-point mapping, with ``text/html``, and ``styles.css`` / ``app.js`` /
  ``map.js`` are served with their documented content types (U13's pure static resolver finally has
  real files);
* every element id this unit and U16 depend on is present in the delivered HTML;
* the honesty strings are really there - DEMO/SYNTHETIC, the advisory statement that a
  recommendation is not an applied decision and that the driver decides, the honesty statement that
  real road routing is not implemented and no line is drawn between the stops (carried verbatim by
  both the markup next to the map and ``web/map.js``), the latency notice naming the accepted ~8 s
  worst case at the ~50-stop scale, and the tzdata/unimplemented-capability wiring;
* the unimplemented-capability list is **wired to the API**, not restated in the page: the script
  reads ``not_implemented_capabilities``, ``route_modes`` and ``timezone_data`` from
  ``GET /api/health``, and the served health payload really carries traffic, side-of-road,
  turn-by-turn, geocoding, real routing and every route mode except ``SMART_ROUTE``;
* no route-mode control other than ``SMART_ROUTE`` exists: the only ``<select>`` in the workspace is
  the plan chooser, no other mode name appears anywhere in the UI assets, and ``SMART_ROUTE`` itself
  is not asserted by the page at all - the plan's ``route_mode`` is displayed from the payload;
* no business formula in ``web/``: the script places documented API payload fields into the DOM and
  computes nothing. The focused review here (a) pins the exact payload fields the script reads, so a
  field cannot quietly become a recomputation or silently disappear, (b) refuses derivation
  primitives (``reduce`` / ``sort`` / ``Math.*``), and (c) pins the one number-formatting call the
  script is allowed to make (metres -> kilometres for display);
* the map configuration is read through the API: the only URL literals in ``web/`` are the Leaflet
  library's own namespace (the tile URL, attribution and max zoom are never written into an asset),
  the script renders the configuration from ``health.map``, and the served ``GET /api/health``
  carries the configuration in force from ``app_settings`` - with the documented default marked
  ``source: "default"`` when a key was never stored, and a stored key overriding it;
* the map degrades honestly: a missing Leaflet library, an unusable tile configuration and
  unreachable tiles each reach the ``#map-notice`` sink, and the non-map workspace is unaffected;
* ``node --check`` passes for every ``web/*.js`` when ``node`` is on the machine. The suite does
  **not** require node: the check skips cleanly when the binary is absent.

**U16 additions (the override controls, the run-history view and the end-to-end integration):**

* every new required element id is present in the **served** HTML (the U15 ids stay too);
* each control is wired to exactly the documented endpoint with the documented method and body:
  ``GET .../recommendation``, ``POST .../selection`` with ``{mode:"recommend", stop_id:<the
  recommended stop>}``, ``POST .../selection`` with ``{mode:"manual", stop_id:<the chosen enabled
  stop>}``, ``DELETE .../selection``, ``PUT /api/plans/{id}`` with ``{stops:[{stop_id, enabled}]}``
  / ``{stops:[{stop_id, priority}]}``, ``POST .../optimize`` and the read-only ``GET
  /api/runs/{run_id}`` for the detail view. This is asserted against the **served script text**, so
  the check is on the bytes the browser would receive;
* the run-history view is **read-only**: no PUT/DELETE and no run-appending POST is issued against
  ``/api/runs/...`` anywhere in the script, and the markup offers no edit/delete/reorder control;
* no drag/reorder control exists and no route mode other than ``SMART_ROUTE`` can be chosen (there
  is no route-mode control at all);
* error envelopes are **surfaced, not swallowed**: the ``{"error": {code, type, message}}`` envelope
  is read, every control has a failure path that reaches ``#error-banner``, the guidance table is
  keyed by documented codes (including ``no_first_stop_selected``, ``plan_busy`` with its retry
  guidance, ``invalid_input`` and ``unsupported_capability``), and no success message is written in
  a ``catch``;
* the ``#loading`` computing state is wired for the two requests that can take seconds: the
  recommendation and the recalculation;
* the selected stop is never invented client-side: the accept path uses the stop the **payload**
  named and the manual path uses the stop the **picker** returned.

**Still confirmed manually only (a browser is required, and this machine is offline):** the page as
it actually renders - Leaflet loading from the configured URL, tiles painting, the map drawn, the
DOM ``app.js`` builds at runtime, and the click-through of the controls below. No test here loads
Leaflet, fetches a tile or executes the script: there is no working network on this machine and no
browser in the test environment. That is an accepted environment limitation, recorded rather than
hidden.

Manual end-to-end checklist - the owner's portfolio flow, in order (D39(f))
==========================================================================

Run: ``python -m api.serve`` (prints its bound URL), then open that URL in a browser. The whole
flow below is exercised by hand; the automated half of this module can only assert the delivered
bytes, never the rendered result.

1. **Start the server** locally: ``python -m api.serve`` (the server root *is* the workspace entry
   point, served as ``/index.html``).
2. **Open it in a browser** at the printed URL. The persistent **DEMO / SYNTHETIC DATA** banner and
   the latency notice (the accepted ~8 s worst case at the ~50-enabled-stop portfolio scale) are on
   screen before any script runs.
3. **Open or create the DEMO/SYNTHETIC plan**: pick a stored plan in "Open plan", or press
   "Create / open the DEMO plan" (``POST /api/plans``, which returns the same deterministic fixture
   instead of duplicating it). The plan summary shows the stops, START and FINISH as plan
   *locations*, the route mode, and the first-stop state - which must read
   ``awaiting_first_stop_choice`` with no selected stop and no provenance.
4. **Request a recommendation**: press "Get recommendation" (``GET
   /api/plans/{id}/recommendation``). The ``#loading`` computing state must appear while the request
   is in flight, and the measured ``computation_seconds`` must be displayed when it returns.
5. **Understand that it is only a recommendation**: the advisory banner says it is *not an applied
   decision*, the panel reports ``applied_decision: false`` / ``as_plan_state: false``, and the
   plan's first-stop state is still ``awaiting_first_stop_choice`` - nothing was applied (D32).
6. **Inspect the alternatives and the rejected candidates**: the ranked top-K candidates with their
   complete-route metrics (travel, waiting, service, FINISH arrival, feasibility) and the rejected
   candidates with the ids of the stops whose hard window their complete route misses.
7. **Accept it or choose another stop**: press "Accept the recommendation" (``POST
   /api/plans/{id}/selection`` with ``mode=recommend`` and the recommended stop the panel displayed),
   or pick a different enabled stop in "First stop (manual)" and press "Use this stop" (the same
   endpoint with ``mode=manual``).
8. **See the selection pinned with provenance**: the plan summary and the selection panel must now
   read ``first_stop_selected``, the selected stop, provenance ``accepted_recommendation`` (accept)
   or ``manual_choice`` (manual picker) and ``pinned: true`` - all re-read from the server.
9. **See the full ordered route**: the route panel and the timeline show the engine's own order with
   the per-stop ETA/arrival, waiting, service start, service duration, departure, the local service
   window and the lateness; the map presents that same order as a 1-based number badge on each stop
   marker and draws no line between the stops.
10. **See ETA / waiting / service / FINISH**: the timeline columns above plus the summary's FINISH
    arrival - every figure the API's own, unchanged.
11. **Compare BEFORE vs AFTER**: the summary's BEFORE (the driver's own input order) against the
    AFTER (the RoutePilot order), with the saved duration and distance the API reports, the explicit
    violations, and both fingerprints. The algorithm baseline is labelled as internal, never as
    BEFORE.
12. **Disable / restore a stop or change priority and recalculate**: use a stop row's "Disable this
    stop" / "Restore this stop" or the priority field plus "Change priority" (each a ``PUT
    /api/plans/{id}`` with exactly one change), then press "Recalculate" (``POST
    /api/plans/{id}/optimize``). The computing state appears, exactly one run row is appended, and
    the route, the selection state and the run history are all re-read from the server. The computing
    indicator and the disabled controls must **persist through the post-recalculate route re-read**
    (``GET /api/plans/{id}/route``, measured in seconds at the ~50-stop scale), and must clear only
    once the route and the run history have come back - the plan chooser is never re-enabled while
    those reads are still in flight.
13. **Inspect the immutable run history**: the run-history table lists the plan's runs (id, kind,
    status, created_at, algorithm and version, tzdata version, both fingerprints, the recorded
    order, the recorded recommended stop and the stored top-K count); "Show run detail (read-only)"
    reads one run back through ``GET /api/runs/{run_id}`` and shows its metrics with the user **and**
    algorithm baselines and the after route, its violations and the recorded recommendation/top-K as
    the audit of what that run showed. Nothing in the view edits, reorders or deletes a run, and no
    stored recommendation is ever presented as the plan's current decision.
14. Finally read "What this build does NOT do": traffic, side-of-road, turn-by-turn, geocoding, real
    routing and every route mode except SMART_ROUTE, straight from the API's own capability report -
    and note that the page offers no control for any of them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from api.http_server import API_JSON_CONTENT_TYPE, STATIC_CONTENT_TYPES
from api.map_configuration import MAP_SETTING_DEFAULTS, MAP_SETTING_KEYS
from api.services import ApiServices
from demo.dataset import DEMO_PLAN_ID
from tests.api.support import (
    ServerBackedTestCase,
    cleanup_scratch_root,
    new_database_file,
    new_scratch_directory,
)

#: Remove the scratch tree once this module has run, so the suite leaves no artifact behind.
tearDownModule = cleanup_scratch_root

#: The repository's real ``web/`` directory and the four files this unit delivers.
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "web"
INDEX_HTML = WEB_ROOT / "index.html"
STYLES_CSS = WEB_ROOT / "styles.css"
APP_JS = WEB_ROOT / "app.js"
MAP_JS = WEB_ROOT / "map.js"
WEB_ASSETS = (INDEX_HTML, STYLES_CSS, APP_JS, MAP_JS)

#: Every element id the work units require (tests and U17 depend on them): the U15 ids stay working
#: and U16 adds the override controls, the error banner, the run-history list and the run detail.
REQUIRED_ELEMENT_IDS = (
    # U15 (unchanged)
    "status-banner",
    "latency-notice",
    "plan-select",
    "create-demo-plan",
    "recommendation-panel",
    "advisory-banner",
    "recommended-stop",
    "alternatives",
    "rejected-candidates",
    "selection-panel",
    "first-stop-state",
    "route-panel",
    "timeline",
    "summary-panel",
    "before-after",
    "map",
    "map-notice",
    "history-panel",
    "loading",
    # U16: the approved override controls and the run-history view
    "get-recommendation",
    "accept-recommendation",
    "manual-stop-select",
    "choose-first-stop",
    "cancel-selection",
    "stop-list",
    "recalculate",
    "error-banner",
    "run-history",
    "run-detail",
)

#: The controls whose click handler must call exactly one documented endpoint, and which endpoint
#: that is (the key of the script's own ``ENDPOINTS`` table). The endpoint's *documented method and
#: body* are asserted separately, below, against the same table.
CONTROL_ENDPOINT_WIRING = {
    "getRecommendation": "recommendation",
    "acceptRecommendation": "selection",
    "chooseFirstStop": "selection",
    "cancelSelection": "clearSelection",
    "updateStop": "updateStop",
    "recalculate": "optimize",
    "showRunDetail": "run",
}

#: Every endpoint the script's ``ENDPOINTS`` table declares, with the documented HTTP method and the
#: documented request body shape. The methods come from ``api/http_server.py``'s route table and the
#: bodies from the service layer's accepted fields; a control that drifted from either would fail
#: here.
DOCUMENTED_ENDPOINTS = {
    "health": ("GET", "/api/health"),
    "plans": ("GET", "/api/plans"),
    "createDemoPlan": ("POST", "/api/plans"),
    "plan": ("GET", "/api/plans/"),
    "updateStop": ("PUT", "/api/plans/"),
    "recommendation": ("GET", "/api/plans/", "/recommendation"),
    "selection": ("POST", "/api/plans/", "/selection"),
    "clearSelection": ("DELETE", "/api/plans/", "/selection"),
    "route": ("GET", "/api/plans/", "/route"),
    "optimize": ("POST", "/api/plans/", "/optimize"),
    "runs": ("GET", "/api/plans/", "/runs"),
    "run": ("GET", "/api/runs/"),
}

#: The error codes of the API's own documented envelope (``api.serialization.ERROR_CODES``) whose
#: guidance the UI must carry. Two of them are the ones this unit names explicitly: the honest
#: no-selection refusal and the busy plan with its retry guidance.
REQUIRED_ERROR_GUIDANCE_CODES = (
    "no_first_stop_selected",
    "plan_busy",
    "invalid_input",
    "unsupported_capability",
    "illegal_state",
)

#: The two actions that must show the ``#loading`` computing state (D39(e)): they run the
#: synchronous exhaustive engine work the latency notice describes.
COMPUTING_REQUESTS = ("recommendation", "optimize")

#: Vocabulary that would mean a reorder/drag **control** (D21/D39(d) keep it out of scope). The
#: words a page may honestly use to say it offers no such control are deliberately not listed.
REORDER_VOCABULARY = (
    "draggable", "ondragstart", "dragstart", "datatransfer", "sortable", "drop here", "move up",
    "move down", "reorder this",
)

#: Actions the read-only history may never offer.
HISTORY_EDIT_VOCABULARY = ("delete run", "edit run", "remove run", "reorder run", "rename run")

#: The API payload fields the UI is documented to read, grouped by the response they come from.
#: This is the anti-recomputation gate: a field the script stops reading - or starts deriving -
#: shows up here as a failure, so the rendering path stays "payload value -> DOM".
READ_PAYLOAD_FIELDS = {
    "health": (
        "data_provenance", "demo_data", "computation", "timezone_data",
        "implemented_capabilities", "not_implemented_capabilities", "route_modes",
        "accepted_mvp_latency", "health.map", "configuration.values",
    ),
    "plan": (
        "plan.id", "plan.timezone", "plan.route_mode", "plan.departure", "plan.finish",
        "plan.departure_time", "plan.data_provenance", "plan.inputs_fingerprint",
        "plan.first_stop", "plan.stops", "plan.counts", "selected_stop_id", "selection_source",
        "pinned",
    ),
    "recommendation": (
        "data.status", "data.recommended_stop_id", "data.advisory", "data.applied_decision",
        "data.as_plan_state", "data.note", "data.computation_seconds", "data.counts",
        "data.ranked", "data.rejected", "data.diagnostics", "first_leg", "complete_route",
        "violating_stop_ids", "candidate.rank",
    ),
    "route": (
        "data.order", "data.timeline", "data.metrics", "data.violations", "data.fingerprints",
        "data.selection", "estimated_arrival", "waiting_sec", "service_start",
        "service_duration_sec", "estimated_departure", "service_window_start", "lateness_sec",
        "metrics.after", "user_baseline", "algorithm_baseline", "saved_duration_sec",
        "saved_distance_m", "route_fingerprint", "inputs_fingerprint", "data.provenance",
        "tzdata_version",
    ),
    "runs": (
        "document_.count", "read_only", "document_.note", "run_kind", "created_at",
        "recommendation",
    ),
    # U16: the run-history view and the run detail read these too (list row + stored run).
    "run_history": (
        "document_.data", "algorithm_version", "tzdata_version", "cost_policy",
        "has_committed_route", "top_k", "fingerprints.route_fingerprint",
        "recommendation.recommended_stop_id", "recommendation.ranked_stop_ids",
        "recommendation.resolved_at", "recommendation.as_plan_state",
    ),
}

#: Derivation primitives a UI may not use on payload values. Formatting is allowed; deriving a
#: metric, an order or a rank is not.
FORBIDDEN_DERIVATION_PATTERNS = (
    r"\.reduce\s*\(",
    r"\.sort\s*\(",
    r"\bparseFloat\b",
    r"\bparseInt\b",
    r"\bMath\s*\.\s*(?:max|min|pow|sqrt|round|ceil|log|exp|random|trunc|sign)\b",
)

#: The complete list of arithmetic-looking statements ``web/app.js`` may contain: presentation of
#: one payload number for reading (a seconds count split into h/min/s; metres scaled to kilometres).
#: Anything else - a sum, a difference, a percentage, a rate - is a business formula and is refused.
ALLOWED_ARITHMETIC_STATEMENTS = (
    "var total = Math.abs(value);",
    "var hours = Math.floor(total / 3600);",
    "var minutes = Math.floor((total % 3600) / 60);",
    "var secs = total % 60;",
    "return sign + pad(hours) + \"h \" + pad(minutes) + \"m \" + pad(secs) + \"s\";",
    "return (value / 1000).toFixed(1) + \" km\";",
)

#: Vocabulary that would mean a formula, a claimed business value or a client-side identity. None of
#: these may appear anywhere in the delivered assets, prose comments included: if a comment needs one
#: of these words to explain what the code does, the code is doing too much.
FORBIDDEN_VOCABULARY = (
    "average_", "median_", "score +", "saving =", "compute_score", "estimate_", "weighted",
    "weighted_", "rank_", "sla_", "throughput", "utilisation", "utilization",
)

#: Route-mode names that may not appear in the UI assets: this build implements ``SMART_ROUTE``
#: only, and it offers no control for any other mode (D19).
OTHER_ROUTE_MODES = ("FASTEST", "SHORTEST", "MINIMUM_TURNS", "ON_THE_WAY", "START_TO_FINISH")

#: The one URL family a UI asset may name: the Leaflet library namespace, which only ever appears in
#: a comment (the library URL itself arrives from the API). Everything else - in particular every
#: tile-provider URL - must come from configuration.
URL_LITERAL_ALLOWED_SUBSTRINGS = ("leaflet", "openstreetmap.org/copyright")

#: The one honesty statement the workspace must carry about what the map shows, verbatim in BOTH the
#: markup next to the map and ``web/map.js``. The map-presentation hotfix removed the fabricated
#: straight line between stops, so the string it used to carry for that line is replaced by this
#: disclosure of the same fact: real road routing is not implemented and no line is drawn. The
#: substance of the old expectation - an honesty statement about the map's geometry, present in the
#: markup and in the map module - is unchanged; only the wording follows the map.
NO_ROUTE_GEOMETRY_DISCLOSURE = (
    "Real road routing is not implemented: no line is drawn between the stops. Only the stop "
    "locations, their route order and the recommended or driver-selected first stop are shown."
)

#: The disclosure's first sentence, which is what both assets carry on one line: the honesty-string
#: table matches plain substrings, and the full statement is wrapped across lines in the markup. The
#: full statement is asserted in :meth:`MapPresentationTests.test_the_page_no_longer_claims_a_line_is_drawn`,
#: where the markup's whitespace is collapsed first.
NO_ROUTE_GEOMETRY_HEADLINE = (
    "Real road routing is not implemented: no line is drawn between the stops."
)

#: The honesty strings the workspace must contain, with the asset each one lives in.
HONESTY_STRINGS = {
    "demo_synthetic": ("DEMO / SYNTHETIC", "index.html"),
    "not_real_routing": ("not real routing", "index.html"),
    "advisory_not_applied": ("NOT an applied decision", "index.html"),
    "driver_decides": ("driver decides", "index.html"),
    "no_route_geometry_label": (NO_ROUTE_GEOMETRY_HEADLINE, "index.html"),
    "latency_scale": ("~50-stop", "index.html"),
    "latency_eight_seconds": ("about 8 seconds", "index.html"),
    "loading_state": ("Computing", "index.html"),
    "capability_wiring": ("not_implemented_capabilities", "app.js"),
    "route_mode_wiring": ("route_modes", "app.js"),
    "tzdata_wiring": ("timezone_data", "app.js"),
    "map_notice_state": ('id="map-notice"', "index.html"),
    "computation_seconds_wiring": ("computation_seconds", "app.js"),
}


def strip_js_comments_and_strings(script: str) -> str:
    """The script with comments and string literals blanked, so only code operators remain.

    A naive search for ``-`` or ``/`` matches documentation URLs, prose hyphens and route paths. Only
    the *code* may be searched for arithmetic, so comments and strings are replaced character by
    character with spaces (line structure is preserved, which keeps a failure readable).
    """
    output = []
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


def read_asset(path: Path) -> str:
    """One delivered asset, as text."""
    return path.read_text(encoding="utf-8")


def flatten_javascript_strings(script: str) -> str:
    """A JavaScript asset with its string-literal wrapping removed, so a sentence can be matched.

    A long sentence in a script is written as several concatenated string literals, one per source
    line, so ``"a " + "b"`` never contains ``"a b"`` as a substring. Removing the double-quote
    concatenation operators and the double-quote characters and collapsing all whitespace makes the
    sentence findable whatever way the lines were wrapped.
    """
    for noise in ('" +', '+ "', '"'):
        script = script.replace(noise, " ")
    return " ".join(script.split())


def element_markup(element_id: str, path: Path = INDEX_HTML) -> str:
    """The markup of one element, matched to its own closing tag.

    A plain ``split`` on the first closing tag would cut a nested element short (the latency notice
    contains ``<code>``), so the closing tag is matched back to the opening one.
    """
    content = read_asset(path)
    match = re.search(
        r'<(\w+)[^>]*id="' + re.escape(element_id) + r'"[^>]*>(.*?)</\1>',
        content,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"element #{element_id} was not found in {path.name}")
    return match.group(0)


def function_body(script: str, name: str) -> str:
    """The ``{...}`` body of the function ``name`` in one asset, as text.

    Used to review one rendering function on its own: a claim made *inside* it cannot hide behind a
    payload read that happens somewhere else in the same file.
    """
    declaration = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", script)
    if declaration is None:
        raise AssertionError(f"function {name} was not found")
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


def endpoint_entry(script: str, name: str) -> str:
    """The ``ENDPOINTS.<name>`` block of the script, as text.

    ``web/app.js`` declares every request it can make in one ``ENDPOINTS`` table (URL builder,
    documented HTTP method, documented body), so asserting on this block is asserting on the bytes
    the browser would receive rather than on a Python restatement of them.
    """
    marker = re.search(r"\b" + re.escape(name) + r":\s*\{", script)
    if marker is None:
        raise AssertionError(f"ENDPOINTS.{name} was not found in the delivered script")
    depth = 0
    start = marker.end() - 1
    for end in range(start, len(script)):
        if script[end] == "{":
            depth += 1
        elif script[end] == "}":
            depth -= 1
            if depth == 0:
                return script[start : end + 1]
    raise AssertionError(f"ENDPOINTS.{name} has no closing brace")


def fetch(server, path: str):
    """One request against a server this module started itself: ``(status, content_type, body)``."""
    host, port = server.server_address[0], server.server_address[1]
    request = urllib.request.Request(f"http://{host}:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return (
                response.status,
                response.headers.get("Content-Type", ""),
                response.read().decode("utf-8"),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            error.headers.get("Content-Type", ""),
            error.read().decode("utf-8"),
        )


class WebWorkspaceTestCase(ServerBackedTestCase):
    """Serves the real ``web/`` directory over the real transport, in process and fully offline.

    The DEMO plan is created in ``setUpClass`` through the API itself (``POST /api/plans``), so the
    state under test is exactly what the documented demo flow produces. Each test gets its own
    server over that prepared database file (``use_prepared_database``), and the whole scratch tree
    is removed in ``tearDownModule``.
    """

    #: Reuse the database ``setUpClass`` prepared instead of a fresh one (see the test harness).
    use_prepared_database = True

    @classmethod
    def setUpClass(cls) -> None:
        cls.class_scratch = new_scratch_directory("web-u15")
        cls.class_database = new_database_file(cls.class_scratch)
        seeding = ApiServices(cls.class_database)
        try:
            seeding.plans.create_demo_plan({})
        finally:
            seeding.close()

    def setUp(self) -> None:
        """A **fresh** copy of the prepared state for every test.

        The web tests exercise state changes (a stored tile key, a selection, a created plan), so
        each test needs its own database or a test would observe a previous test's writes. The demo
        plan is deterministic, so re-seeding costs a cheap fixture build, not an engine run.
        """
        directory = new_scratch_directory("web-u15-case")
        self.addCleanup(shutil.rmtree, directory, True)
        self.class_database = new_database_file(directory)
        seeding = ApiServices(self.class_database)
        try:
            seeding.plans.create_demo_plan({})
        finally:
            seeding.close()
        super().setUp()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.class_scratch, ignore_errors=True)

    def missing_static_root(self) -> Path:
        """This case serves the repository's real ``web/`` directory."""
        return WEB_ROOT

    # -- the delivered files themselves ---------------------------------- #
    def test_the_u15_assets_exist_and_are_not_generated(self) -> None:
        for path in WEB_ASSETS:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"{path} is missing")
        # No build step, no bundler, no npm and no vendored map asset: everything web/ contains is
        # one of the four delivered files, so nothing generated or bundled was left behind either.
        self.assertEqual(
            sorted(entry.name for entry in WEB_ROOT.iterdir()),
            sorted(path.name for path in WEB_ASSETS),
        )

    # -- GET / and the content types ------------------------------------- #
    def test_get_root_returns_the_workspace_html(self) -> None:
        status, content_type, body = fetch(self.server, "/")
        self.assertEqual(status, 200, body)
        self.assertEqual(content_type, STATIC_CONTENT_TYPES[".html"])
        self.assertIn("<!doctype html>", body.lower())
        self.assertEqual(body, read_asset(INDEX_HTML))

    def test_get_index_html_returns_the_same_document(self) -> None:
        root = fetch(self.server, "/")
        explicit = fetch(self.server, "/index.html")
        self.assertEqual(explicit[0], 200)
        self.assertEqual(explicit[2], root[2])

    def test_the_required_element_ids_are_in_the_served_html(self) -> None:
        body = fetch(self.server, "/")[2]
        for element_id in REQUIRED_ELEMENT_IDS:
            with self.subTest(element_id=element_id):
                self.assertRegex(body, r'id="' + re.escape(element_id) + r'"')

    def test_the_css_and_javascript_assets_are_served_with_their_content_types(self) -> None:
        expected = (
            ("/styles.css", STATIC_CONTENT_TYPES[".css"], "text/css"),
            ("/app.js", STATIC_CONTENT_TYPES[".js"], "application/javascript"),
            ("/map.js", STATIC_CONTENT_TYPES[".js"], "application/javascript"),
        )
        for path, documented, prefix in expected:
            with self.subTest(path=path):
                status, content_type, body = fetch(self.server, path)
                self.assertEqual(status, 200, body)
                self.assertEqual(content_type, documented)
                self.assertTrue(content_type.startswith(prefix))
                self.assertTrue(body.strip())

    # -- honesty strings -------------------------------------------------- #
    def test_the_honesty_strings_are_present_in_the_delivered_assets(self) -> None:
        assets = {
            "index.html": read_asset(INDEX_HTML),
            "styles.css": read_asset(STYLES_CSS),
            "app.js": read_asset(APP_JS),
            "map.js": read_asset(MAP_JS),
        }
        for name, (needle, where) in HONESTY_STRINGS.items():
            with self.subTest(honesty=name):
                haystack = assets[where].lower()
                self.assertIn(
                    needle.lower(), haystack, f"{needle!r} is missing from {where}"
                )

    def test_the_advisory_banner_states_a_recommendation_is_not_a_decision(self) -> None:
        body = fetch(self.server, "/")[2]
        banner = body.split('id="advisory-banner"', 1)[1].split("</p>", 1)[0]
        self.assertIn("NOT an applied decision", banner)
        self.assertIn("driver", banner)
        self.assertIn("advisory", banner.lower())

    def test_the_map_discloses_that_no_line_is_drawn_between_stops(self) -> None:
        """The map's honesty statement, verbatim in the markup AND in the map module.

        The map-presentation hotfix removed the fabricated straight line between stops, so the one
        honesty string that described that line follows the new, honest wording. The substance is
        unchanged: an honesty statement about the map sits next to the map, and the map module that
        draws it carries the same statement.
        """
        html = read_asset(INDEX_HTML)
        script = read_asset(MAP_JS)
        flattened_html = " ".join(html.split())
        self.assertIn(NO_ROUTE_GEOMETRY_HEADLINE, flattened_html)
        self.assertIn(NO_ROUTE_GEOMETRY_DISCLOSURE, flattened_html)
        # The module is JavaScript: its string may be wrapped across lines, so the source is
        # normalised (concatenation operators and quotes removed, whitespace collapsed) before the
        # same statement is looked for.
        flattened = flatten_javascript_strings(script)
        self.assertIn(NO_ROUTE_GEOMETRY_DISCLOSURE, flattened)
        # ...and the module exports that very statement, so the page and the drawer cannot drift.
        self.assertIn("NO_ROUTE_GEOMETRY_DISCLOSURE", script)
        # The map's "this is not road routing" claim survives, next to the map.
        self.assertIn("not road routing", html)
        # The removed geometry is gone from both assets: nothing claims a line is drawn.
        for asset, content in ((INDEX_HTML, html), (MAP_JS, script)):
            with self.subTest(asset=asset.name):
                self.assertNotIn("synthetic straight-line geometry", content.lower())
                self.assertNotIn("polyline", content.lower())

    def test_the_latency_notice_names_the_accepted_worst_case(self) -> None:
        notice = element_markup("latency-notice")
        # The HTML names the scale in full ("~50-enabled-stop portfolio scale"); the shorter
        # "~50-stop" of the brief is the same accepted figure, spelled out here.
        self.assertIn("~50-enabled-stop portfolio scale", notice)
        self.assertIn("about 8 seconds", notice)
        self.assertIn("computation_seconds", notice)
        self.assertIn("no", notice.lower())  # "has no background job queue"

    def test_the_computing_state_exists_and_is_driven_by_the_script(self) -> None:
        """The ``#loading`` state is real, and the two slow requests run through it (D39(e)).

        U16 moved the flag into one helper (``withComputation``) so the recommendation and the
        recalculation cannot drift apart; the flag itself is still set and cleared for every request
        that can take seconds, and the state is always cleared on failure too.
        """
        self.assertRegex(read_asset(INDEX_HTML), r'id="loading"')
        script = read_asset(APP_JS)
        self.assertIn("setLoading(true", script)
        self.assertIn("setLoading(false)", script)
        self.assertIn('byId("loading")', script)
        computation = function_body(script, "withComputation")
        self.assertIn("setLoading(true", computation)
        self.assertIn("setLoading(false)", computation)
        for action in ("getRecommendation", "recalculate"):
            with self.subTest(action=action):
                self.assertIn("withComputation", function_body(script, action))

    # -- capability honesty is wired to the API --------------------------- #
    def test_the_unimplemented_capabilities_the_page_must_show_come_from_the_api(self) -> None:
        payload = self.get("/api/health").json()
        entries = {entry["capability"]: entry for entry in payload["not_implemented_capabilities"]}
        for capability in (
            "provider:traffic",
            "provider:side_of_road",
            "provider:turn_by_turn",
            "provider:geocoding",
            "provider:real_routing",
        ):
            with self.subTest(capability=capability):
                self.assertIn(capability, entries)
                self.assertTrue(entries[capability]["detail"])
        self.assertNotEqual(entries["provider:real_routing"]["status"], "implemented")

    def test_every_route_mode_but_smart_route_is_reported_unimplemented(self) -> None:
        payload = self.get("/api/health").json()
        self.assertEqual(payload["route_modes"]["implemented"], ["SMART_ROUTE"])
        self.assertEqual(
            sorted(payload["route_modes"]["not_implemented"]), sorted(OTHER_ROUTE_MODES)
        )

    def test_the_page_reads_the_capability_report_instead_of_restating_it(self) -> None:
        script = read_asset(APP_JS)
        self.assertIn("not_implemented_capabilities", script)
        self.assertIn("route_modes", script)
        # The capability entries are rendered generically from the payload (name, status, detail,
        # requires); no capability claim is written out as page text.
        for hardcoded_claim in (
            "no live or historical traffic data",
            "no GeocodingProvider is implemented",
            "travel time and distance are DEMO/SYNTHETIC",
            "a real RoutingProvider",
        ):
            with self.subTest(claim=hardcoded_claim):
                self.assertNotIn(hardcoded_claim, script)

    def test_no_capability_status_row_is_written_into_the_renderer(self) -> None:
        """Every capability-status row is built from the payload, never asserted by the page.

        ``renderCapabilities`` may label a row and add its own presentation note, but a status
        written as a **row value** in the script would be a capability claim the API does not make -
        exactly the removed ``["Travelling-salesman solver", "not implemented"]`` (U15 review issue
        1). Each row must instead be built from ``not_implemented_capabilities`` /
        ``implemented_capabilities`` / ``route_modes``, which the second half of this test pins.
        """
        body = function_body(read_asset(APP_JS), "renderCapabilities")
        rows = re.findall(r"\[[^\[\]]*\]", body, re.DOTALL)
        self.assertGreaterEqual(len(rows), 1, "no capability row was found to review")
        for row in rows:
            if not re.search(r"""["']""", row):
                continue  # `|| []`: an empty fallback list states no capability at all
            with self.subTest(row=row):
                # A definition-list row is `[label, value]`; a hardcoded capability row is exactly
                # that shape with the status as the literal value.
                self.assertIsNotNone(
                    re.search(r"""["']\s*[,)\]]""", row, re.DOTALL),
                    f"row {row!r} is not a definition-list row",
                )
                self.assertIsNotNone(
                    re.search(r"""["']\s*[,)]\s*["']""", row, re.DOTALL),
                    f"row {row!r} states a capability status value instead of building it from the "
                    "API's capability report",
                )
        self.assertGreaterEqual(
            body.count("modes."), 2, "the route-mode rows no longer read the payload"
        )
        for source in ("not_implemented_capabilities", "implemented_capabilities", "route_modes"):
            with self.subTest(source=source):
                self.assertIn(source, body)

    def test_the_tzdataless_state_is_read_from_the_api_and_shown(self) -> None:
        payload = self.get("/api/health").json()
        self.assertIn("timezone_data", payload)
        self.assertIn("source", payload["timezone_data"])
        self.assertIn("install_command", payload["timezone_data"])
        self.assertIn("timezone_data", read_asset(APP_JS))
        self.assertIn("tzdata", read_asset(APP_JS).lower())

    # -- no route-mode control -------------------------------------------- #
    def test_there_is_no_route_mode_control_only_the_documented_pickers(self) -> None:
        """The only selects are the plan chooser (U15) and the manual first-stop picker (U16).

        There is deliberately no route-mode control of any kind: ``SMART_ROUTE`` is the only
        implemented mode, the plan's own ``route_mode`` is displayed from the payload, and no
        fallback to it is ever implied (D19/D39(d)).
        """
        body = read_asset(INDEX_HTML)
        selects = re.findall(r'<select[^>]*id="([^"]+)"', body)
        self.assertEqual(sorted(selects), ["manual-stop-select", "plan-select"])
        self.assertEqual(len(re.findall(r"<select\b", body)), len(selects))
        self.assertNotIn("<option", body)  # the options are built from the API's plan list

    def test_no_route_mode_other_than_smart_route_appears_in_the_ui_assets(self) -> None:
        for path in WEB_ASSETS:
            content = read_asset(path)
            for mode in OTHER_ROUTE_MODES:
                with self.subTest(asset=path.name, mode=mode):
                    self.assertNotIn(mode, content)

    def test_the_ui_never_asserts_the_route_mode_itself(self) -> None:
        """SMART_ROUTE is displayed from the plan payload, never claimed by the page."""
        self.assertNotIn("SMART_ROUTE", read_asset(INDEX_HTML))
        self.assertIn("plan.route_mode", read_asset(APP_JS))

    # -- no business formula ---------------------------------------------- #
    def test_the_script_reads_the_documented_api_payload_fields(self) -> None:
        script = read_asset(APP_JS)
        for group, fields in READ_PAYLOAD_FIELDS.items():
            for field in fields:
                with self.subTest(group=group, field=field):
                    self.assertIn(
                        field, script, f"app.js no longer reads {group}.{field}"
                    )

    def test_the_script_uses_no_derivation_primitive(self) -> None:
        script = read_asset(APP_JS)
        for pattern in FORBIDDEN_DERIVATION_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNone(
                    re.search(pattern, script),
                    f"app.js contains the derivation primitive {pattern!r}: every metric, order "
                    "and rank must come from the API unchanged",
                )

    def test_every_arithmetic_statement_in_the_script_is_a_display_formatter(self) -> None:
        """No accumulation or computed metric appears in ``web/app.js``.

        This is the offline half of the no-business-formula gate: the script may split a seconds
        count and scale metres to kilometres for reading, and it may concatenate text, but it may
        never accumulate a value (``+=``) or compute a derived quantity - a sum, an average, a
        saving, a rate or a rank. Comments and string literals are blanked first, so documentation
        URLs, prose hyphens and route paths can neither mask nor fake a formula.
        """
        script = strip_js_comments_and_strings(read_asset(APP_JS))
        accumulating = re.compile(r"(?<![=!<>+\-*/%])[-+*/%]=(?!=)")
        offenders = [
            line.strip()
            for line in script.splitlines()
            if accumulating.search(line) and line.strip() not in ALLOWED_ARITHMETIC_STATEMENTS
        ]
        self.assertEqual(
            offenders,
            [],
            f"web/app.js accumulates or derives a value: {offenders}. Every metric, saving, order "
            "and rank must come from the API unchanged (D39(f))",
        )
        # The formatters themselves are the only arithmetic-looking code, and they are pinned.
        for statement in ALLOWED_ARITHMETIC_STATEMENTS:
            with self.subTest(statement=statement):
                self.assertIn(statement, read_asset(APP_JS))

    def test_the_script_uses_no_formula_vocabulary(self) -> None:
        haystack = (read_asset(APP_JS) + read_asset(MAP_JS)).lower()
        for word in FORBIDDEN_VOCABULARY:
            with self.subTest(word=word):
                self.assertNotIn(word.lower(), haystack)

    def test_the_only_number_formatting_the_script_does_is_presentation(self) -> None:
        """Metres -> kilometres for display is the one arithmetic-looking call in web/app.js."""
        script = read_asset(APP_JS)
        self.assertEqual(script.count("toFixed"), 1)
        self.assertIn("(value / 1000).toFixed(1)", script)
        for formatter in ("function duration(", "function distance(", "function instant("):
            self.assertIn(formatter, script)

    def test_the_script_places_payload_values_into_the_dom(self) -> None:
        """The rendering path is ``payload value -> textContent``, with no arithmetic between."""
        script = read_asset(APP_JS)
        for renderer in (
            "text(data.computation_seconds)",
            "text(data.recommended_stop_id)",
            "text(counts.candidates_evaluated)",
            "duration(metrics.saved_duration_sec)",
            "fingerprint((data.fingerprints || {}).route_fingerprint)",
            "badgeFor(after.feasible)",
            "text(data.status)",
            "text(firstStop.selection_source",
        ):
            with self.subTest(renderer=renderer):
                self.assertIn(renderer, script)

    def test_the_script_does_not_fabricate_a_route_or_a_selection(self) -> None:
        """No route, order, candidate or selection may be invented or written client-side.

        The U16 write paths (the selection endpoints and the run-appending endpoint) now exist, so
        they may only be reached through the ``ENDPOINTS`` table and the documented bodies - never as
        an inline path, and never with a client-chosen stop. The one stop the accept path sends is
        the stop the API's own recommendation payload named.
        """
        script = read_asset(APP_JS)
        # No request path is ever built inline: every call goes through ENDPOINTS (the table is
        # asserted in `test_every_endpoint_keeps_its_documented_method_and_path`). The one place a
        # path may appear as a literal is that table itself.
        table_start = script.index("var ENDPOINTS = {")
        table_end = script.index("\n  };", table_start)
        outside_table = script[:table_start] + script[table_end:]
        for inline_path in ('"/selection"', '"/optimize"', '"/runs"', '"/route"'):
            with self.subTest(inline_path=inline_path):
                self.assertNotIn(inline_path, outside_table)
        # A recommendation is reported as advisory and never applied.
        self.assertIn("data.applied_decision", script)
        self.assertIn("data.as_plan_state", script)
        self.assertIn("data.advisory", script)
        # The accept path takes the stop from the payload, and the manual path from the picker.
        accept = function_body(script, "acceptRecommendation")
        self.assertIn("data.recommended_stop_id", accept)
        self.assertIn("recommended", accept)
        self.assertNotIn("selected_stop_id", accept)
        manual = function_body(script, "chooseFirstStop")
        self.assertIn('byId("manual-stop-select")', manual)
        self.assertIn("select.value", manual)
        # The one write on the page that is not a driver decision is the open-or-create DEMO step.
        self.assertIn("createDemoPlan", script)

    # -- the map configuration comes from the API -------------------------- #
    def test_no_tile_url_literal_is_written_into_the_ui_assets(self) -> None:
        for path in WEB_ASSETS:
            content = read_asset(path)
            for url in re.findall(r"https?://[^\s\"'<>)]+", content):
                with self.subTest(asset=path.name, url=url):
                    self.assertTrue(
                        any(allowed in url for allowed in URL_LITERAL_ALLOWED_SUBSTRINGS),
                        f"{path.name} hardcodes the URL {url!r}; the tile provider, the "
                        "attribution, the max zoom and the map library URL must be read over the "
                        "API (app_settings, D15/D39(c))",
                    )

    def test_the_tile_keys_are_fetched_through_the_api(self) -> None:
        script = read_asset(APP_JS)
        self.assertIn("/api/health", script)
        self.assertIn("health.map", script)
        self.assertIn("configuration.values", script)
        self.assertIn("RoutePilotMap.mountMap(configuration", script)
        # The documented key names and their sources travel with the API, not with the page: the
        # markup names no settings key holding a *value* (the two names it does mention are the
        # tile and library keys, named in a comment that says the values come from the API).
        self.assertIn("configured_keys", script)
        self.assertIn("defaulted_keys", script)
        self.assertNotIn("map_library_css_url", read_asset(INDEX_HTML))
        for url_key in ("tile_attribution", "tile_max_zoom", "map_library_css_url"):
            with self.subTest(key=url_key):
                self.assertNotIn(url_key, read_asset(INDEX_HTML))

    def test_the_health_payload_reports_the_map_configuration_in_force(self) -> None:
        payload = self.get("/api/health").json()
        self.assertIn("map", payload)
        block = payload["map"]
        for key in MAP_SETTING_KEYS:
            with self.subTest(key=key):
                self.assertEqual(block["values"][key], MAP_SETTING_DEFAULTS[key])
        # Nothing is stored in a fresh database, so every key is reported as defaulted: the API
        # never implies that a default is stored configuration (D16).
        self.assertEqual(block["defaulted_keys"], list(MAP_SETTING_KEYS))
        self.assertEqual(block["configured_keys"], [])
        self.assertTrue(block["usable"])
        self.assertEqual(
            sorted({setting["source"] for setting in block["settings"]}), ["default"]
        )
        self.assertEqual(len(block["settings"]), len(MAP_SETTING_KEYS))
        self.assertEqual(
            [setting["key"] for setting in block["settings"]], list(MAP_SETTING_KEYS)
        )

    def test_a_stored_tile_key_overrides_the_default_over_the_api(self) -> None:
        stored_url = "https://tiles.example.invalid/{z}/{x}/{y}.png"
        self.put("/api/settings/tile_url", body={"value": stored_url})
        self.assertEqual(self.get("/api/settings/tile_url").json()["data"]["value"], stored_url)
        block = self.get("/api/health").json()["map"]
        self.assertEqual(block["values"]["tile_url"], stored_url)
        self.assertIn("tile_url", block["configured_keys"])
        self.assertNotIn("tile_url", block["defaulted_keys"])
        # One stored key changes one key: the attribution default is untouched.
        self.assertEqual(
            block["values"]["tile_attribution"], MAP_SETTING_DEFAULTS["tile_attribution"]
        )

    def test_an_unusable_tile_configuration_is_reported_as_unusable(self) -> None:
        """A tile URL without the {z}/{x}/{y} template cannot build a layer: say so, never guess."""
        self.put("/api/settings/tile_url", body={"value": "not-a-tile-template"})
        block = self.get("/api/health").json()["map"]
        self.assertFalse(block["usable"])
        self.assertIn("tile map is unavailable", block["unavailable_note"])

    def test_the_map_library_url_is_configuration_too(self) -> None:
        stored_library = "https://cdn.example.invalid/leaflet.js"
        self.put("/api/settings/map_library_url", body={"value": stored_library})
        block = self.get("/api/health").json()["map"]
        self.assertEqual(block["values"]["map_library_url"], stored_library)
        self.assertIn("map_library_url", block["configured_keys"])

    def test_the_map_degrades_honestly_and_leaves_the_workspace_usable(self) -> None:
        script = read_asset(MAP_JS)
        # A missing library, an unusable configuration and unreachable tiles each report themselves
        # through the notice and change nothing else.
        for wiring in (
            "could not be loaded",
            "unavailable_note",
            "tileerror",
            "tileload",
            "setNoticeSink",
        ):
            with self.subTest(wiring=wiring):
                self.assertIn(wiring, script)
        self.assertRegex(read_asset(INDEX_HTML), r'id="map-notice"')
        for element_id in ("timeline", "summary-panel", "recommendation-panel", "selection-panel"):
            with self.subTest(element_id=element_id):
                self.assertRegex(read_asset(INDEX_HTML), r'id="' + element_id + r'"')

    # -- the payloads the page renders ------------------------------------- #
    def test_the_recommendation_endpoint_serves_the_panels_the_page_renders(self) -> None:
        payload = self.get(f"/api/plans/{DEMO_PLAN_ID}/recommendation").json()
        data = payload["data"]
        self.assertTrue(data["advisory"])
        self.assertFalse(data["applied_decision"])
        self.assertFalse(data["as_plan_state"])
        self.assertIn("ranked", data)
        self.assertIn("rejected", data)
        self.assertTrue(data["ranked"])
        self.assertIn("complete_route", data["ranked"][0])
        self.assertEqual(data["ranked"][0]["complete_route"]["violating_stop_ids"], [])
        rejected = data["rejected"]
        self.assertTrue(rejected)
        self.assertTrue(rejected[0]["complete_route"]["violating_stop_ids"])
        self.assertIsNone(rejected[0]["rank"])

    def test_the_plan_first_stop_state_is_unchanged_by_reading_the_recommendation(self) -> None:
        before = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]["first_stop"]
        self.get(f"/api/plans/{DEMO_PLAN_ID}/recommendation")
        after = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]["first_stop"]
        self.assertEqual(before, after)
        self.assertIsNone(after["selected_stop_id"])
        self.assertIsNone(after["selection_source"])
        self.assertEqual(after["state"], "awaiting_first_stop_choice")

    def test_the_route_panel_data_is_readable_for_a_selected_plan(self) -> None:
        """A selection is made only to prove the route panel's payload exists (U16 owns the UI).

        The selection is ``manual`` and made through the API, so it is the documented driver
        decision and the route is then the committed route for the stop **the driver chose** - not a
        recommendation presented as applied state.
        """
        plan = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]
        enabled = [stop["id"] for stop in plan["stops"] if stop["enabled"]]
        selection = self.select_first_stop(DEMO_PLAN_ID, "manual", enabled[0])
        self.assertEqual(selection.status, 200, selection.text)

        response = self.get(f"/api/plans/{DEMO_PLAN_ID}/route")
        self.assertEqual(response.status, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["plan_id"], DEMO_PLAN_ID)
        self.assertEqual(data["order"][0], enabled[0])
        self.assertEqual(data["selection"]["selected_stop_id"], enabled[0])
        self.assertEqual(data["selection"]["selection_source"], "manual_choice")
        self.assertEqual(len(data["timeline"]), len(data["order"]))
        first_row = data["timeline"][0]
        for field in (
            "estimated_arrival", "waiting_sec", "service_start", "service_duration_sec",
            "estimated_departure", "service_window_start", "lateness_sec",
        ):
            with self.subTest(field=field):
                self.assertIn(field, first_row)
        self.assertIn("after", data["metrics"])
        self.assertIn("user_baseline", data["metrics"])
        self.assertIn("saved_duration_sec", data["metrics"])
        self.assertIn("route_fingerprint", data["fingerprints"])
        self.assertIn("inputs_fingerprint", data["fingerprints"])

    def test_a_route_request_without_a_selection_is_refused_honestly(self) -> None:
        """No selection: the documented 409 refusal, never an invented route."""
        before = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]["first_stop"]
        self.assertEqual(before["state"], "awaiting_first_stop_choice")
        refused = self.get(f"/api/plans/{DEMO_PLAN_ID}/route")
        self.assertEqual(refused.status, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "no_first_stop_selected")

    def test_the_run_history_placeholder_endpoint_is_read_only(self) -> None:
        payload = self.get(f"/api/plans/{DEMO_PLAN_ID}/runs").json()
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["data"], [])
        self.assertIn("only POST /api/plans/{id}/optimize", payload["note"])

    def test_the_history_panel_is_documented_as_read_only_history(self) -> None:
        """The panel names the read endpoint, the only appending endpoint, and its read-only rule."""
        body = read_asset(INDEX_HTML)
        panel = body.split('id="history-panel"', 1)[0].rsplit("<section", 1)[1]
        self.assertIn("/runs", panel)
        self.assertIn("read-only", panel.lower())
        self.assertIn("only", panel.lower())
        self.assertIn("POST /api/plans/{id}/optimize", panel)
        self.assertIn("never the plan's current", panel)

    # -- node syntax check (optional) --------------------------------------- #
    @unittest.skipUnless(shutil.which("node"), "node is not installed on this machine")
    def test_the_javascript_assets_pass_a_node_syntax_check(self) -> None:
        """``node --check`` on every ``web/*.js``; skipped cleanly when node is absent.

        The suite must not depend on node: when the binary is missing this test reports a skip and
        every other test in this module still runs.
        """
        for path in sorted(WEB_ROOT.glob("*.js")):
            with self.subTest(path=path.name):
                completed = subprocess.run(
                    ["node", "--check", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"node --check {path.name} failed:\n{completed.stdout}\n{completed.stderr}",
                )


class U16ControlWiringTests(unittest.TestCase):
    """U16: every control is wired to the documented endpoint, method and body.

    These cases read the **delivered** ``web/index.html`` and ``web/app.js`` - the same bytes the
    transport serves - and assert the contract of each control against the route table and accepted
    request fields of ``api/`` (D39(d)). No browser and no server are needed: the wiring is a
    property of the delivered script.
    """

    def test_every_required_control_id_is_in_the_markup(self) -> None:
        body = read_asset(INDEX_HTML)
        for element_id in (
            "get-recommendation", "accept-recommendation", "manual-stop-select",
            "choose-first-stop", "cancel-selection", "stop-list", "recalculate",
            "error-banner", "run-history", "run-detail",
        ):
            with self.subTest(element_id=element_id):
                self.assertRegex(body, r'id="' + re.escape(element_id) + r'"')

    def test_every_endpoint_keeps_its_documented_method_and_path(self) -> None:
        """The script's own endpoint table carries the documented method and path of each call."""
        script = read_asset(APP_JS)
        for name, expected in DOCUMENTED_ENDPOINTS.items():
            with self.subTest(endpoint=name):
                block = endpoint_entry(script, name)
                method, *path_parts = expected
                self.assertRegex(block, r'method:\s*"' + re.escape(method) + r'"')
                self.assertIn(path_parts[0], block)
                for part in path_parts[1:]:
                    self.assertIn(part, block)

    def test_a_control_never_reaches_for_an_endpoint_it_does_not_own(self) -> None:
        """Each control names its own documented endpoint, and no unexpected one."""
        script = read_asset(APP_JS)
        for control, endpoint in CONTROL_ENDPOINT_WIRING.items():
            body = function_body(script, control)
            with self.subTest(control=control):
                self.assertIn(
                    "ENDPOINTS." + endpoint,
                    body,
                    f"{control} no longer names ENDPOINTS.{endpoint}",
                )
                unexpected = [
                    name for name in DOCUMENTED_ENDPOINTS
                    if name != endpoint and ("ENDPOINTS." + name) in body
                ]
                self.assertEqual(
                    unexpected,
                    [],
                    f"{control} reaches for endpoint(s) {unexpected} that are not its documented "
                    f"one ({endpoint})",
                )
                # A control either calls the endpoint itself or hands it to the computing-state
                # helper; both keep the endpoint in the control's own body.
                names_endpoint = ("ENDPOINTS." + endpoint) in body
                runs_through_helper = (
                    "withComputation(" in body
                    and re.search(r"\b" + re.escape(endpoint) + r"\s*,", body) is not None
                )
                self.assertTrue(
                    names_endpoint or runs_through_helper,
                    "%s neither uses ENDPOINTS.%s nor runs it through withComputation"
                    % (control, endpoint),
                )

    def test_the_selection_bodies_carry_the_documented_mode_and_stop(self) -> None:
        """``POST .../selection`` sends ``mode`` plus ``stop_id``, and cancel sends no body."""
        script = read_asset(APP_JS)
        selection = endpoint_entry(script, "selection")
        self.assertIn('method: "POST"', selection)
        self.assertIn("/selection", selection)
        self.assertIn("mode:", selection)
        self.assertIn("stop_id:", selection)
        # The documented mode vocabulary lives in one table (D4/D6): `recommend` and `manual`.
        self.assertIn('recommend: "recommend"', script)
        self.assertIn('manual: "manual"', script)
        self.assertIn("FIRST_STOP_MODES.recommend", function_body(script, "acceptRecommendation"))
        self.assertIn("FIRST_STOP_MODES.manual", function_body(script, "chooseFirstStop"))
        clear = endpoint_entry(script, "clearSelection")
        self.assertIn('method: "DELETE"', clear)
        self.assertIn("/selection", clear)
        self.assertRegex(clear, r"body: function \(\) \{ return null; \}")

    def test_the_stop_edit_body_is_a_single_documented_stop_change(self) -> None:
        """``PUT /api/plans/{id}`` carries ``{stops:[{stop_id, enabled|priority}]}`` - one change."""
        script = read_asset(APP_JS)
        update = endpoint_entry(script, "updateStop")
        self.assertIn('method: "PUT"', update)
        self.assertIn("/api/plans/", update)
        self.assertIn("stops: [entry]", update)
        self.assertIn("stop_id: stopId", update)
        self.assertIn("enabled", update)
        self.assertIn("priority", update)
        # The two controls that use it send exactly one of the two documented changes.
        row = function_body(script, "stopRow")
        self.assertIn("updateStop(stop.id, { enabled: false })", row)
        self.assertIn("updateStop(stop.id, { enabled: true })", row)
        self.assertIn("updateStop(stop.id, { priority:", row)
        # No reorder/position field is ever sent: that control does not exist (D21/D39(d)).
        for forbidden in ("input_position", "position:", "order_overrides"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, update)

    def test_the_recommendation_and_optimize_calls_are_read_and_recalculate(self) -> None:
        """``GET .../recommendation`` is a read and ``POST .../optimize`` is the recalculation."""
        script = read_asset(APP_JS)
        recommendation = endpoint_entry(script, "recommendation")
        self.assertIn('method: "GET"', recommendation)
        self.assertIn("/recommendation", recommendation)
        optimize = endpoint_entry(script, "optimize")
        self.assertIn('method: "POST"', optimize)
        self.assertIn("/optimize", optimize)
        self.assertRegex(optimize, r"body: function \(\) \{ return null; \}")
        recalculate = function_body(script, "recalculate")
        # Recalculate must re-read the plan (first-stop state), the history and the route.
        for follow_up in ("refreshFromServer", "loadRuns", "reloadRouteFromServer"):
            with self.subTest(follow_up=follow_up):
                self.assertIn(follow_up, recalculate)

    def test_every_action_refreshes_from_the_server_instead_of_editing_locally(self) -> None:
        """No action may update a panel from its own request body (D39(d))."""
        script = read_asset(APP_JS)
        for action in ("applySelection", "updateStop"):
            with self.subTest(action=action):
                self.assertIn("refreshFromServer", function_body(script, action))
        self.assertIn("refreshFromServer", function_body(script, "recalculate"))
        # The refresh itself reads the plan back through the API.
        refresh = function_body(script, "refreshFromServer")
        self.assertIn("request(ENDPOINTS.plan", refresh)
        self.assertIn("renderPlan(plan, document_)", refresh)

    def test_no_reorder_or_drag_control_exists(self) -> None:
        body = (read_asset(INDEX_HTML) + read_asset(APP_JS)).lower()
        for word in REORDER_VOCABULARY:
            with self.subTest(word=word):
                self.assertNotIn(word, body)

    def test_no_control_can_make_the_engine_apply_a_recommendation(self) -> None:
        """A selection is always an explicit driver press carrying an explicit stop (D4/D32)."""
        script = read_asset(APP_JS)
        self.assertNotIn('mode: "accept"', script)
        self.assertNotIn('"accept"', endpoint_entry(script, "selection"))
        for control in ("acceptRecommendation", "chooseFirstStop"):
            with self.subTest(control=control):
                body = function_body(script, control)
                self.assertIn("addEventListener", read_asset(APP_JS))  # bound from boot
                self.assertIn("FIRST_STOP_MODES.", body)
        binding = function_body(read_asset(APP_JS), "boot")
        for control in ("accept-recommendation", "choose-first-stop", "cancel-selection"):
            with self.subTest(control=control):
                self.assertIn('bind("' + control + '"', binding)


class U16RunHistoryTests(unittest.TestCase):
    """U16: the run-history view is read-only, in the script and in the markup."""

    def test_no_request_writes_to_a_run(self) -> None:
        """No PUT/DELETE and no run-appending POST is ever issued against a run path."""
        script = read_asset(APP_JS)
        runs = endpoint_entry(script, "runs")
        run = endpoint_entry(script, "run")
        self.assertIn('/runs"', runs)          # the plan's own history path
        self.assertIn('/api/runs/"', run)      # one stored run by its own id
        self.assertIn("encodeURIComponent(runId)", run)
        for name, block in (("runs", runs), ("run", run)):
            with self.subTest(endpoint=name):
                self.assertIn('method: "GET"', block)
                for verb in ("PUT", "DELETE", "POST", "PATCH"):
                    self.assertNotIn('method: "' + verb + '"', block)

    def test_the_run_detail_control_only_reads_one_run(self) -> None:
        body = function_body(read_asset(APP_JS), "showRunDetail")
        self.assertIn("request(ENDPOINTS.run", body)
        for verb in ("PUT", "DELETE", "POST"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, body)

    def test_the_history_markup_offers_no_edit_delete_or_reorder_control(self) -> None:
        body = read_asset(INDEX_HTML)
        history = body.split('id="history-panel"', 1)[1].split("</section>", 1)[0].lower()
        for word in HISTORY_EDIT_VOCABULARY:
            with self.subTest(word=word):
                self.assertNotIn(word, history)
        self.assertIn("read-only", history)

    def test_the_history_view_states_a_stored_recommendation_is_history(self) -> None:
        """A stored recommendation must never be presented as the plan's current decision."""
        script = read_asset(APP_JS)
        detail = function_body(script, "renderRunDetail")
        self.assertIn("recommendation.recommended_stop_id", detail)
        self.assertIn("never the", detail.lower())
        self.assertIn("recommendation.as_plan_state", detail)
        self.assertIn("recommendation.ranked_stop_ids", detail)
        history = function_body(script, "renderRunHistory")
        self.assertIn("not the plan's", history.lower())
        self.assertIn("read_only", history)

    def test_the_run_detail_shows_both_baselines_and_the_after_route(self) -> None:
        """Metrics with the user AND algorithm baselines, the after route, violations and top-K."""
        detail = function_body(read_asset(APP_JS), "renderRunDetail")
        for field in (
            "metrics.after", "metrics.user_baseline", "metrics.algorithm_baseline",
            "saved_duration_sec", "saved_distance_m", "has_committed_route", "run.top_k",
            "run.violations", "algorithm_version", "tzdata_version",
        ):
            with self.subTest(field=field):
                self.assertIn(field, detail)
        for field in ("fingerprints", "created_at", "run_kind", "status"):
            with self.subTest(field=field):
                self.assertIn(field, detail)


class U16ErrorAndLoadingTests(unittest.TestCase):
    """U16: error envelopes and the computing state are surfaced honestly."""

    def test_the_error_envelope_is_read_from_the_api_response(self) -> None:
        script = read_asset(APP_JS)
        request = function_body(script, "request")
        self.assertIn("payload.error", request)
        self.assertIn("detail.code", request)
        self.assertIn("detail.message", request)
        self.assertIn("ApiError", request)
        # A failing response is always an error: no 4xx/5xx becomes a returned payload.
        self.assertIn("if (!response.ok)", request)

    def test_every_action_has_a_failure_path_into_the_error_banner(self) -> None:
        """No control and no load swallows a failure silently."""
        script = read_asset(APP_JS)
        for action in (
            "getRecommendation", "acceptRecommendation", "chooseFirstStop", "cancelSelection",
            "updateStop", "recalculate", "requestRoute", "showRunDetail", "createOrOpenDemoPlan",
            "changePlan", "reloadRouteFromServer", "boot",
        ):
            with self.subTest(action=action):
                self.assertIn("showError(", function_body(script, action))
        # Nothing is hidden: the catch blocks never write a success message.
        for forged_success in ("\"ok\"", '"ok"'):
            for line in script.splitlines():
                if ".catch(" in line and forged_success in line:
                    self.fail(f"a catch block writes a success tone: {line.strip()}")

    def test_the_documented_error_codes_carry_guidance(self) -> None:
        script = read_asset(APP_JS)
        for code in REQUIRED_ERROR_GUIDANCE_CODES:
            with self.subTest(code=code):
                self.assertIn(code + ":", script)
        # The two the unit names explicitly: the honest refusal and the busy plan's retry guidance.
        self.assertIn("awaiting_first_stop_choice", script)
        self.assertIn("Retry guidance", script)
        self.assertIn("single-flight", script)

    def test_the_error_banner_is_wired_and_cleared(self) -> None:
        markup = read_asset(INDEX_HTML)
        self.assertRegex(markup, r'id="error-banner"[^>]*role="alert"')
        script = read_asset(APP_JS)
        self.assertIn('byId("error-banner")', function_body(script, "showError"))
        self.assertIn('byId("error-banner")', function_body(script, "clearError"))
        self.assertIn("banner.hidden = false", function_body(script, "showError"))
        # Success paths clear the previous refusal instead of leaving a stale error on screen.
        for action in ("getRecommendation", "recalculate", "updateStop", "acceptRecommendation"):
            with self.subTest(action=action):
                self.assertIn("clearError()", function_body(script, action))

    def test_a_successful_plan_load_clears_a_previously_shown_error(self) -> None:
        """The stale-error hole: the banner is cleared by the plan LOAD path too, not only by actions.

        The banner used to be cleared by ``changePlan``, ``createOrOpenDemoPlan`` and each action,
        but not by ``openPlan`` itself - and ``boot`` auto-opens the first stored plan by calling
        ``openPlan(planId)`` directly. A refusal left on screen by an earlier build or session could
        therefore sit above a perfectly healthy workspace. ``openPlan``'s success path now clears it,
        so a successful load can never leave a stale error behind. A real API failure is untouched:
        the clear sits on the resolved load path, after the plan is in state, and no message text or
        error contract changes.
        """
        script = read_asset(APP_JS)
        body = function_body(script, "openPlan")
        self.assertIn("clearError()", body, "openPlan must clear a previous error on its success path")
        self.assertLess(
            body.index("state.plan = plan"),
            body.index("clearError()"),
            "the clear must happen on the resolved load, not before the response arrived",
        )
        # The gap the fix closes: boot reaches the load path directly, without an action clearing.
        boot = function_body(script, "boot")
        self.assertIn("openPlan(planId)", boot)
        self.assertNotIn("clearError()", boot)
        # A failure still shows: openPlan keeps its rejection path, and the boot chain reports it.
        self.assertIn("showError(", boot)

    def test_the_loading_state_is_wired_for_the_two_slow_requests(self) -> None:
        """The recommendation and the recalculation show ``#loading`` (D39(e))."""
        script = read_asset(APP_JS)
        computation = function_body(script, "withComputation")
        self.assertIn("setLoading(true", computation)
        self.assertIn("setLoading(false)", computation)
        self.assertIn("computation_seconds", script)
        for action, endpoint in (
            ("getRecommendation", "recommendation"), ("recalculate", "optimize")
        ):
            with self.subTest(action=action):
                body = function_body(script, action)
                self.assertIn("withComputation", body)
                self.assertIn("ENDPOINTS." + endpoint, body)
        # The computing state names the controls it disables while a computation is in flight.
        loading = function_body(script, "setLoading")
        self.assertIn("loading.hidden", loading)
        self.assertIn("COMPUTING_CONTROLS", loading)
        self.assertIn('"loading"', script)

    def test_the_computing_state_outlives_the_follow_up_server_reads(self) -> None:
        """`setLoading(false)` is reached only after the report chain has settled.

        A recalculation's report then re-reads the route (``GET /api/plans/{id}/route``, measured in
        seconds at the ~50-stop scale) and the run history. Clearing the computing state when the
        request promise resolves would claim the page is idle and re-enable the plan chooser while
        those reads are still in flight, so the served script must resolve the report promise first
        and clear the state only inside the resolution handler (D39(d)/D39(e), D26/D32, U16
        contracts 1 and 3).
        """
        script = read_asset(APP_JS)
        computation = function_body(script, "withComputation")
        # The report is invoked exactly once, and its promise is what the resolution handler wraps.
        chain = re.search(
            r"Promise\.resolve\(\s*report\(document_\)\s*\)\s*\.then\(",
            computation,
        )
        self.assertIsNotNone(
            chain, "withComputation must resolve the report promise before clearing the state"
        )
        invocations = [m.start() for m in re.finditer(r"report\(document_\)", computation)]
        self.assertEqual(len(invocations), 1, "withComputation must invoke report exactly once")
        clears = [m.start() for m in re.finditer(r"setLoading\(false\s*\)\s*;", computation)]
        self.assertTrue(clears, "withComputation must clear the computing state somewhere")
        # No clear may sit before the report has even been invoked.
        self.assertFalse(
            [index for index in clears if index < invocations[0]],
            "setLoading(false) is reached before report is invoked at all",
        )
        # The clear that belongs to the success path is inside the awaited chain: the settled report
        # result is what comes back and the clear precedes returning it.
        resolved = computation[chain.start():]
        self.assertIn("setLoading(false);", resolved, "the awaited chain never clears the state")
        self.assertIn("return result;", resolved)
        self.assertLess(
            resolved.index("setLoading(false);"),
            resolved.index("return result;"),
            "the computing state must be cleared while the awaited report result is returned",
        )
        # The rejection handler still clears, so a failure cannot leave the page computing forever.
        rejected = resolved[resolved.index("catch("):]
        self.assertIn("setLoading(false);", rejected)
        self.assertIn("throw error;", rejected)
        # The recalculate report really is the chain that re-reads the route and the runs, so the
        # state kept alive above is the state that covers those reads.
        recalculate = function_body(script, "recalculate")
        self.assertIn("reloadRouteFromServer", recalculate)
        self.assertIn("loadRuns()", recalculate)


class U16StateHonestyTests(unittest.TestCase):
    """U16: what the panels show comes from the server, and every documented endpoint exists."""

    def test_the_script_reads_the_u16_payload_fields(self) -> None:
        script = read_asset(APP_JS)
        for group, fields in READ_PAYLOAD_FIELDS.items():
            for field in fields:
                with self.subTest(group=group, field=field):
                    self.assertIn(field, script, f"app.js no longer reads {group}.{field}")

    def test_the_selection_state_surface_reports_mode_provenance_and_pinning(self) -> None:
        script = read_asset(APP_JS)
        for field in (
            "firstStop.state", "firstStop.mode", "selected_stop_id", "selection_source", "pinned",
        ):
            with self.subTest(field=field):
                self.assertIn(field, script)
        panel = function_body(script, "renderSelectionPanel")
        self.assertIn("firstStopSentence", panel)

    def test_a_stale_recommendation_is_labelled_and_cannot_be_accepted(self) -> None:
        """After the plan changes, the recommendation in hand is stale, not current (D4/D11)."""
        script = read_asset(APP_JS)
        stale = function_body(script, "markRecommendationStale")
        self.assertIn("recommendationIsStale = true", stale)
        self.assertIn("STALE", stale)
        self.assertIn("renderRecommendation(state.recommendation)", stale)
        accept = function_body(script, "updateAcceptControl")
        self.assertIn("state.recommendationIsStale", accept)
        self.assertIn("state.computing", accept)
        # Reading a fresh recommendation clears the stale mark.
        self.assertIn(
            "state.recommendationIsStale = false", function_body(script, "getRecommendation")
        )
        # Opening a plan drops the previous plan's recommendation entirely.
        self.assertIn("state.recommendation = null", function_body(script, "openPlan"))
        # The plan change reaches the stale path from the shared route reload.
        self.assertIn("markRecommendationStale", function_body(script, "reloadRouteFromServer"))


class U16ServerEndpointsTestCase(ServerBackedTestCase):
    """The live half of U16: the documented endpoints the controls use, over the real transport.

    The demo plan is created through the API in ``setUp``, exactly as the documented "create or open
    the DEMO plan" step does, so the state under test is what the controls would meet in a browser.
    """

    def missing_static_root(self) -> Path:
        """This case drives the API the controls call, so it needs no static files."""
        return self.scratch / "no-web"

    def setUp(self) -> None:
        super().setUp()
        self.create_demo_plan()

    def test_the_endpoints_the_controls_depend_on_exist_and_behave(self) -> None:
        """The documented methods of the endpoints the controls use, through the real transport."""
        plan = self.get(f"/api/plans/{DEMO_PLAN_ID}").json()["data"]
        enabled = [stop["id"] for stop in plan["stops"] if stop["enabled"]]
        disabled = [stop["id"] for stop in plan["stops"] if not stop["enabled"]]

        # GET recommendation: advisory, writes nothing.
        recommendation = self.get(f"/api/plans/{DEMO_PLAN_ID}/recommendation")
        self.assertEqual(recommendation.status, 200, recommendation.text)
        payload = recommendation.json()["data"]
        self.assertTrue(payload["advisory"])
        self.assertFalse(payload["applied_decision"])
        recommended = payload["recommended_stop_id"]

        # POST selection with mode=recommend + the recommended stop.
        accepted = self.post(
            f"/api/plans/{DEMO_PLAN_ID}/selection",
            body={"mode": "recommend", "stop_id": recommended},
        )
        self.assertEqual(accepted.status, 200, accepted.text)
        selection = accepted.json()["data"]
        self.assertEqual(selection["selection_source"], "accepted_recommendation")
        self.assertTrue(selection["pinned"])
        self.assertEqual(selection["state"], "first_stop_selected")

        # DELETE selection: back to awaiting_first_stop_choice.
        cleared = self.delete(f"/api/plans/{DEMO_PLAN_ID}/selection")
        self.assertEqual(cleared.status, 200, cleared.text)
        self.assertEqual(cleared.json()["data"]["state"], "awaiting_first_stop_choice")

        # POST selection with mode=manual + a chosen enabled stop.
        chosen = self.post(
            f"/api/plans/{DEMO_PLAN_ID}/selection",
            body={"mode": "manual", "stop_id": enabled[0]},
        )
        self.assertEqual(chosen.status, 200, chosen.text)
        manual = chosen.json()["data"]
        self.assertEqual(manual["selection_source"], "manual_choice")
        self.assertEqual(manual["selected_stop_id"], enabled[0])

        # PUT with enabled=false / true and with a new priority.
        if disabled:
            restored = self.put(
                f"/api/plans/{DEMO_PLAN_ID}",
                body={"stops": [{"stop_id": disabled[0], "enabled": True}]},
            )
            self.assertEqual(restored.status, 200, restored.text)
        disabled_response = self.put(
            f"/api/plans/{DEMO_PLAN_ID}",
            body={"stops": [{"stop_id": enabled[-1], "enabled": False}]},
        )
        self.assertEqual(disabled_response.status, 200, disabled_response.text)
        restored_response = self.put(
            f"/api/plans/{DEMO_PLAN_ID}",
            body={"stops": [{"stop_id": enabled[-1], "enabled": True}]},
        )
        self.assertEqual(restored_response.status, 200, restored_response.text)
        # A priority the fixture itself reports, or 0 when the fixture stores none: either way the
        # value sent is a plain integer, which is exactly what the UI's priority control sends.
        reported_priorities = [
            stop["priority"] for stop in plan["stops"] if stop["priority"] is not None
        ]
        new_priority = int(reported_priorities[0]) if reported_priorities else 0
        priority = self.put(
            f"/api/plans/{DEMO_PLAN_ID}",
            body={"stops": [{"stop_id": enabled[0], "priority": new_priority}]},
        )
        self.assertEqual(priority.status, 200, priority.text)
        stored = priority.json()["data"]
        self.assertEqual(
            next(stop["priority"] for stop in stored["stops"] if stop["id"] == enabled[0]),
            new_priority,
        )

        # POST optimize: exactly one run row is appended and the history is still read-only.
        before = self.get(f"/api/plans/{DEMO_PLAN_ID}/runs").json()
        optimize = self.post(f"/api/plans/{DEMO_PLAN_ID}/optimize")
        self.assertEqual(optimize.status, 201, optimize.text)
        run = optimize.json()["data"]
        after = self.get(f"/api/plans/{DEMO_PLAN_ID}/runs").json()
        self.assertEqual(after["count"], before["count"] + 1)
        self.assertTrue(after["read_only"])
        self.assertEqual(after["data"][-1]["id"], run["id"])
        self.assertIn("computation_seconds", optimize.json()["computation"])

        # GET /api/runs/{run_id}: the read-only detail the UI renders.
        detail = self.get(f"/api/runs/{run['id']}")
        self.assertEqual(detail.status, 200, detail.text)
        data = detail.json()["data"]
        for field in ("metrics", "violations", "recommendation", "top_k", "fingerprints"):
            with self.subTest(field=field):
                self.assertIn(field, data)
        self.assertIn("user_baseline", data["metrics"])
        self.assertIn("algorithm_baseline", data["metrics"])
        self.assertIn("after", data["metrics"])
        # The recorded recommendation is history, never plan state.
        self.assertFalse(data["recommendation"]["as_plan_state"])
        # GET /api/runs/{id} exists as a read: the write verbs are refused (405).
        for verb in ("PUT", "DELETE"):
            with self.subTest(verb=verb):
                refused = self.request(verb, f"/api/runs/{run['id']}")
                self.assertEqual(refused.status, 405, refused.text)
                self.assertEqual(refused.json()["error"]["code"], "method_not_allowed")


class WebWorkspaceWithoutWebDirectoryTests(ServerBackedTestCase):
    """U13's behaviour is unchanged: with no ``web/`` directory a static request is a JSON 404."""

    def missing_static_root(self) -> Path:
        return self.scratch / "no-web"

    def test_a_missing_index_is_still_a_documented_404(self) -> None:
        for path in ("/", "/index.html", "/app.js"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.content_type, API_JSON_CONTENT_TYPE)
                self.assertEqual(response.json()["error"]["code"], "unknown_path")


class MapContainerAndStylesheetTests(unittest.TestCase):
    """Stage 4 browser hotfix: Leaflet CSS loads with Leaflet JS, and the container clips its tiles.

    The defect, seen in a real browser: the OSM tiles loaded but escaped the map container and
    scattered through the whole page. Cause - the API's documented default for
    ``map_library_css_url`` was ``None`` and the page injected the ``<link>`` only for a truthy URL,
    so ``leaflet.css`` was never loaded. Leaflet positions its panes and its 256x256 tile ``<img>``
    elements **absolutely** and those rules live in its stylesheet, so with the stylesheet missing
    the tiles fell back to normal document flow, and ``.map`` had neither ``position`` nor
    ``overflow`` to contain them.

    These guards are structural and deterministic (no browser is available here): they pin the
    configured stylesheet, the load order, the fallback derivation and the container rules. The
    rendered result itself stays human-verified - see the manual checklist above.
    """

    def test_the_stylesheet_default_is_a_real_leaflet_stylesheet(self) -> None:
        css_url = MAP_SETTING_DEFAULTS["map_library_css_url"]
        js_url = MAP_SETTING_DEFAULTS["map_library_url"]
        self.assertIsInstance(css_url, str, "the map stylesheet must be configured, not None")
        self.assertTrue(css_url.strip())
        self.assertTrue(css_url.startswith("https://"), css_url)
        self.assertTrue(css_url.endswith(".css"), css_url)
        # The stylesheet is the sibling of the script in the SAME pinned release, so overriding one
        # library URL cannot silently point the two at different Leaflet versions.
        self.assertEqual(css_url.rsplit("/", 1)[0], js_url.rsplit("/", 1)[0])

    def test_the_page_appends_leaflet_css_before_it_loads_leaflet_js(self) -> None:
        script = read_asset(APP_JS)
        self.assertIn('link.rel = "stylesheet"', script)
        self.assertIn("values.map_library_css_url", script)
        # The configured stylesheet wins; when only the script URL is configured, the sibling
        # leaflet.css of the same distribution is derived from it.
        self.assertIn("|| deriveStylesheetUrl(libraryUrl)", script)
        self.assertIn("function deriveStylesheetUrl", script)
        # Order matters: the rules must be in place before Leaflet initializes.
        self.assertLess(
            script.index('link.rel = "stylesheet"'),
            script.index("loadScript(libraryUrl)"),
            "the Leaflet stylesheet must be injected before the Leaflet script is loaded",
        )

    def test_the_derivation_accepts_the_leaflet_script_names_and_nothing_else(self) -> None:
        """The fallback maps the known Leaflet script names to `leaflet.css` beside them.

        The rule is exercised through its source: it is a fallback for a configuration that sets
        only the script URL, and the shipped default always supplies the stylesheet URL, so the
        configured value is what a running page uses today.
        """
        script = read_asset(APP_JS)
        match = re.search(r"var match = /(.*)/[a-z]*\.exec\(libraryUrl\);", script)
        self.assertIsNotNone(match, "the derivation pattern is missing from web/app.js")
        pattern = match.group(1)
        for accepted in ("leaflet.js", "leaflet.min.js", "leaflet-src.js"):
            self.assertRegex(accepted, pattern, f"{accepted} must resolve to a sibling leaflet.css")
        for refused in ("openlayers.js", "mapbox-gl.js", "leaflet.png"):
            self.assertNotRegex(refused, pattern, f"{refused} must not be treated as Leaflet")

    def test_the_map_container_is_positioned_and_clipped(self) -> None:
        styles = read_asset(STYLES_CSS)
        block = re.search(r"^\.map \{(?P<body>.*?)^\}", styles, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(block, "the .map rule is missing from web/styles.css")
        body = block.group("body")
        self.assertIn("position: relative", body, "Leaflet needs a positioned container")
        self.assertIn("overflow: hidden", body, "a tile must never escape the map container")
        self.assertIn("height: 420px", body, "the container needs a stable explicit height")

    def test_tiles_and_panes_are_kept_out_of_normal_document_flow(self) -> None:
        """Defence in depth: even if leaflet.css fails to load, tiles cannot join the page flow."""
        styles = read_asset(STYLES_CSS)
        self.assertIn(".map .leaflet-pane", styles)
        self.assertIn("position: absolute", styles)
        self.assertIn(".map img.leaflet-tile", styles)
        self.assertIn("max-width: none", styles)


class MapPresentationTests(unittest.TestCase):
    """The map-presentation hotfix: no fabricated line, route order via numbered stop markers.

    Every assertion is a source-level check of the delivered bytes, so it is deterministic and needs
    no browser, no Leaflet and no network. Nothing here computes a value: the order number is the
    index inside the API's own ``order`` array, and the recommendation/selection ids are payload
    values passed straight through.
    """

    def test_no_line_is_created_anywhere_in_the_map_module(self) -> None:
        """The fabricated straight-line geometry is gone for good, with no replacement geometry."""
        script = read_asset(MAP_JS)
        self.assertNotIn("polyline", script.lower())
        self.assertNotRegex(script, r"L\.\s*(polygon|polyline|curve|geodesic)\b")
        self.assertNotIn("synthetic straight-line geometry", script.lower())
        # No routing provider or URL was introduced with the removal.
        self.assertNotRegex(script.lower(), r"osrm|graphhopper|openrouteservice|mapbox|valhalla")
        self.assertNotRegex(script, r"https?://")

    def test_the_map_still_degrades_honestly_and_its_notices_promise_no_line(self) -> None:
        script = read_asset(MAP_JS)
        for wiring in (
            "could not be loaded",
            "unavailable_note",
            "tileerror",
            "tileload",
            "setNoticeSink",
            "reportTilesAfterGrace",
        ):
            with self.subTest(wiring=wiring):
                self.assertIn(wiring, script)
        flattened = flatten_javascript_strings(script).lower()
        self.assertIn("route-order numbers", flattened)
        self.assertNotIn("straight-line route order", flattened)

    def test_draw_route_numbers_stop_markers_from_the_payload_order(self) -> None:
        """The badge is the 1-based index in ``route.order`` - nothing derived, nothing sorted."""
        script = read_asset(MAP_JS)
        body = function_body(script, "drawRoute")
        self.assertIn("route.order", body)
        self.assertIn("orderPosition(", body)
        position = function_body(script, "orderPosition")
        self.assertIn("order.indexOf(", position)
        self.assertIn("index + 1", position)
        # No client-side ordering and no derivation primitive: the map module's one arithmetic
        # expression is the 1-based index above. (`.map(` is Leaflet's own map constructor here,
        # never an array projection - so the constructor is pinned and the projection is refused.)
        self.assertIn("return index < 0 ? null : index + 1;", position)
        self.assertIn("window.L.map(container", script)
        self.assertIsNone(re.search(r"(?<!window\.L)\.map\(", script))
        for primitive in ("sort(", ".reduce(", "Math.", "function distance("):
            with self.subTest(primitive=primitive):
                self.assertNotIn(primitive, script)
        # The markers are numbered with the payload position, and the popup states position N of M.
        self.assertIn("marker-number", function_body(script, "marker"))
        stop_marker = function_body(script, "stopMarker")
        self.assertIn("Route order position", stop_marker)
        self.assertIn(" of ", stop_marker)

    def test_the_plan_enabled_stops_are_drawn_when_no_route_is_in_hand(self) -> None:
        """No committed route yet still draws the payload's ENABLED stops, unnumbered."""
        body = function_body(read_asset(MAP_JS), "drawRoute")
        self.assertIn("stop.enabled === true", body)
        self.assertIn("order.length ? order : enabledStopIds", body)
        # Coordinates come from the payload stop records only; no point is invented.
        self.assertIn("stop.latitude", body)
        self.assertIn("stop.longitude", body)

    def test_the_recommended_and_driver_selected_stops_are_passed_and_styled_apart(self) -> None:
        script = read_asset(APP_JS)
        draw = function_body(script, "drawMap")
        self.assertIn("recommendedStopId:", draw)
        self.assertIn("recommendation.recommended_stop_id", draw)
        self.assertIn("selectedStopId:", draw)
        self.assertIn("firstStop.selected_stop_id", draw)

        map_script = read_asset(MAP_JS)
        body = function_body(map_script, "drawRoute")
        self.assertIn("payload.recommendedStopId", body)
        self.assertIn("payload.selectedStopId", body)
        self.assertIn("marker-recommended", body)
        self.assertIn("marker-selected", body)
        # The recommendation is advisory and is never applied by the map: nothing here reaches the
        # API, and the map only ever reads the two ids the caller passed in.
        self.assertIn("advisory only, not applied", function_body(map_script, "stopRoleText"))
        self.assertNotIn("apply", body.lower())

        styles = read_asset(STYLES_CSS)
        self.assertIn(".routepilot-marker .marker-recommended {", styles)
        self.assertIn(".routepilot-marker .marker-selected {", styles)
        recommended = styles.split(".routepilot-marker .marker-recommended {", 1)[1].split("}", 1)[0]
        selected = styles.split(".routepilot-marker .marker-selected {", 1)[1].split("}", 1)[0]
        self.assertNotEqual(recommended, selected, "the advice and the driver's decision must differ")
        self.assertIn("dashed", recommended)
        self.assertIn("var(--accent-route-strong)", selected)
        # ...and the difference must be the treatment, not just the colour: the recommendation is the
        # dashed advisory marker and the driver's own selection is a SOLID one. Without this, a dashed
        # selection (or a dashed combined rule) would still satisfy every assertion above.
        self.assertNotIn("dashed", selected,
                         "the driver's selection is a solid marker, not the dashed advisory marker")
        # The same stop can be both only when the driver accepted the recommendation, and the
        # combined state is styled as the driver's selection, which is what actually happened.
        self.assertIn(".routepilot-marker .marker-recommended.marker-selected {", styles)
        combined = styles.split(".routepilot-marker .marker-recommended.marker-selected {", 1)[1]
        self.assertIn("var(--accent-route-strong)", combined.split("}", 1)[0])
        self.assertNotIn("dashed", combined.split("}", 1)[0],
                         "the combined state is the driver's decision, so it stays solid")

    def test_the_fit_bounds_survives_over_the_drawn_points_only(self) -> None:
        body = function_body(read_asset(MAP_JS), "drawRoute")
        self.assertIn("fitBounds(window.L.latLngBounds(points)", body)
        for point in ("startPoint", "finishPoint", "points.push(latlng)"):
            with self.subTest(point=point):
                self.assertIn(point, body)

    def test_the_page_no_longer_claims_a_line_is_drawn(self) -> None:
        html = read_asset(INDEX_HTML)
        script = read_asset(APP_JS)
        for asset, content in ((INDEX_HTML, html), (APP_JS, script)):
            with self.subTest(asset=asset.name):
                self.assertNotIn("synthetic straight-line geometry", content.lower())
        flattened = flatten_javascript_strings(script)
        self.assertIn("draws no line between the stops", flattened)
        # The collapsed technical details carry the same honest disclosure.
        details = html.split("Demo limitations / technical details", 1)[1]
        self.assertIn(NO_ROUTE_GEOMETRY_DISCLOSURE, " ".join(details.split()))


if __name__ == "__main__":
    unittest.main()
