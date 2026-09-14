"""The U15 web workspace: the static assets, the honesty strings and the no-business-formula gate.

What this module verifies, and what it deliberately cannot
==========================================================

**Verified here (offline, in process, on an ephemeral loopback port; no browser, no network):**

* the transport serves ``web/`` now that the directory exists: ``GET /`` returns the workspace HTML
  through the documented entry-point mapping, with ``text/html``, and ``styles.css`` / ``app.js`` /
  ``map.js`` are served with their documented content types (U13's pure static resolver finally has
  real files);
* every element id this unit and U16 depend on is present in the delivered HTML;
* the honesty strings are really there - DEMO/SYNTHETIC, the advisory statement that a
  recommendation is not an applied decision and that the driver decides, the synthetic
  straight-line geometry label, the latency notice naming the accepted ~8 s worst case at the
  ~50-stop scale, and the tzdata/unimplemented-capability wiring;
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

**Confirmed manually only (a browser is required, and this machine is offline):** the page as it
actually renders - Leaflet loading from the configured URL, tiles painting, the map drawn, and the
DOM ``app.js`` builds at runtime. No test here loads Leaflet, fetches a tile or executes the script:
there is no working network on this machine and no browser in the test environment. That is an
accepted environment limitation, recorded rather than hidden.

Manual visual checklist (browser: ``python -m api.serve``, then open the printed URL)
-------------------------------------------------------------------------------------

1. Open ``http://127.0.0.1:8000/`` (the server root is the workspace entry point).
2. See the persistent **DEMO / SYNTHETIC DATA** banner before anything else, plus the latency notice
   naming the accepted ~8 s worst case at the ~50-stop scale.
3. See the plan summary: stops, departure, START and FINISH as plan **locations**, and the
   first-stop state with its provenance and pinning.
4. Press **"Create / open the DEMO plan"** if no plan is listed; it should open ``demo-route-01`` and
   report provenance ``DEMO_SYNTHETIC``.
5. Press **"Compute the recommendation"**: the loading/computing state must appear while the request
   is in flight, and the measured ``computation_seconds`` must be displayed afterwards.
6. Read the recommendation: the advisory banner says it is **not an applied decision**, the
   recommended stop is named, the ranked alternatives show their **complete-route** metrics, and the
   rejected candidates show their **violating stops**. The plan's first-stop state must still read
   ``awaiting_first_stop_choice`` - a recommendation changes nothing (D32).
7. Press **"Compute the committed route"**: read the route order and timeline (ETA/arrival, waiting,
   service start, service duration, departure, the local service window and the lateness), then the
   BEFORE vs AFTER summary with saved time and distance, feasibility, violations and fingerprints.
   On a plan with no selection yet the honest answer is the documented
   ``409 no_first_stop_selected`` refusal, not an invented route.
8. Look at the map: START, FINISH and the stops drawn, the route order joined by **straight-line
   synthetic geometry** labelled as such, and visible tile attribution. Offline (as on this machine)
   expect the honest fallback instead: ``#map-notice`` explains that the tile map is unavailable
   while the timeline, summary, recommendation and selection stay fully usable.
9. Read "What this build does NOT do": traffic, side-of-road, turn-by-turn, geocoding, real routing
   and every route mode except SMART_ROUTE, straight from the API's capability report.
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

#: Every element id the work unit requires (tests and U16 depend on them).
REQUIRED_ELEMENT_IDS = (
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
)

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
    "return sign + parts.join(\" \");",
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

#: The honesty strings the workspace must contain, with the asset each one lives in.
HONESTY_STRINGS = {
    "demo_synthetic": ("DEMO / SYNTHETIC", "index.html"),
    "not_real_routing": ("not real routing", "index.html"),
    "advisory_not_applied": ("NOT an applied decision", "index.html"),
    "driver_decides": ("driver decides", "index.html"),
    "synthetic_geometry_label": ("synthetic straight-line geometry", "index.html"),
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

    def test_the_synthetic_geometry_label_is_next_to_the_map(self) -> None:
        for asset in (INDEX_HTML, MAP_JS):
            content = read_asset(asset)
            with self.subTest(asset=asset.name):
                self.assertIn("synthetic straight-line geometry", content.lower())
        self.assertIn("not road routing", read_asset(INDEX_HTML))

    def test_the_latency_notice_names_the_accepted_worst_case(self) -> None:
        notice = element_markup("latency-notice")
        # The HTML names the scale in full ("~50-enabled-stop portfolio scale"); the shorter
        # "~50-stop" of the brief is the same accepted figure, spelled out here.
        self.assertIn("~50-enabled-stop portfolio scale", notice)
        self.assertIn("about 8 seconds", notice)
        self.assertIn("computation_seconds", notice)
        self.assertIn("no", notice.lower())  # "has no background job queue"

    def test_the_computing_state_exists_and_is_driven_by_the_script(self) -> None:
        self.assertRegex(read_asset(INDEX_HTML), r'id="loading"')
        script = read_asset(APP_JS)
        self.assertIn("setLoading(true)", script)
        self.assertIn("setLoading(false)", script)
        self.assertIn('byId("loading")', script)

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
    def test_the_only_select_is_the_plan_chooser(self) -> None:
        body = read_asset(INDEX_HTML)
        self.assertEqual(len(re.findall(r"<select\b", body)), 1)
        self.assertRegex(body, r'<select[^>]*id="plan-select"')
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
        """No route, order, candidate or selection may be invented or written client-side."""
        script = read_asset(APP_JS)
        # The selection endpoints and the run-appending endpoint are U16 write paths: this unit
        # requests and renders only, so they are named in prose but never used as a request path.
        for forbidden in ('"/selection"', '"/optimize"', '"/reoptimize"', "api.selection",
                          "api.optimize"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)
        # The one write on the page is the documented open-or-create DEMO plan step.
        self.assertEqual(script.count('method: "POST"'), 1)
        self.assertIn("createDemoPlan", script)
        self.assertIn('body: "{}"', script)
        # A recommendation is reported as advisory and never applied.
        self.assertIn("data.applied_decision", script)
        self.assertIn("data.as_plan_state", script)
        self.assertIn("data.advisory", script)

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

    def test_the_history_panel_says_it_is_a_u16_placeholder(self) -> None:
        body = read_asset(INDEX_HTML)
        panel = body.split('id="history-panel"', 1)[0].rsplit("<section", 1)[1]
        self.assertIn("U16", panel)
        self.assertIn("/runs", panel)

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


if __name__ == "__main__":
    unittest.main()
