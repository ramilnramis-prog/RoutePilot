# RoutePilot — Architecture

This document explains **how** the product implements the decisions in
[`DECISIONS.md`](DECISIONS.md) under the requirements of
[`PRODUCT_SPEC_v2.md`](PRODUCT_SPEC_v2.md) (the current Source of Truth;
[`PRODUCT_SPEC.md`](PRODUCT_SPEC.md) v1 is historical and unchanged).

Precedence when documents disagree (v2 section 37): the **current product specification** plus
**explicitly approved later decisions** in the registry control future implementation. Where this
document and the registry disagree, the registry wins.

Stages 0, 1 and 1.5 implement the foundation, the demo scenario and the semantic migration to
RECOMMEND / MANUAL. Stage 2 (complete-route optimizer and recommendation) is not started.

---

## 1. Layers and the dependency rule

```
web/      HTML/CSS/JS + Leaflet/OSM tiles        (later)  -- never imported by core
api/      transport: stdlib http.server -> FastAPI (later) -- depends on core
storage/  SQLite repositories                    (later)  -- depends on core, never the reverse
demo/     deterministic demo dataset + synthetic matrix (later) -- depends on core
core/     domain model, time, engine              <-- depends on nothing but the stdlib
tools/    doctor and developer utilities          -- may inspect core, never imported by it
```

The rule is one-directional: **`core/` imports only the Python standard library.** It contains no
HTTP, no UI, no storage, no network, no vendor SDK, no Leaflet, no OSM. This is enforced by an
automated import check in the test suite (`tests/test_core_isolation.py`), not by convention.

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
    route_plan.py      RoutePlan + invariant validation + order validation
    order_override.py  OrderConstraint, OrderOverrides (generic, D21)
    first_stop.py      FirstStopMode, SelectionSource, FirstStopState, RecommendationStatus,
                       FirstStopIntent, FirstStopRecommendation, CandidateDiagnostic,
                       FirstStopCandidate
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
    cost.py            weighted scoring over implemented components (D13, D31)
    first_stop/
      evaluation.py    first-stop candidate timing, cost breakdown, deterministic ranking
  validation/
    errors.py          error taxonomy (D26)

demo/
  dataset.py           deterministic ~30-stop demo plan (DEMO/SYNTHETIC, spec section 24)
  synthetic_matrix.py  deterministic synthetic travel matrix (DEMO/SYNTHETIC)
  report.py            numeric demo report: `python -m demo.report`
```

Stage status: `core/model`, `core/time`, `core/validation` and `core/engine/providers.py` are
Stage 0; `core/engine/cost.py`, `core/engine/first_stop/evaluation.py` and `demo/` are Stage 1. The
first-stop semantics were then migrated off the revoked AUTO model (D4/D32): the engine recommends,
the driver decides. There is still no optimizer, no storage code, no API and no UI.

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
  recomputation or a changed recommendation (enforced by the optimizer in Stage 2; the model records
  the decision now).
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
`None`): `recommended`, `no_fully_feasible_route` (v2 §14), `no_active_stops`, `empty_plan`
(+ diagnostics and, from Stage 2, the rejected candidates with their violating stops and reasons).

### 3.5 Order overrides (D21)

`OrderOverrides` holds a tuple of `OrderConstraint(kind, stop_id, position)`:

- `kind='first_stop'` — implemented; at most one instance, and when present it must mirror the
  driver's `selected_stop_id` (validated, so the two representations cannot silently diverge). An
  absent override is not a conflict: the selection is authoritative;
- `kind='position'` — reserved for future drag/reorder. The domain can represent it, but this stage
  rejects it with `UnsupportedConstraintError` instead of pretending to honour it.

No constraint solver is built yet.

### 3.6 Cost policy (D13, amended)

`RouteCostPolicy` = name + `weights: Mapping[CostComponent, float]` + per-component
`ComponentStatus` + a `provisional` marker. The default policy ships **no weights**: an empty
policy is honest, whereas invented numbers would become product truth by accident.

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
`UnsupportedFeatureError`. The only weighted policy is `demo_provisional_v1`
(`travel_time = 1`, `waiting_time = 2`, marked `provisional`), whose numbers are explicitly not
product truth (D31). Scoring itself is one function, `core.engine.cost.score_breakdown`, so the
objective stays visible and testable.

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

### 5.1 First-stop recommendation (Stage 1, D32)

`core.engine.first_stop.evaluate_first_stop_candidates` times and prices **every** possible first
stop with the same per-leg arithmetic as a real route leg, and returns a
`FirstStopEvaluationReport`:

- `ranked` — feasible candidates ordered by `(score, travel_time, stop_id)`. The explicit tie-break
  keeps results reproducible even when a weight set cannot separate candidates;
- `infeasible` — candidates whose hard window cannot be met, each with its explicit `Violation`.
  They are **never** ranked and never recommended, even when their score is the lowest of all
  candidates;
- `disabled_stop_ids` — excluded stops, reported rather than silently dropped.

What this deliberately is **not**: it never selects, pins or applies anything. `report.recommended()`
is advisory input for the driver (D4), and the driver's decision lives in `FirstStopIntent`, not in
the engine.

Spec §8 and D32 require ranking **complete route outcomes**
(`START → candidate → optimized remaining stops → FINISH`). The optimizer that produces them is
Stage 2, so `remaining_route_estimate` is `None` rather than approximated, and `feasible` still
describes the first leg only. The reason is measurable: for every candidate arriving before opening,
`travel + waiting` is fixed by the opening time, so at a 1:1 weight ratio they tie exactly. The demo
report shows that degeneracy explicitly, which is why the demo weights are marked provisional (D31).
Once complete-route outcomes exist, candidate feasibility must cover the whole route (D32).

### 5.2 Demo data and provenance (Stage 1)

`demo/dataset.py` builds a deterministic, ~30-stop demo plan (departure 04:00, many customers
opening 08:00, mixed window kinds, priorities, one disabled stop, one stop without a service
duration). `demo/synthetic_matrix.py` provides a deterministic synthetic matrix (1 coordinate
degree = 1 hour) carrying `DataProvenance.DEMO_SYNTHETIC` and **empty** provider capabilities.

`demo/report.py` (`python -m demo.report`) prints the whole numeric story: input order, notable
first-leg timelines, all candidate costs, the top 5, why the winner is neither nearest nor
farthest, the departure-time sweep, waiting-weight sensitivity, the window-end-policy comparison
and the unknown-hours caveat. Synthetic data is labelled everywhere and the report never presents
it as road routing.

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
| 7–10 | `tests/engine/test_first_stop_evaluation.py` (A/B/C criteria of the *recommendation*), `tests/demo/test_report.py` (departure sweep) |
| 11–14 | `tests/time/test_timeline.py` |
| 15, 16 | `tests/time/test_tz_strict_validation.py` |
| 18 | Stage 2 (local search monotonicity) |
| 19 | Stage 2/5 |
| 20 | `tests/demo/test_report.py` (report determinism, sweep determinism) |
| 21 | Stage 3 (SQLite round-trip) |
| — | `tests/test_core_isolation.py` (D1/§22), `tests/tools/test_doctor.py` (D12), `tests/engine/test_cost.py` (D13/D31), `tests/demo/*` (spec §24) |

- The suite prints a warning and uses a detected system TZif tree when `tzdata` is unavailable, so
  the DST tests are meaningful on an offline machine; the real fix remains `pip install tzdata`.

## 9. Extension points

| Stage | Extension |
|---|---|
| 0 ✅ | foundation: docs, domain skeleton, time layer, strict DST, error taxonomy, doctor, tests, storage schema proposal |
| 1 ✅ | cost scoring over implemented components, deterministic demo dataset (~30 stops), synthetic matrix, 04:00 / 08:00 scenario, candidate evaluation, numeric demo report |
| 1.5 ✅ | semantics migration off the revoked AUTO model: RECOMMEND/MANUAL, recommendation vs driver decision, `awaiting_first_stop_choice` (D4–D11, D32) |
| 2 | recommendation engine over **complete route outcomes** + optimizer + remaining-route term + top-K + driver-choice actions + caching + performance measurement |
| 3 | SQLite repositories behind the approved schema |
| 4 | API transport + web UI (map, timeline panel, summary, override controls) |
| 5 | reoptimization after each served stop, active-leg protection groundwork |

## 10. Explicit non-goals of the current stages

Stage 0 and Stage 1 deliberately contain no optimizer, no route selection, no pinning, no demo UI,
no SQLite code, no API, no geocoding, no routing provider, no traffic, no side-of-road logic, no
active-leg handling and no LLM integration.
