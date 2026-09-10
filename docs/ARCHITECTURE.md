# RoutePilot — Architecture

This document explains **how** the product implements the decisions in
[`DECISIONS.md`](DECISIONS.md) under the requirements of [`PRODUCT_SPEC.md`](PRODUCT_SPEC.md).
Where this document and the registry disagree, the registry wins.

Stage 0 implements the foundation only: domain skeleton, time layer, timeline arithmetic, error
taxonomy, `doctor`, tests, and a storage schema **proposal**. No optimizer, UI, storage code or
demo dataset exists yet.

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
    service_window.py  WindowKind, ServiceWindow (value object, D28)
    route_stop.py      GeocodeStatus, ServiceStatus, RouteStop
    route_plan.py      RoutePlan + invariant validation + order validation
    order_override.py  OrderConstraint, OrderOverrides (generic, D21)
    first_stop.py      FirstStopMode, SelectionSource, PinnedVia, FirstStopStatus,
                       FirstStopIntent, FirstStopResolution, CandidateDiagnostic, FirstStopCandidate
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
                       + capability records (D15/D16)  -- interfaces only in Stage 0
  validation/
    errors.py          error taxonomy (D26)
```

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
- any other combination raises `InvalidServiceWindowError`.

`unknown` and `unrestricted` both mean "no waiting, no lateness, no invented hours", but they are
**not** the same state: `unknown` marks missing data and is surfaced to the driver, `unrestricted`
states a known 24/7 access. Neither is ever turned into guessed business hours.

### 3.2 `RouteStop`

Spec §17 fields: `id`, `raw_address`, `normalized_address`, `latitude`, `longitude`,
`service_duration`, service window, `priority`, status, `enabled`, `notes`.

The three state fields are independent (D20):

- `geocode_status` — `pending | resolved | ambiguous | failed` (address → coordinates);
- `service_status` — `pending | in_progress | served | failed | skipped` (route execution);
- `enabled` — a disabled stop is excluded from optimization regardless of `service_status`.

Validated invariants: `geocode_status == resolved` requires coordinates; a `fixed` window requires
a resolved customer (a stop with an unknown address and a fixed window is rejected rather than
half-modelled).

### 3.3 `RoutePlan`

```
id, timezone (IANA), departure (PlaceRef START), departure_time (Instant),
finish (PlaceRef FINISH), route_mode, first_service_stop (FirstStopIntent),
order_overrides (OrderOverrides), cost_policy, stops, default_service_duration
```

Structural invariants (D10):

- **I1** START is a `PlaceRef`, never a `RouteStop`; `validate_order()` rejects any order containing
  the departure location, and START never appears in timelines.
- **I2** FINISH is a `PlaceRef` and is never part of the optimized order; it may not appear in it.
- **I3** a pinned first stop stays first until explicitly unpinned (enforced by the optimizer in
  Stage 2; the model records the intent now).
- **I4** AUTO never yields a "waiting for driver choice" state when a feasible candidate exists;
  MANUAL without a chosen first stop is a legal unresolved state, not an error.

`validate_order(order)` is pure domain validation (not a solver) and guarantees spec §27.3/§27.4/
§27.17: exactly the enabled stops, each exactly once, no duplicates, no unknowns, no disabled
stops, no START, no FINISH.

### 3.4 First stop: intent vs resolution (D11)

```
FirstStopIntent      mode, pinned, pinned_stop_id              # persisted user intent
FirstStopResolution  selected_stop_id, selection_source,       # derived, cached, never intent
                     pinned_via, status, resolved_at,
                     inputs_fingerprint, diagnostics
```

Validated combinations:

| Case | source | pinned | pinned_via |
|---|---|---|---|
| AUTO, unpinned | `auto_recommendation` | `false` | `None` |
| AUTO, driver pressed Lock | `auto_recommendation` | `true` | `lock` |
| driver overrode AUTO | `driver` | `true` | `override` |
| MANUAL choice | `driver` | `true` | `manual_mode` |

Model rules: `pinned_via` is `None` exactly when `pinned` is `false`; `lock` implies
`auto_recommendation`; `override`/`manual_mode` imply `driver`; `status == resolved` implies
`selected_stop_id is not None`; an unresolved status implies `selected_stop_id is None`. There is
deliberately **no** rule tying `selection_source` to `pinned` — that invariant was explicitly
rejected in D6.

First-stop statuses (D9): `resolved`, `unresolved_empty_plan`, `unresolved_manual_awaiting_choice`,
`no_active_stops`, `no_feasible_first_stop` (+ diagnostics).

### 3.5 Order overrides (D21)

`OrderOverrides` holds a tuple of `OrderConstraint(kind, stop_id, position)`:

- `kind='first_stop'` — implemented; at most one instance; when the intent is pinned it must agree
  with `first_stop_intent.pinned_stop_id` (consistency is validated, so two sources of truth cannot
  silently diverge);
- `kind='position'` — reserved for future drag/reorder. The domain can represent it, but Stage 0
  rejects it with `UnsupportedConstraintError` instead of pretending to honour it.

No constraint solver is built at Stage 0.

### 3.6 Cost policy (D13, amended)

`RouteCostPolicy` = name + `weights: Mapping[CostComponent, float]` + per-component
`ComponentStatus`. Stage 0 ships **no weights**: an empty policy is honest, whereas default numbers
would become product truth by accident.

Component statuses make capability honesty machine-checkable (D16):

- `implemented` — safe to use today;
- `planned` — declared in the spec, not implemented;
- `requires_provider` — cannot be computed without information the domain does not have.
  `wrong_side_penalty`, `u_turn_penalty`, `backtracking_penalty` are `requires_provider`
  (road geometry / direction), which is what prevents fake side-of-road logic from creeping into
  the objective (spec §11).

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

## 5. Timeline semantics (spec §7)

For each stop, in order, starting from `departure_time` at the departure location:

```
travel_time          = matrix(previous_location, stop_location)
estimated_arrival    = departure_from_previous + travel_time
fixed window:  waiting_time = max(0, window_start - estimated_arrival)
               service_start = max(estimated_arrival, window_start)   # never before opening
unrestricted/unknown: waiting_time = 0, service_start = estimated_arrival
service_duration     = stop.service_duration or plan.default_service_duration (else error)
estimated_departure  = service_start + service_duration
lateness             = max(0, service_start - window_end)     # > 0  <=> cannot begin inside window
overtime             = max(0, estimated_departure - window_end)  # informational only
feasibility          = infeasible iff lateness > 0, else feasible
```

Key semantics, decided in D13's amendment:

- a **hard** fixed window is met or violated — never "penalised a bit";
- `lateness > 0` produces an explicit `Violation(kind='time_window_infeasible')` attached to the
  stop **and** to the solution, whose status becomes `has_infeasible_windows`;
- service finishing after closing time records `overtime` as information; it is not an
  infeasibility and not a weight, because soft windows do not exist yet;
- `window_kind='unknown'` raises no violation — it raises a timeline flag (`window_unknown`) so the
  driver sees "hours unknown" instead of silently treated-as-open.

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
| 5, 6 | `tests/model/test_first_stop.py` (pinned first stop; Lock keeps provenance) |
| 7–10 | Stage 2 (selector) — intent/resolution semantics already covered by model tests |
| 11–14 | `tests/time/test_timeline.py` |
| 15, 16 | `tests/time/test_tz_strict_validation.py` |
| 18 | Stage 2 (local search monotonicity) |
| 19 | Stage 2/5 |
| 20 | Stage 1/2 (golden demo route) |
| 21 | Stage 3 (SQLite round-trip) |
| — | `tests/test_core_isolation.py` (D1/§22), `tests/tools/test_doctor.py` (D12) |

- The suite prints a warning and uses a detected system TZif tree when `tzdata` is unavailable, so
  the DST tests are meaningful on an offline machine; the real fix remains `pip install tzdata`.

## 9. Extension points

| Stage | Extension |
|---|---|
| 1 | cost weights, demo dataset (~30 stops), synthetic matrix, 04:00/08:00 scenario |
| 2 | first-stop selector (candidates → scoring → selection), optimizer (seed + local search), top-K explanation |
| 3 | SQLite repositories behind the schema proposal |
| 4 | API transport + web UI (map, timeline panel, summary, override controls) |
| 5 | reoptimization after each served stop, active-leg protection groundwork |

## 10. Explicit Stage 0 non-goals

No optimizer, no scoring, no cost weights, no first-stop selection, no demo dataset, no SQLite
code, no API, no UI, no geocoding, no routing, no traffic, no side-of-road logic, no active-leg
handling, no LLM integration.
