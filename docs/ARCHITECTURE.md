# RoutePilot — Architecture

This document explains **how** the product implements the decisions in
[`DECISIONS.md`](DECISIONS.md) under the requirements of
[`PRODUCT_SPEC_v2.md`](PRODUCT_SPEC_v2.md) (the current Source of Truth;
[`PRODUCT_SPEC.md`](PRODUCT_SPEC.md) v1 is historical and unchanged).

Precedence when documents disagree (v2 section 37): the **current product specification** plus
**explicitly approved later decisions** in the registry control future implementation. Where this
document and the registry disagree, the registry wins.

Stages 0, 1 and 1.5 implement the foundation, the demo scenario and the semantic migration to
RECOMMEND / MANUAL. **Stage 2 is implemented** (complete-route evaluation and optimizer, exhaustive
first-stop recommendation, fingerprints, leg cache, baselines, measured performance, the
complete-route demo narrative, the default objective of D35 - the complete elapsed route
duration, ranked by the owner's deterministic 5-tuple, and the exact incremental / delta
complete-route evaluator of Stage 2.2 U7). The **scale target is ~50 enabled service
stops** (D36): the exhaustive complete-route first-stop loop is performance-qualified at that scale,
and ~100 stops is an **engineering stress reference that is not performance-qualified**. The
incremental evaluator that D34 deferred is **implemented** (U7): it prices each candidate from the
base route's evaluated prefix, with identical results and about 2.5x lower latency at the portfolio
and stress scales and about 2.2x on the ~30-stop demo plan - the speedup is **not flat across
scales** (D37).

---

## 1. Layers and the dependency rule

```
web/      static HTML/CSS/JS workspace + Leaflet/OSM tiles (Stage 4 U15/U16)
                                                       -- served as bytes, never imported by Python
api/      transport: stdlib http.server + framework-agnostic service layer (Stage 4 U13/U14)
                                                       -> FastAPI later -- depends on core
storage/  SQLite repositories                    (Stage 3) -- depends on core, never the reverse
demo/     deterministic demo dataset + synthetic matrix + demo report -- depends on core
tools/    doctor, benchmark and developer utilities -- may inspect core, never imported by it
core/     domain model, time, engine              <-- depends on nothing but the stdlib
```

The rule is one-directional: **`core/` imports only the Python standard library.** It contains no
HTTP, no UI, no storage, no network, no vendor SDK, no Leaflet, no OSM. This is enforced by an
automated import check in the test suite (`tests/test_core_isolation.py`), not by convention.
`web/` is static: no Python module imports it, and it holds no business formula - every value it
shows is rendered from an API payload unchanged.

Two consequences that shape everything else:

- the optimizer can be replaced (OR-Tools, VRP, constraint solver) without touching the domain;
- travel time, geocoding and routing enter the domain only as **Protocols**, so the product is
  testable without network, browser or API keys.

## 2. Module map

```
core/
  model/
    ids.py             StopId, PlanId, RunId
    value_objects.py   Instant, DurationSec, GeoPoint, PlaceRef, DataProvenance
    service_window.py  WindowKind, WindowEndPolicy, ServiceWindow (value object, D28/D29)
    route_stop.py      GeocodeStatus, ServiceStatus, RouteStop
    route_plan.py      RoutePlan + invariant validation + order validation + fingerprints
    order_override.py  OrderConstraint, OrderOverrides (generic, D21)
    first_stop.py      FirstStopMode, SelectionSource, FirstStopState, RecommendationStatus,
                       FirstStopIntent, FirstStopRecommendation, FirstStopCandidate,
                       CandidateMetrics, CandidateDiagnostic
    cost_policy.py     CostComponent, ComponentStatus, RouteCostPolicy (D13)
    route_mode.py      RouteMode + ROUTE_MODE_STATUS registry (D19)
    solution.py        StopTimeline, TimelineFlag, Feasibility, Violation, RouteMetrics,
                       BaselineKind, RouteSolution
  time/
    tzdata.py          tzdata source detection, IANA zones, explicit fallback (D2/D12)
    tz.py              strict DST resolution (D3)
    timeline.py        ETA / waiting / service start / departure / lateness (spec §7)
  engine/
    providers.py       TravelMatrix / RoutingProvider / GeocodingProvider Protocols
                       + capability records (D15/D16)
    cost.py            weighted scoring over implemented components (D13, D31, D35)
    first_stop/
      evaluation.py    COMPLETE-ROUTE first-stop recommendation: every enabled candidate is
                       optimized (START -> candidate -> remaining stops -> FINISH), ranked by the
                       configured policy, rejected candidates kept with their violating stops
                       (Stage 2 U4; v2 sections 12-14, 20; D32)
    optimizer/
      cache.py         LegCache + CacheStats: deterministic, transparent, measured leg reuse
      route_problem.py frozen prepared problem (travel/distance tables, precomputed window table)
                       + the integer-second fast pass the hot loops use
      route_evaluation.py evaluate_order: THE compile of one order into a complete route,
                       FINISH leg included (v2 section 15); user_baseline_order; build_solution
      seed.py          constraint-aware greedy seed (complete-route criterion, D17)
      local_search.py  deterministic 2-opt / Or-opt improvement, lexicographic acceptance,
                       exact incremental evaluate-from-the-divergence pricing (U7)
      optimize.py      RouteProblem -> seed -> improvement -> authoritative evaluation
      solve.py         solver boundary: optimize + commit with both baselines
      route_fingerprint.py the committed route's own digest (v2 section 7, D4)
  validation/
    errors.py          error taxonomy (D26)
  repositories.py      the pure Protocol ports (plan, immutable run history, settings); declared in
                       `core/`, implemented under `storage/`, and `core/` never imports them
                       (Stage 3 U10/U11, D14/D38)

demo/
  dataset.py           deterministic ~30-stop demo plan (DEMO/SYNTHETIC, spec section 24)
  synthetic_matrix.py  deterministic synthetic travel matrix (DEMO/SYNTHETIC)
  scale_dataset.py     deterministic scale fixtures: the ~50-enabled-stop portfolio fixture (the
                       primary MVP target, D36) and the ~100-stop stress fixture
  report.py            complete-route demo report: `python -m demo.report`
  storage_roundtrip.py end-to-end storage round-trip demo: `python -m demo.storage_roundtrip`
                       (Stage 3 U12; in-memory only, real engine + real repositories)

storage/
  __init__.py          the storage error taxonomy (no I/O: importing it never opens a database)
  sqlite/
    database.py        connection helper + ordered, idempotent migration runner (Stage 3 U9)
    migrations/0001_init.sql  the approved DDL, byte-unchanged (docs/STORAGE_SCHEMA.md sections 2-6)
    route_plan_repository.py        plan + stop persistence, exact round-trip (U10)
    optimization_run_repository.py  append-only immutable run history (U11)
    app_settings_repository.py      the app_settings key/value store (U11)

api/
  http_server.py       the stdlib ThreadingHTTPServer transport: the route table, request parsing,
                       response writing, static-asset serving, the pure static-path resolver and the
                       documented error-mapping table; no business formula (Stage 4 U13/U14)
  services.py          the framework-agnostic application layer over the repository Protocols: no
                       HTTP type, no status code and no JSON, so a future transport can replace
                       http_server.py without touching it (Stage 4 U13/U14)
  serialization.py     domain -> JSON-ready payloads; the one place the API's serialisation contracts
                       (UTC instants, integer seconds, real booleans, enum strings, fingerprints) are
                       decided (Stage 4 U13/U14)
  map_configuration.py the map configuration the workspace reads over the API: the approved
                       tile_url / tile_attribution / tile_max_zoom settings plus the Leaflet library
                       location, each with its documented default and its source (Stage 4 U15;
                       D15/D39(c))
  serve.py             the command-line entry point "python -m api.serve": loopback bind by default,
                       --port / --db / --static-root / --quiet, and the gitignored default database
                       var/routepilot.db (Stage 4 U13)

web/
  index.html           the workspace markup: the DEMO/SYNTHETIC banner, the latency notice, the plan
                       chooser, the override controls, the map, the recommendation / alternatives /
                       rejected candidates, the route timeline, the BEFORE vs AFTER summary and the
                       read-only run history (Stage 4 U15/U16)
  styles.css           the workspace stylesheet, served as-is (Stage 4 U15)
  app.js               the workspace controller: calls the documented endpoints and renders payload
                       values unchanged - it computes no route, metric, rank or saving
                       (Stage 4 U15/U16)
  map.js               the Leaflet map controller: draws START / FINISH / stops and the route order as
                       labelled synthetic straight-line geometry from API payloads, and reports an
                       honest notice when the library, the tiles or the configuration fail
                       (Stage 4 U15; D15/D39(c))

tools/
  doctor.py            environment health (D12)
  benchmark_optimizer.py  exhaustive first-stop benchmark over the ~30-stop demo plan, the
                       ~50-stop portfolio fixture and the ~100-stop stress reference, with the
                       per-fixture target that applies to each (D34/D36)
  workspace_fingerprint.py deterministic working-tree digest
  isolation_check.py   the `core/` import-boundary check
```

Stage status: `core/model`, `core/time`, `core/validation` and `core/engine/providers.py` are
Stage 0; `core/engine/cost.py` and `demo/` are Stage 1, whose semantics were then migrated off the
revoked AUTO model (D4/D32). `core/engine/optimizer/`, `core/engine/first_stop/evaluation.py`,
`demo/report.py` and `tools/benchmark_optimizer.py` are **Stage 2**. `storage/` (the migration runner
and the three SQLite repositories), `core/repositories.py` (their pure Protocol ports) and
`demo/storage_roundtrip.py` are **Stage 3** (U9–U12, D38), so storage code now exists; `core/` still
contains none. **Stage 4 units U13–U16 are delivered** (D39): U13 is the stdlib transport, the
framework-agnostic service layer, the JSON contracts and the error mapping, U14 the
recommendation / selection / route / optimize / run-history endpoints with the synchronous
single-flight contract, U15 the static `web/` workspace and U16 the override controls, the read-only
run history and the integration guard. **U17 is this documentation and acceptance unit** and adds no
code, test or spec file. `core/` contains **no** API, UI or storage code: the API, the workspace and
the repositories all depend on `core/`, never the reverse.

## 3. Domain model

### 3.1 Value objects

| Type | Meaning |
|---|---|
| `Instant` | timezone-aware `datetime`, always stored in **UTC** |
| `DurationSec` | `int` seconds |
| `GeoPoint` | `latitude`, `longitude` — coordinates only, never routing metadata |
| `PlaceRef` | labelled location + point; used for START and FINISH, which are **not** stops |
| `ServiceWindow` | `window_kind` + optional local start/end (D28) |
| `DataProvenance` | `DEMO_SYNTHETIC` or `REAL_ROUTING` — travels with every solution |

`ServiceWindow` rules (`__post_init__`, tested):

- `fixed` → `start_local` and `end_local` both required, and `start_local < end_local`;
- `unrestricted` / `unknown` → both are `None`;
- `window_end_policy` may only be set on a `fixed` window; `None` means "inherit the plan default"
  (D29);
- any other combination raises `InvalidServiceWindowError`.

`unknown` and `unrestricted` both mean "no waiting, no lateness, no invented hours", but they are
**not** the same state: `unknown` marks missing data and is surfaced to the driver, `unrestricted`
states a known 24/7 access. Neither is ever turned into guessed business hours.

### 3.2 `RouteStop`

Spec §17 fields: `id`, `raw_address`, `normalized_address`, `latitude`, `longitude`,
`service_duration`, service window, `priority`, status, `enabled`, `notes`; plus `input_position`
(v2 §25).

The three state fields are independent (D20):

- `geocode_status` — `pending | resolved | ambiguous | failed` (address → coordinates);
- `service_status` — `pending | in_progress | served | failed | skipped` (route execution);
- `enabled` — a disabled stop is excluded from optimization regardless of `service_status`.

`input_position` is a fourth, different kind of field (D33, v2 §25/§30): immutable **input-order
provenance**, not route order. It is non-negative, unique within a plan, gaps are allowed, and
neither optimization, route reordering, order overrides nor drag/reorder may change it. The
user-facing BEFORE baseline is built from it, and a newly appended stop takes
`max(input_position) + 1` so historical positions stay stable.

Validated invariants: `geocode_status == resolved` requires coordinates; a `fixed` window requires
a resolved customer (a stop with an unknown address and a fixed window is rejected rather than
half-modelled); `input_position` is an integer `>= 0` and unique within the plan.

### 3.3 `RoutePlan`

```
id, timezone (IANA), departure (PlaceRef START), departure_time (Instant),
finish (PlaceRef FINISH), route_mode, window_end_policy (D29),
first_service_stop (FirstStopIntent), order_overrides (OrderOverrides),
cost_policy, stops, default_service_duration
```

Structural invariants (D10):

- **I1** START is a `PlaceRef`, never a `RouteStop`; `validate_order()` rejects any order containing
  the departure location, and START never appears in timelines.
- **I2** FINISH is a `PlaceRef` and is never part of the optimized order; it may not appear in it.
- **I3** a driver-selected first stop stays first through any optimization and is never replaced by a
  recomputation or a changed recommendation (enforced by the optimizer: `with_first_stop` fixes the
  first position and no local-search move may touch it).
- **I4** in RECOMMEND mode the system always returns ranked recommendations when a feasible candidate
  exists, but a **committed** first service stop — and therefore a committed working route — requires
  an explicit driver choice. `awaiting_first_stop_choice` is a valid state, not an error.
- **I5** a recommendation never implies selection: `recommended_stop_id` and `selected_stop_id` are
  separate fields with separate lifecycles.
- **I6** a candidate's previewed complete-route figure must come from the same objective and
  optimizer that builds the final route (preview and commit never diverge).

`validate_order(order)` is pure domain validation (not a solver) and guarantees spec §27.3/§27.4/
§27.17: exactly the enabled stops, each exactly once, no duplicates, no unknowns, no disabled
stops, no START, no FINISH.

The plan holds its stops in **input order** (`input_position`), and
`RoutePlan.user_baseline_order()` is the user-facing BEFORE route of v2 §30:
`START -> enabled stops sorted by input_position -> FINISH`. Disabled stops are omitted without
renumbering the remaining positions.

### 3.4 First stop: the driver's decision vs the engine's recommendation (D4/D11/D32)

```
FirstStopIntent          mode, selected_stop_id, selection_source, pinned   # persisted decision
FirstStopRecommendation  recommended_stop_id, ranked top-K, status,         # derived, recomputable
                         resolved_at, inputs_fingerprint, diagnostics
```

The optimizer recommends; the driver decides. `awaiting_first_stop_choice` is the normal starting
state in RECOMMEND mode, and a recommendation may exist while nothing is selected:

```
recommended_stop_id = S73
selected_stop_id    = None
state               = awaiting_first_stop_choice     # valid, expected
```

Validated combinations (D6):

| Driver action | mode | selection_source | pinned | state |
|---|---|---|---|---|
| no choice yet | `recommend` | — | `false` | `awaiting_first_stop_choice` |
| no choice yet | `manual` | — | `false` | `awaiting_first_stop_choice` |
| pressed "start from this stop" on the recommendation | `recommend` | `accepted_recommendation` | `true` | `first_stop_selected` |
| chose another stop | `recommend` | `manual_choice` | `true` | `first_stop_selected` |
| chose directly | `manual` | `manual_choice` | `true` | `first_stop_selected` |

Model rules: a selection requires provenance (`selection_source`), provenance requires a selection,
nothing selected cannot be pinned, MANUAL mode only accepts `manual_choice`, and **a selected first
stop is always pinned** - `selected_stop_id != None` with `pinned = false` is invalid (v2 §5).
Cancelling the selection returns to `awaiting_first_stop_choice` (v2 §6); the domain never chooses a
stop on the driver's behalf.

`pinned_via` and the old `auto_recommendation` / `driver` values are gone: with no automatic
application there is no "Lock" action left for them to describe.

Plan-level states (derived, never stored): `awaiting_first_stop_choice`, `first_stop_selected`,
`no_active_stops`, `empty_plan`. Recommendation outcomes (D9, distinct reasons rather than a bare
`None`): `recommended`, `no_fully_feasible_route` (v2 §14), `no_active_stops`, `empty_plan` - and,
from Stage 2, every rejected candidate with its violating stops and reasons (§5.1).

### 3.5 Order overrides (D21)

`OrderOverrides` holds a tuple of `OrderConstraint(kind, stop_id, position)`:

- `kind='first_stop'` — implemented; at most one instance, and when present it must mirror the
  driver's `selected_stop_id` (validated, so the two representations cannot silently diverge). An
  absent override is not a conflict: the selection is authoritative;
- `kind='position'` — reserved for future drag/reorder. The domain can represent it, but this stage
  rejects it with `UnsupportedConstraintError` instead of pretending to honour it.

No constraint solver is built yet.

### 3.6 Cost policy (D13 amended, D31 sensitivity, D35 default)

`RouteCostPolicy` = name + `weights: Mapping[CostComponent, float]` + per-component
`ComponentStatus` + a `provisional` marker. An empty policy is the neutral baseline and ships no
weights: invented numbers would become product truth by accident.

Component statuses make capability honesty machine-checkable (D16):

- `implemented` — engine code computes and scores it today: `travel_time`, `waiting_time`,
  `distance`;
- `planned` — declared in the spec, not implemented: early/late penalties, soft-window violation
  penalty, priority, finish direction, and `first_stop_remaining_route_weight`;
- `requires_provider` — cannot be computed without information the domain does not have.
  `wrong_side_penalty`, `u_turn_penalty`, `backtracking_penalty` are `requires_provider`
  (road geometry / direction), which is what prevents fake side-of-road logic from creeping into
  the objective (spec §11).

A weight may only be assigned to an `implemented` component; anything else raises
`UnsupportedFeatureError`. Scoring itself is one function, `core.engine.cost.score_breakdown`, so
the objective stays visible and testable.

Two weighted policies exist, and only one of them is a default:

- **`smart_route_elapsed_v1` — the default SMART_ROUTE objective (D35)**, built by
  `core.model.cost_policy.smart_route_elapsed_policy()` (`travel_time = 1`, `waiting_time = 1`,
  `provisional = False`). It measures the **complete elapsed route duration** = travel + waiting +
  service, equivalently the estimated FINISH arrival time for a fixed departure. Service time is
  deliberately not a component - every candidate of one plan serves exactly the same stops, so
  `total_service_time` is constant and is reported rather than scored - which is why travel and
  waiting at 1:1 **is** the elapsed-duration objective and not a hidden weight. The default
  additional waiting preference is zero, and the optimizer already accepts moves on
  `(violations, elapsed seconds)`, so ranking and optimization measure the same quantity.
- **`demo_provisional_v1` — the non-default waiting-preference sensitivity study (D31)**, built by
  `demo_provisional_policy()` (`travel_time = 1`, `waiting_time = 2`, marked `provisional`). It is
  **not** the shipped objective any more; it exists so the report can show what a non-zero waiting
  preference would do.

Since Stage 2 scoring is applied to the **complete route's** measured breakdown:
`core.engine.first_stop.evaluation.score_of` weights the candidate's complete travel seconds,
complete waiting seconds and metric distance, and `distance` keeps a weight of 0 in both policies.
The objective is a **reported** figure; the order comes from the deterministic ranking key of §5.1.

## 4. Time model

- Storage and all arithmetic in UTC `Instant`; each plan carries one IANA zone for local
  presentation and for resolving local wall-clock window times.
- `tzdata.py` detects the IANA database source: `package` (`tzdata` installed), `system`
  (TZPATH/`PYTHONTZPATH`), or `missing`. It reports the IANA version when known and always carries
  the exact fix command. Nothing is activated silently; `activate_system_tzif_fallback()` exists for
  explicit dev/test use and is never called from production paths.
- `tz.py` resolves a local window time strictly (D3, spec §21):

```
resolve_local(window_local_time, service_date, tz):
    fold0, fold1 = wall time with fold=0 / fold=1
    if round_trip(fold0) != wall_time  -> NonexistentLocalTimeError (DST gap)
    if fold0.utcoffset() != fold1.utcoffset() -> AmbiguousLocalTimeError (carries both candidates)
    return the single unambiguous instant
```

  Ambiguous local times are **not** auto-resolved: the error carries both candidate instants so a
  future explicit disambiguation call can be built on top of it without changing the error contract.
- The service date is the local date of the estimated arrival in the plan's zone.
- Overnight windows (`end_local <= start_local`) are rejected in Stage 0 (Open item 1 in
  `DECISIONS.md`).

## 5. Timeline semantics (spec §7, D29)

For each stop, in order, starting from `departure_time` at the departure location:

```
travel_time          = matrix(previous_location, stop_location)
estimated_arrival    = departure_from_previous + travel_time
fixed window:  waiting_time = max(0, window_start - estimated_arrival)
               service_start = max(estimated_arrival, window_start)   # never before opening
unrestricted/unknown: waiting_time = 0, service_start = estimated_arrival
service_duration     = stop.service_duration or plan.default_service_duration (else error)
estimated_departure  = service_start + service_duration
start_lateness       = max(0, service_start - window_end)
finish_overtime      = max(0, estimated_departure - window_end)
lateness             = finish_overtime                     # under service_finish_before_end
                     | start_lateness                      # under service_start_before_end
feasibility          = infeasible iff lateness > 0, else feasible
```

The arithmetic lives in `compute_stop_timeline`, used both by the chained `compute_timeline` and by
first-stop candidate evaluation, so a candidate's first leg and a real route leg cannot drift apart.

Key semantics:

- the meaning of the window **end** is explicit (D29): with the default `service_finish_before_end`
  a stop that would finish after closing is infeasible; with `service_start_before_end` beginning
  service in time is enough and the overrun is recorded as `finish_overtime` information only;
- `lateness` is the miss measured under the applied policy, so `lateness > 0` always means the same
  thing: the stop cannot be served within its permitted window;
- a **hard** window miss produces an explicit `Violation(kind='time_window_infeasible')` attached to
  the stop and to the solution, whose status becomes `has_infeasible_windows` (D13 amendment);
- `window_kind='unknown'` raises no violation — it raises a timeline flag (`window_unknown`) so the
  driver sees "hours unknown" instead of silently treated-as-open.

### 5.1 First-stop recommendation: COMPLETE routes, not first legs (Stage 2, v2 §12-§14, §20; D32)

`core.engine.first_stop.evaluation.evaluate_first_stop_candidates` evaluates **every enabled stop**
as a first-stop candidate and ranks the resulting **complete routes**:

```
START -> candidate -> optimized remaining enabled stops -> FINISH      (FINISH leg included)
```

The pipeline, once per candidate, is exactly the committed-route pipeline (I6 - preview and commit
never diverge):

```
build_problem(plan, travel_matrix, first_stop_id=candidate)   # frozen prepared problem
  -> greedy seed (complete-route criterion)  -> local improvement  -> evaluate_order (authoritative)
  -> score_breakdown over the complete route's measured travel/waiting
```

* **Candidate set**: exhaustive - one optimizer run per enabled stop, in `input_position` order,
  with no prefilter, no shortlist and no fixed-K cut (v2 §20, D34). `candidates_evaluated` is always
  `ranked + rejected`, and the report is rejected outright if it is not.
* **Metrics** (v2 §12): first-leg travel, first-stop ETA, first-stop waiting, first-stop service
  start, complete travel, complete waiting, total service, complete duration, FINISH arrival,
  violating stop ids and the objective breakdown. Every complete figure is the whole route's,
  FINISH leg included (v2 §15).
* **Feasibility is a property of the complete route** (v2 §14, D32). A candidate whose remainder
  misses a hard window is **not ranked**; it is kept in `rejected` with the violating stop ids and
  one `CandidateDiagnostic` per violating stop, keyed by `candidate_stop_id` so a caller can ask a
  rejected candidate for *its own* reasons. With no fully feasible candidate the status is
  `no_fully_feasible_route` and there is no recommended stop - never a fabricated winner.
* **Ranking**: the owner's deterministic 5-tuple (D35) - complete elapsed duration, complete travel
  time, complete waiting time, `input_position`, `stop_id` - all from the complete-route metrics, the
  FINISH leg included. The weighted score is a **reported** figure and is deliberately not a key
  component, so no weighting is hidden in the tie-break; `total_service_time` is identical for every
  candidate of one plan and is therefore reported, never scored (it cannot separate them). With the
  default 1:1 weights the score equals complete travel + waiting, i.e. the complete duration minus
  that constant service time.
* **Cache and cost**: all candidates share one `LegCache`, so every leg is priced once and the
  reuse is measured (`cache_stats`) instead of claimed. The *prepared problem* is deliberately not
  shared: `with_first_stop` builds a fresh problem per candidate, which is why the loop costs what
  D34 and D36 record. The objective and the shipped weights are documented in §3.6/D35.
* **Nothing is applied**: `plan.first_stop_state` stays `awaiting_first_stop_choice`; a
  recommendation is not a selection (D4/D32/I5), and `recommended_stop_id`/`selected_stop_id` remain
  separate fields with separate lifecycles.

**Implemented (Stage 2.2 U7):** the incremental / delta complete-route evaluator that D34 deferred.
It prices each candidate by resuming from the base route's own evaluated state at the move's
divergence and walking only the runs the move reorders, plus the FINISH leg, so the reused prefix is
bit-identical to recomputing it. It removes about 60% of the measured latency at the portfolio and
stress scales and about 55% (about 2.2x, not 2.5x) on the ~30-stop demo plan
(`core/engine/optimizer/local_search.py`: `PreparedSearch`, `prepare_prefix_states`,
`move_divergence`, `move_index_runs`), and the reference full pass stays in place and is the
comparison baseline the tests use. No prefilter and no approximation was authorized or introduced,
and under D36 this is an engineering-scale improvement, not a gate on the portfolio MVP.

### 5.1.1 Scale and measured performance (D36, amending D34)

The primary MVP performance target is **approximately 50 enabled service stops**. Two fixtures are
built by the **same** deterministic generator in `demo/scale_dataset.py`, so the smaller one is a
scale subset of the larger and neither can drift into a different shape:

| Fixture | Builder | Stops | Enabled | Target |
|---|---|---|---|---|
| portfolio (**primary MVP target**) | `build_portfolio_plan()` | 55 | **50** (`PORTFOLIO_ENABLED_STOP_COUNT`) | preferred ≤ ~3 s, acceptable ≤ ~5 s, **reported**; asserted guard: the generous owner-accepted bound of D34 |
| stress (engineering reference) | `build_scale_plan()` | 100 | 97 | v2 §20 numbers **reported only**; **not performance-qualified**, the ≤ ~5 s figure is **not an MVP gate** |

The portfolio fixture's enabled count is a property of its own deterministic disabled policy
(`PORTFOLIO_DISABLED_EVERY = 10`), asserted on every call to `build_portfolio_plan()`, and every
label prints the **enabled** count next to the total - a fixture with materially fewer enabled stops
is never called a "50-stop" fixture. The ~100-stop default and its tests are untouched.

`tools/benchmark_optimizer.py` measures all three fixtures (demo plan, portfolio, stress) and
attaches a **profile** to each, so the same measurement is read under the target that applies to it:
every profile asserts the same **generous owner-accepted regression bound** of D34 (~150 s) - the
primary MVP scale included - while the reported v2 §20 targets are printed with their honest verdict,
so no scale is gated on a missed engineering target and none is left without a regression guard.
`demo/report.py` prints a `SCALE AND PERFORMANCE` block covering the same three
scales, with the owner's D36 statement verbatim, the enabled counts, and the portfolio fixture's
**live measured** number - reported as it is. It is **outside** the ≤ ~5 s target and just above the
owner's ≤ ~8 s "good enough" target (~8.0-8.5 s warm on this development machine, ~0.17 s per
candidate, every candidate hitting the deterministic per-candidate evaluation ceiling). Stage 2.2 U7
replaced the per-move full re-evaluation with an exact incremental / delta evaluator (prefix reuse
plus the FINISH leg, `PreparedSearch` in `core/engine/optimizer/local_search.py`), which made the
portfolio and stress scales about **2.5x** faster and the smaller ~30-stop demo plan about **2.2x**
faster (50 enabled stops: 21.1 s -> 8.0-8.5 s warm; 97 enabled stops:
74.8 s -> 29.9-31.1 s; the 31-enabled-stop demo plan: 5.7 s -> 2.5-2.7 s, all measured on this machine
with the same benchmark) without changing which routes are priced, in which order, or which one is
accepted - so the remaining gap needs further work inside the complete-route evaluations themselves
(one route pass per candidate move), not a prefilter, a shortlist, an approximate ranking or any
quality-degrading cut - none of which is authorized. The ~50-stop scale decision introduces **no hard
validation limit**: the domain and the generator stay able to evolve beyond 50 stops (D18).

### 5.2 Demo data, the demo narrative and its calibration (Stage 1 + Stage 2 U5)

`demo/dataset.py` builds a deterministic ~30-stop demo plan: departure 04:00 Europe/Moscow, 31
enabled stops + 1 disabled stop, mixed window kinds (`fixed`, `unrestricted`, `unknown`), mixed
opening times (08:00, 08:30, 09:00, 10:00), priorities, one stop without a service duration, and
short services of 4-5 minutes plus the 10-minute plan default for the stop whose duration is
unknown. `demo/synthetic_matrix.py` provides the synthetic matrix (1 coordinate
degree = 1 hour) carrying `DataProvenance.DEMO_SYNTHETIC` and **empty** provider capabilities.

The fixture is **calibrated**, and this is the part that makes the demo demonstrate v2 §33 instead
of an artefact:

* the 04:00 → 08:00 gap is 240 minutes while the service area spans only ~7 to ~132 synthetic
  minutes, so **no** candidate can drive the gap away: every complete route contains real waiting
  and the ranking is decided by the whole route, not by one candidate happening to arrive at 08:00;
* services are short so the whole 31-stop enabled route fits inside one working day. With long
  services the
  day cannot be served at all inside the closing times, and the optimizer legitimately spills the
  route into the next day's windows - which is a different (and misleading) demonstration;
* every fixed window except one closes at **19:00 or 20:00**, so a route that wastes the morning is
  a *worse* complete route, not an infeasible one;
* **one customer is the feasibility bottleneck**: `S32-EARLY-CLOSE` (1h36m from the warehouse)
  closes at 10:00, so a complete route can only serve it in its first two hours. The 26 ranked
  candidates reach it between 08:00 and 09:56; five candidates (`S11-OPEN-0900`,
  `S12-OPEN-0900B`, `S07-OPEN-1000`, `S05-FARTHEST`, `S27-OPEN-1000B`) reach it at 12:27-13:36 and
  are **rejected** with that stop named as violating (v2 §14, D9). Without it every candidate would
  be feasible and the report would have no rejection diagnostics to print at all;
* the same deadline makes the **USER baseline infeasible**: the input order (a plausible
  nearest-first work list) serves the bottleneck 17th and arrives at 13:23. That is not avoidable by
  moving the deadline - the input order serves the bottleneck *after* the five rejected candidates
  do, so a deadline late enough to keep the input order feasible also makes every candidate feasible
  (measured: 08:00-13:30 → 31 feasible / 0 rejected). The demo therefore shows both the v2 §14
  rejection diagnostics **and** an infeasible BEFORE route that the optimizer turns into a feasible
  AFTER route;
* the plan's `input_position` order (its immutable input-order provenance, v2 §30) is a plausible
  **nearest-first work list**, so the USER baseline is a route a driver could really have entered.

What the demo shows at 04:00 under the default elapsed-duration objective (D35) - all numbers are
printed by the report and pinned by `tests/demo/test_report.py`:

| candidate | first leg | complete waiting | complete duration | rank |
|---|---|---|---|---|
| `S23-UNKNOWN-HOURS2` (recommended) | 37m | 2h37m | 10h49m | #1 of 26 |
| `S01-NEAR` (nearest) | 7m | 3h53m | 11h42m | #20 of 26 |
| `S05-FARTHEST` (farthest, opens 10:00) | 2h12m | 3h48m | 12h56m | REJECTED |

The nearest candidate has the cheapest first leg **and** the least complete driving (5h34m, the
minimum of the ranking - `S02-NEAR2` ties it), and still ranks #20 of 26, because starting there
means waiting 3h53m before the first customer opens. The farthest candidate loses for the opposite
reason: opening at 10:00, it cannot serve the early-closing customer at all and is rejected rather
than ranked. The recommendation is neither. The report derives those comparisons from the ranking it
claims they are about (`complete_travel_rank`, `fewest_driving_ids`), so a superlative such as
"least complete driving" is computed for the run that printed it.

Changing the departure time changes the answer - 04:00, 05:00 and 06:00 all recommend
`S23-UNKNOWN-HOURS2`, at 07:00 `S14-PRIORITY-2` takes over, and at 08:00 the nearest customer
`S01-NEAR` becomes the strongest complete route. Complete-route quality decides, not the first leg.

The same table-shaped audit is printed for every sweep hour as `OBJECTIVE ALIGNMENT` (D35): per
departure hour, the recommendation the **previous** default produced (the non-default D31
provisional policy ranked with the pre-D35 key) next to the new elapsed-duration recommendation with
its FINISH, complete travel, waiting, service and feasibility. At 04:00-06:00 the two differ
(`S25-ON-OPENING` / `S25-ON-OPENING` / `S08-UNKNOWN-HOURS` → `S23-UNKNOWN-HOURS2`); at 07:00 and
08:00 they agree. That table is the audit trail of the objective change, and the shipped answer is
the elapsed-duration one - nothing was tuned to preserve the previous winner.

`demo/report.py` (`python -m demo.report`) prints that story from the engine's own objects: plan and
status, the recommended candidate with its complete-route metrics, the top-5 ranking, the
recommendation's complete route stop by stop, the nearest/farthest complete outcomes with their
ranks and an explicit why-it-wins comparison, USER vs OPTIMIZED vs the internal ALGORITHM baseline,
the departure sweep, the **objective-alignment** audit of the D35 change (the previous D31
provisional recommendation against the new elapsed-duration one, per departure hour, with FINISH,
complete travel, waiting, service and feasibility), the **non-default** sensitivity study of what a
*non-zero* waiting preference would do (D31, including the degenerate 1:1 case, which is numerically
the shipped objective), the rejected-candidate diagnostics (grouped by candidate, with violating
stop ids), both fingerprints, the work counters, the measured ~30-stop runtime and the
**scale/performance block** (D36): the ~30-stop demo plan, the ~50-enabled-stop portfolio fixture -
the primary MVP scale target, with its own live measured number - and the ~100-stop stress reference,
which is relabelled as future scale / **not performance-qualified** while keeping its honest recorded
measurement and the owner-accepted bound (D34). The report is deterministic (the only non-deterministic lines
are the measured runtimes, which are labelled and only printed when measured), it is labelled
DEMO/SYNTHETIC everywhere, and it never presents synthetic travel as road routing. Its exhaustive
evaluation is memoized inside the module, so the report and the demo tests evaluate each distinct
plan (and each sensitivity policy) once.

Baselines (v2 §30, D22): **USER** = `START → enabled stops in input_position order → FINISH` (the
user-facing BEFORE), **OPTIMIZED** = the optimized route around the driver's selection (AFTER), and
**ALGORITHM** = the greedy seed before local improvement, kept internal and never shown as BEFORE.
The report computes these from a **copy** of the demo plan carrying a first-stop selection, because
a committed route requires the driver's decision (I4/D32); the demo plan itself is never mutated.

## 6. Errors vs violations (D26)

| | Meaning | Represented by |
|---|---|---|
| Error | invalid input/configuration; the request cannot be answered | exception hierarchy in `core/validation/errors.py` |
| Violation | valid input whose outcome is infeasible | data (`Violation`) carried by the timeline/solution |

Hierarchy (mirrors `core/validation/errors.py` exactly):

```
RoutePilotError
├── ValidationError
│   ├── InvalidTimezoneNameError          (not a syntactically valid IANA identifier)
│   ├── UnknownTimezoneError              (well-formed, not in the database)
│   ├── DSTValidationError
│   │   ├── NonexistentLocalTimeError     (DST gap)
│   │   └── AmbiguousLocalTimeError       (carries both candidate instants)
│   ├── InvalidServiceWindowError
│   ├── InvalidRouteStopError
│   ├── InvalidRoutePlanError
│   ├── InvalidCostPolicyError            (incomplete or dishonest policy)
│   ├── InvalidOrderError
│   ├── StopNotGeocodedError              (never guess an address)
│   ├── MissingServiceDurationError       (never invent service time)
│   └── UnsupportedFeatureError           (declared, not implemented - D16)
│       ├── UnsupportedConstraintError
│       └── UnsupportedRouteModeError
└── ConfigurationError
    └── TimezoneDataMissingError          (carries the exact install command)
```

A missed hard window is a **violation**, not an exception: the route is still built so the driver
can see the problem, but it can never be reported as a normal valid route.

## 7. Providers (D15/D16) — interfaces only in Stage 0

`core/engine/providers.py` declares `TravelMatrixProvider`, `RoutingProvider`, `GeocodingProvider`
and a `ProviderCapabilities` record (`side_of_road`, `traffic`, `one_way`, `turn_by_turn`). The
domain consumes only these Protocols. The demo map is Leaflet + OSM tiles behind configuration with
visible attribution; `core/` never references them.

## 8. Testing policy

- `unittest`, deterministic, offline: no network, no browser, no LLM, no paid API.
- Test-to-spec mapping:

| Spec §27 | Where |
|---|---|
| 1, 2 | `tests/model/test_route_plan.py` (model invariants), timeline tests |
| 3, 4, 17 | `validate_order` tests (exactly-once, no disabled, no loss/duplication) |
| 5, 6 | `tests/model/test_first_stop.py` (the driver's choice; provenance records how it was made, never by the engine) |
| 7–10 | `tests/engine/test_first_stop_evaluation.py` (the *recommendation*), `tests/time/test_timeline.py` |
| 11–14 | `tests/engine/test_first_stop_evaluation.py` (complete-route feasibility, ranking, rejected candidates), `tests/model/test_first_stop.py` |
| 15, 16 | `tests/time/test_tz_strict_validation.py` |
| 18 | `tests/engine/test_optimizer.py` (local-search monotonicity, fingerprints), `tests/engine/test_optimizer_evaluation.py` (complete-route metrics, baselines) |
| 19 | `tests/engine/test_optimizer_performance.py`, `tests/engine/test_optimizer.py` (demo-plan 31-enabled-stops measurement) |
| 20 | `tools/benchmark_optimizer.py` (demo plan + ~50-stop portfolio fixture + ~100-stop stress reference, all with no prefilter), `tests/engine/test_optimizer_performance.py`, `tests/tools/test_benchmark_optimizer_labels.py` |
| 21 | `tests/storage/` (migrations, plan/stop round-trip, immutable run history, settings, hand-edited-row failures), `tests/demo/test_storage_roundtrip.py` and `python -m demo.storage_roundtrip` (Stage 3, U9–U12) |
| 24, 33 | `tests/demo/test_dataset.py` (fixture shape and calibration: 31 enabled + 1 disabled stop, the early-closing bottleneck, short services and the 10m default, determinism), `tests/demo/test_report.py` (the four §33 claims: nearest/farthest are not the recommendation, the departure sweep changes it, complete-route quality decides; the computed least-driving comparison; the rejected-candidate diagnostics; the D35 objective-alignment audit; the D31 weight sensitivity; the D36 scale/performance block and its enabled counts), `tests/demo/test_synthetic_matrix.py` |
| — | `tests/test_core_isolation.py` (D1/§22), `tests/tools/test_doctor.py` (D12), `tests/engine/test_cost.py` (D13/D31/D35), `tests/model/test_cost_policy.py` (D13/D16/D31/D35), `tests/tools/test_workspace_fingerprint.py` |

- The suite prints a warning and uses a detected system TZif tree when `tzdata` is unavailable, so
  the DST tests are meaningful on an offline machine; the real fix remains `pip install tzdata`.
- The incremental evaluator's exactness is proven twice: the default suite compares every move of a
  whole generated neighbourhood (and the whole search's decisions) against the reference full pass,
  and `tests/engine/test_optimizer_performance.py` gates the demo-plan and portfolio comparisons
  behind `ROUTEPILOT_SLOW_TESTS`, alongside a deliberate corruption that must make the comparison
  fail - so the equivalence gate cannot be vacuous.
- The demo-scale exhaustive evaluation costs several seconds per plan. `demo/report.py` memoizes the
  evaluation, the departure sweep, the D35 objective-alignment evaluations, the D31
  weight-sensitivity policies, the recommendation preview and the ~50-stop portfolio evaluation, so
  the report and the demo tests evaluate each distinct plan (and each sensitivity policy) once
  instead of once per assertion. `tests/demo/test_report.py` still costs about a minute and is the
  slowest module in the suite; that is the measured cost of evaluating 31 complete routes
  exhaustively, not a prefilter or a shortcut.
- The **~50-stop portfolio measurement** and the **~100-stop stress loop** are heavy, so they are
  opt-in behind `ROUTEPILOT_SLOW_TESTS` (`tests/engine/test_optimizer_performance.py`, and the CLI
  tests of `tests/demo/test_report.py`) and are never asserted at an exact wall-clock second: the
  measured figure is **reported** and every measured scale - the portfolio scale included - is
  guarded by the generous owner-accepted regression bound of D34 (~150 s, D36), not by the reported
  ≤ ~3 s / ≤ ~5 s engineering targets. The default fast suite pins the
  labels, the enabled counts and the fixture determinism only.

## 9. Extension points

| Stage | Extension |
|---|---|
| 0 ✅ | foundation: docs, domain skeleton, time layer, strict DST, error taxonomy, doctor, tests, storage schema proposal |
| 1 ✅ | cost scoring over implemented components, deterministic demo dataset (~30 stops), synthetic matrix, 04:00 / 08:00 scenario, candidate evaluation, numeric demo report |
| 1.5 ✅ | semantics migration off the revoked AUTO model: RECOMMEND/MANUAL, recommendation vs driver decision, `awaiting_first_stop_choice` (D4–D11, D32) |
| 2 ✅ | complete-route evaluation (FINISH leg included) + deterministic optimizer (greedy seed, 2-opt/Or-opt improvement, leg cache) + **exhaustive** complete-route first-stop recommendation with top-K and rejected-candidate diagnostics + recommendation and route fingerprints + the three baselines + the complete elapsed-duration default objective with the owner's deterministic 5-key ranking (D35) + the scale decision (**~50 enabled stops is the primary MVP target**, D36) with the portfolio fixture and the ~100-stop stress benchmark + the complete-route demo narrative (U1–U6, U6b) + the exact incremental complete-route evaluator (U7) |
| 2.2 ✅ | the **exact incremental / delta complete-route evaluator** (U7): prefix reuse from the base route's evaluated state at the move's own divergence, plus the FINISH leg, with the reference full pass kept intact as the comparison baseline and an opt-in slow equivalence gate. Same moves, same order, same accept/reject decisions, same `evaluations` ceiling - about 2.5x lower latency at the portfolio and stress scales and about 2.2x on the ~30-stop demo plan (D37) |
| 3 ✅ | SQLite storage behind the approved schema (D38, U9–U12): `storage/sqlite/migrations/0001_init.sql` (the approved DDL, byte-unchanged) + `storage/sqlite/database.py` (connection helper, ordered and idempotent migration runner) + `core/repositories.py` (the pure Protocol ports) + `storage/sqlite/route_plan_repository.py` (plan/stop persistence, exact round-trip), `storage/sqlite/optimization_run_repository.py` (append-only immutable run history) and `storage/sqlite/app_settings_repository.py` (settings key/value store) + the end-to-end round-trip demo `python -m demo.storage_roundtrip`. No ORM, no new dependency, `core/` imports no storage module, no database file committed |
| 4 ✅ | API transport + web workspace (D39, U13–U16): `api/http_server.py` (the stdlib `http.server` transport, the pure static-path resolver and the documented error-mapping table), `api/services.py` (the framework-agnostic service layer with per-plan single-flight) and `api/serialization.py` (the JSON contracts) plus `api/map_configuration.py` and `api/serve.py` (`python -m api.serve`, loopback by default, gitignored `var/routepilot.db`); the recommendation / selection / route / optimize / run-history endpoints; and the static workspace `web/index.html`, `web/styles.css`, `web/app.js`, `web/map.js` (override controls, read-only run history, honest map degradation). `core/` was not touched by Stage 4 |
| 5 | reoptimization after each served stop, active-leg protection groundwork |

## 10. Explicit non-goals of the current stages

Stages 0–2 contained no demo UI, no API, no geocoding, no routing provider, no traffic, no
side-of-road logic, no active-leg handling, no LLM integration, no automatic commitment of a
recommendation, and no candidate prefilter or approximation. **Stages 0–2 also contained no SQLite
code**; storage now exists under `storage/` (Stage 3, U9–U12, D38), and the **API and the web
workspace now exist** under `api/` and `web/` (Stage 4, U13–U16, D39) - both strictly outside
`core/`. The still-forbidden list stands unchanged:

- **no FastAPI and no other Python web framework**, and **no new dependency** of any kind: the
  transport is the stdlib `http.server` and FastAPI remains a future replacement only (D1/D39(a));
- **no npm, no bundler, no frontend framework and no build step**: `web/` is static HTML/CSS/JS
  served by the same local server (D39(b));
- **no real routing, geocoding or traffic provider** - every travel time and distance stays
  DEMO/SYNTHETIC;
- **no map vendor inside `core/`**: Leaflet and the OSM-compatible tiles live only in the browser and
  are configured through `app_settings`, and no Leaflet asset is vendored (D15/D39(c));
- **no drag/reorder** (D21), **no active-leg behaviour** (D24, Stage 5), and no reoptimization after a
  served stop;
- **no route mode other than `SMART_ROUTE`** (D19);
- **no automatic application of a recommendation**: the engine recommends, the driver decides
  (D4/D32), and a recommendation is never plan state.

`core/` itself still contains **no** API, UI or SQLite code and never imports them. The exact
incremental / delta evaluator is implemented (§9, U7) and changes only *how* a route is priced: it
introduces no prefilter, no shortlist and no approximation, and the reference full pass remains the
comparison baseline.
The D36 scale decision adds **no hard stop-count maximum** and no new validation limit: it changes a
performance target, not the domain.
