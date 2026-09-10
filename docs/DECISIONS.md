# RoutePilot — Decision Registry

This file is the **only** place where a decision counts as settled. Chat/session history is
not a decision log. The product specification ([`PRODUCT_SPEC.md`](PRODUCT_SPEC.md)) is the
Source of Truth for *what* the product must do; this registry records *how* we decided to do it.

- Registry revision: **D1–D31**, approved 2026-09-11 (Stage 0, extended during Stage 1).
- Status values: `approved` (settled), `amended` (settled with a recorded change), `deferred` (recorded, not implemented).

---

## D1 — Stack and dependency direction

- Backend, domain model, optimization and calculations: **Python**, stdlib-first.
- FastAPI may replace the transport layer later **without rewriting** `core/`.
- Frontend: HTML / CSS / JavaScript.
- `core/` must not import HTTP/UI, storage or network modules. Enforced by an automated
  import check in the test suite, not by convention.
- Status: `approved`. Spec: §22, §26, §14.

## D2 — Time model

- All absolute timestamps are stored in **UTC**; every `RoutePlan` carries an explicit
  **IANA** time zone (e.g. `Europe/Moscow`).
- Local input/output is resolved through that time zone.
- `datetime` + `zoneinfo` + `tzdata`. No custom timezone or DST tables.
- Fixed UTC offsets are not an acceptable long-term model.
- tzdata is the only runtime dependency (pure data package) and its version participates in
  `inputs_fingerprint`.
- Status: `approved`. Spec: §20.

## D3 — DST policy: strict validation

- A local service-window time that is either **nonexistent** (DST gap) or **ambiguous**
  (occurs twice) produces an **explicit validation/disambiguation error**.
- The domain never silently shifts such a time forward and never silently chooses `fold=0`/`fold=1`.
- The domain must never silently change a customer's service window.
- Configurable resolution policies may be added later; **strict validation is the default**.
- Status: `approved`. Spec: §21.

## D4 — AUTO first-stop semantics

- AUTO evaluates candidate first stops, **automatically applies** the best one and builds the
  complete route **without requiring confirmation**.
- The applied automatic choice is **dynamic**: `selection_source = auto_recommendation`, `pinned = false`.
- When meaningful inputs change, AUTO must re-evaluate and may choose a different first stop.
  Triggers: `departure_time`, `departure_location`, active stop set, service windows,
  priorities, finish location, route/travel matrix, traffic (when it exists later).
- A deterministic `inputs_fingerprint` prevents a stale recommendation from being retained.
- Status: `approved`. Spec: §3, §5.

## D5 — Pinning only by explicit user action

- `pinned = true` arises **only** from an explicit user action: Lock, override in AUTO, or a
  MANUAL selection.
- An automatic selection never produces `pinned = true`.
- Status: `approved`. Spec: §3, §4, §18.

## D6 — Selection provenance and pinning are separate

- `selection_source` describes **how the stop was selected**; `pinned` describes **whether that
  selection is currently locked**; `pinned_via` describes **what locked it**.
- Locking an automatically recommended stop **must not** rewrite its provenance.
- Canonical states:

| State | selection_source | pinned | pinned_via |
|---|---|---|---|
| AUTO recommendation | `auto_recommendation` | `false` | `None` |
| AUTO recommendation locked by driver | `auto_recommendation` | `true` | `lock` |
| Driver overrides AUTO | `driver` | `true` | `override` |
| MANUAL selection | `driver` | `true` | `manual_mode` |

- The invariant `selection_source == 'auto_recommendation' iff pinned == false` is **invalid**.
- Status: `approved`. Spec: §4, §27.6.

## D7 — MANUAL first stop

- The driver explicitly chooses the first service stop: `selection_source = driver`,
  `pinned = true`, `pinned_via = manual_mode`.
- The optimizer preserves that first stop and optimizes all remaining stops around it.
- The system never silently replaces a manually pinned first stop.
- On unpin: the first stop becomes **unresolved**. The domain does **not** choose a stop and does
  **not** switch modes; the user must choose another first stop or explicitly switch to AUTO.
- Status: `approved`. Spec: §3, §6, §18.

## D8 — Unpin in AUTO

- `unpin` → recompute the recommendation → `selection_source = auto_recommendation`,
  `pinned = false`, `pinned_via = None` → rebuild the route.
- Status: `approved`. Spec: §6.

## D9 — `selected_stop_id` may legitimately be `None`

- `selected_stop_id: StopId | None` is a legal domain state; a fake `StopId` is never substituted.
- Distinct reasons are distinguished, not collapsed into one `None`:
  `resolved`, `unresolved_empty_plan`, `unresolved_manual_awaiting_choice`, `no_active_stops`,
  `no_feasible_first_stop`.
- `no_feasible_first_stop` must carry diagnostics (which stop, which constraint, why rejected).
- Status: `approved`. Spec: §6, §7, §10.

## D10 — Model invariants

- **I1** — the departure location (START) is never treated as a service stop and never appears in
  the order or in timelines.
- **I2** — FINISH is fixed and is never reordered as a normal stop.
- **I3** — a pinned first stop stays first through any optimization until explicitly unpinned.
- **I4** — in AUTO, when at least one feasible candidate exists, optimization always returns a
  complete route and never returns a "waiting for driver choice" state. In MANUAL without a
  chosen first stop, no complete route is built, and that is a valid domain state, not an error.
- Status: `approved`. Spec: §1, §2, §3, §18, §27.

## D11 — Intent vs resolution

- `first_service_stop` is split internally:
  - **intent** (persisted user intent): `mode`, `pinned`, `pinned_stop_id`;
  - **resolution** (derived, cached, never intent): `selected_stop_id`, `selection_source`,
    `pinned_via`, `status`, `resolved_at`, `inputs_fingerprint`, `diagnostics`.
- Status: `approved`. Spec: §4, §5.

## D12 — `doctor` environment check

- `tools/doctor.py` reports environment health, including tzdata availability, with an exact
  install command.
- Missing tzdata is a **WARN** in interactive dev mode and a **FAIL** in strict/CI mode, and is
  always a FAIL when a plan actually declares an IANA zone.
- Status: `approved`. Spec: §20, §21 (implicit).

## D13 — Route cost policy *(amended)*

- `RouteCostPolicy` exists as an abstraction with configurable weights; arbitrary weights are
  **not** hardcoded as product truth at early stages.
- Supported components: `travel_time`, `distance`, `waiting_time`, `early_arrival_penalty`,
  `late_arrival_penalty`, `time_window_violation_penalty`, `u_turn_penalty`, `wrong_side_penalty`,
  `backtracking_penalty`, `priority_penalty`, `finish_direction_penalty`,
  `first_stop_remaining_route_weight`.
- Early arrival is allowed and normally creates `waiting_time`. A stop can never begin service
  before `service_window_start`.
- **Amendment (2026-09-11, resolving conflict C1):** a **fixed customer service window is HARD**.
  If service cannot begin within the permitted window, the stop and the solution carry an
  **explicit infeasibility violation**. Hard infeasibility is **never** represented by
  `violation_dominance_factor` or by any large numeric penalty.
  - hard window violation → explicit `Violation` / infeasible state;
  - ordinary cost weights → only choose among **feasible** alternatives;
  - `violation_dominance_factor` is retained **only** for ordering *soft* components relative to
    each other, and it does not represent impossibility;
  - soft/preferred lateness may be introduced later **only** as an explicitly separate concept of
    a soft/preferred service window.
- Status: `amended`. Spec: §10.

## D14 — Storage

- SQLite for the initial version.
- Entities to plan for: route plans, route stops, route optimization runs, application settings.
- The exact schema is **proposed before implementation** and implemented only after approval.
- Storage code never leaks into optimization logic; `core/` never imports storage.
- Storage implementation is **deferred**: the schema refinements of D29 (window end policy on the
  plan and per stop) and D30 (versioned order-override JSON) are recorded in the proposal, but no
  SQLite code exists yet.
- Status: `approved` (proposal reviewed and refined; implementation still deferred). Spec: §26.

## D15 — Providers and map configuration

- Vendor access is isolated behind interfaces: `GeocodingProvider`, `RoutingProvider`,
  `TravelMatrixProvider`, plus map/tile configuration.
- Business/domain logic never depends on Google Maps, Yandex, OSM or any specific vendor.
- Demo map: **Leaflet + OpenStreetMap-compatible tiles**, with visible attribution, tile provider
  isolated in configuration, no core dependency on Leaflet/OSM, and fully offline tests.
  A synthetic polyline is never presented as real road routing.
- Status: `approved`. Spec: §15, §25.

## D16 — Capability honesty

- Every cost component, route mode and provider feature declares its implementation status.
- Unimplemented functionality is never presented as working.
- Side-of-road logic is never inferred from latitude/longitude; real side-of-road logic requires
  road geometry, direction and routing-provider data.
- Status: `approved`. Spec: §11, §19, §29.

## D17 — Optimizer abstraction

- Optimization is isolated from UI, storage and routing providers behind a solver abstraction.
- MVP baseline: deterministic greedy/nearest-neighbor seed → local improvement (2-opt class).
  Nearest-neighbor is **never** the final product algorithm.
- Local improvement must never accept a move that worsens the accepted cost.
- Later replacement (richer local search, VRP, constraint solvers, OR-Tools, time windows,
  multi-vehicle) must not require rewriting the domain model.
- Status: `approved`. Spec: §14, §27.18.

## D18 — Scale and chunking

- Designed for dozens → ~100 → 100+ stops.
- Never assume a third-party routing API can optimize all stops in one request.
- Matrix and route geometry are requested in chunks.
- Flow: addresses → coordinates → matrix → RoutePilot optimizer → ordered stops → geometry →
  navigator/map.
- Status: `approved`. Spec: §13.

## D19 — Route modes

- `FASTEST`, `SHORTEST`, `MINIMUM_TURNS`, `ON_THE_WAY`, `START_TO_FINISH`, `SMART_ROUTE` are
  representable in the domain.
- MVP implements `SMART_ROUTE` (or a deterministic demo approximation) only; the rest are
  declared not implemented and are never presented as working.
- Note: first-stop mode (`auto` / `manual`, §3) and route mode (§19) are **different axes** and
  must not be conflated in naming.
- Status: `approved`. Spec: §19.

## D20 — Address intake and stop state

- Intake flow: parse candidate addresses → geocode → identify ambiguous/failed → require
  correction → only then optimize. An ambiguous address is never silently guessed.
- `geocode_status`: `pending` | `resolved` | `ambiguous` | `failed`.
- `service_status` (execution state, not geocoding): `pending` | `in_progress` | `served` |
  `failed` | `skipped`. Stage 0 defines it but implements no active-route behaviour.
- `enabled` is independent: a disabled stop is excluded from optimization regardless of
  `service_status`.
- Status: `approved`. Spec: §16, §17.

## D21 — Manual control and generic order overrides

- A **generic order-override concept** is part of the domain from the start; the architecture is
  not limited to first-stop pinning.
- The MVP implements only: first-stop selection and first-stop pinning. Future drag/reorder must
  be expressible through the same mechanism without redesigning `RoutePlan`.
- No full constraint solver is built at Stage 0.
- A manually pinned first stop survives route optimization.
- Status: `approved`. Spec: §18.

## D22 — Metrics and baselines

- User-facing **BEFORE** = the stop order exactly as supplied by the user; **AFTER** = the
  RoutePilot optimized order. This answers "how much did RoutePilot improve the route I would
  otherwise have driven?".
- Internally an `algorithm_baseline` (e.g. greedy/nearest-neighbor) may be recorded for
  benchmarking optimizer quality, and must never be labelled as the user's "before" route.
- Status: `approved`. Spec: §25.

## D23 — Demo dataset and provenance labelling

- Deterministic demo dataset of ~30 realistic service stops including departure location, finish
  location, departure time, several service windows, service durations, priorities and
  deterministic synthetic travel costs.
- The demo includes the scenario: departure around 04:00, several customers opening around 08:00,
  and shows that nearest is not always selected, farthest is not always selected, and that
  departure time can change the recommendation.
- All synthetic travel data is clearly labelled **DEMO/SYNTHETIC** and is never displayed as real
  road routing data. Demo results must be deterministic.
- Status: `approved`. Spec: §24.

## D24 — Active leg protection (recorded, not implemented)

- Dynamic reoptimization must not silently change the leg the driver is already executing.
- Future recomputation may change the route **after** the committed active leg.
- Recorded now; no MVP implementation.
- Status: `deferred`. Spec: §12.

## D25 — LLM / AI policy

- Core route decisions are deterministic and algorithmic; no LLM is required to calculate a route.
- The optimizer and its tests must work without an LLM.
- Explanations come from deterministic cost breakdowns, not generated prose.
- Future LLM use is limited to optional features (unstructured address extraction, OCR cleanup,
  natural-language import, explanation assistance).
- Status: `approved`. Spec: §23, §9.

## D26 — Error taxonomy

- One shared error model. **Errors** describe invalid input/configuration; **violations** describe
  valid input with an infeasible outcome. These are never conflated.
- Explicit errors are required for at least: DST gap, DST ambiguity, unknown time zone, missing
  tzdata, invalid service window, unsupported (declared but unimplemented) constraint.
- An ambiguous DST time error carries both candidate absolute instants so a future disambiguation
  API can be built on it.
- Status: `approved`. Spec: §16, §21, §10.

## D27 — Repository safety and documentation

- Git is used from the beginning. Never committed: secrets, API keys, local databases, logs,
  caches, temporary artifacts.
- Required files: `.env.example`, `.gitignore`, `README.md`, `docs/ARCHITECTURE.md`,
  `docs/DECISIONS.md`.
- Documentation set:
  - `docs/PRODUCT_SPEC.md` = what the product must do (verbatim Source of Truth, never rewritten
    as a summary). **Not** named `TZ.md`, because `TZ` is confused with timezone terminology.
  - `docs/DECISIONS.md` = approved decisions.
  - `docs/ARCHITECTURE.md` = how the product implements them.
  - `docs/STORAGE_SCHEMA.md` = proposed persistence model.
- Status: `approved`. Spec: §28.

## D28 — Service window representation

- Domain uses a `ServiceWindow` value object with an explicit discriminator
  `window_kind = fixed | unrestricted | unknown`.
- `fixed`: `start_local` and `end_local` are both required.
- `unrestricted` / `unknown`: both values are `None`.
- Missing opening hours are never invented.
- Persistence may later flatten this into `window_kind` + `service_window_start` +
  `service_window_end` (see `docs/STORAGE_SCHEMA.md`).
- Status: `approved`. Spec: §7, §17.

---

## D29 — Window end semantics

- What the **end** of a service window means is an explicit concept, never a hidden global
  assumption:

  | `window_end_policy` | Meaning |
  |---|---|
  | `service_finish_before_end` | service must **finish** before closing (conservative; MVP/demo default) |
  | `service_start_before_end` | it is enough that service **begins** before closing |

- Resolution order: a stop-level override on `ServiceWindow`, otherwise the plan-level default.
  This is what lets a specific customer or provider opt into the looser interpretation later
  without redesigning anything.
- The default and the demo behaviour is `service_finish_before_end`. Example: window 08:00-18:00,
  `service_duration` 30m, service start 17:50 -> **infeasible**, because service would finish at
  18:20.
- Timeline terminology (updated with this decision):
  - `lateness` = the miss measured under the **applied** policy; `lateness > 0` means the stop
    cannot be served within its permitted window, and that is exactly when it is infeasible;
  - `finish_overtime` = how long service runs past the closing instant (always recorded);
  - `start_lateness` = the start miss, available as a diagnostic.
- Status: `approved`. Spec: §7, §10.

## D30 — Order overrides are persisted as versioned JSON

- For the MVP, a plan's general order overrides are persisted as a **structured, versioned JSON
  envelope** (`{"version": 1, "constraints": [...]}`), not as a normalized order-constraints table.
- Reason: the MVP implements only first-stop pinning and the final constraint vocabulary for
  arbitrary drag/reorder is not known yet.
- The version field exists so the format can migrate to a normalized table later **without
  changing the core domain model**.
- Status: `approved`. Storage implementation remains deferred (see D14). Spec: §18.

## D31 — Provisional demo weights

- The only weighted policy in the project is `demo_provisional_v1`
  (`travel_time = 1`, `waiting_time = 2`), marked `provisional` in code and in every report.
- These numbers are **not product truth**: they exist to demonstrate the architecture. The demo
  report shows their sensitivity, including the degenerate 1:1 case where every candidate that
  arrives before opening ties exactly and the tie-break hands the choice to the nearest stop.
- The capability gate still applies: a weight may only be set for a component whose status is
  `implemented`, so nothing unimplemented can be scored silently.
- Status: `approved`. Spec: §10, §24.

---

## Stage gates

| Gate | Requirement |
|---|---|
| Stage 0 completion | all tests pass · `doctor` runs · domain model reviewed · `STORAGE_SCHEMA.md` reviewed · `git status` clean after one commit |
| Stage 0 → Stage 1 | explicit human approval of Stage 0 **and** of the storage schema |
| Storage implementation | only after `docs/STORAGE_SCHEMA.md` is explicitly approved |

---

## Open items (recorded, not decided)

1. **Overnight service windows** (`end_local <= start_local`, e.g. 22:00–02:00) are not supported in
   Stage 0: such a window is rejected as `InvalidServiceWindowError` rather than silently interpreted
   as a next-day close. Needs a decision before real customers with night hours are imported.
2. **Soft / preferred windows** — lateness penalties may only be introduced together with an explicit
   soft-window concept (see D13 amendment).
3. **Storage of order overrides** in SQLite (normalized table vs JSON column) — to be settled with the
   storage schema review.
4. **`algorithm_baseline` definition** (which heuristic, which tie-breaking) — Stage 2.
5. **Active-leg protection** implementation — Stage 5.

## Environment notes (machine-specific, not product decisions)

- This development machine has **no working network** (the configured SOCKS proxy
  `127.0.0.1:10801` refuses connections and direct HTTPS fails), so `python -m pip install tzdata`
  cannot currently succeed.
- `zoneinfo` therefore has no IANA database by default, but a valid **TZif** tree exists at
  `C:\Program Files\Git\mingw64\share\zoneinfo` (IANA version **2026a**, from Git for Windows).
- Mechanism used: the standard `PYTHONTZPATH` search path (or `zoneinfo.reset_tzpath()`).
  Production code never activates it silently; the test bootstrap does, and prints a warning.
  `doctor` detects and reports it, and always prints the real fix: `python -m pip install tzdata`.
