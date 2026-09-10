# RoutePilot — Decision Registry

This file is the **only** place where a decision counts as settled. Chat/session history is
not a decision log. The current Source of Truth for *what* the product must do is
[`PRODUCT_SPEC_v2.md`](PRODUCT_SPEC_v2.md); [`PRODUCT_SPEC.md`](PRODUCT_SPEC.md) v1 is historical
and unchanged. Precedence (v2 section 37): the current specification plus explicitly approved later
decisions in this registry control future implementation; this registry records *how* we decided to
do it.

- Registry revision: **D1–D33**, approved 2026-09-11 (Stage 0, extended during Stage 1; D4–D11
  amended when the AUTO semantics were revoked; D5/D9/D16 aligned with v2 sections 5, 14 and 23;
  D33 added for `input_position`).
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

## D4 — First-stop modes: recommendation, not selection *(amended)*

> **Amended 2026-09-11: the previously approved AUTO semantics are revoked.** The product has not
> been released, so no obsolete AUTO behaviour is retained for compatibility.

- The first-stop modes are **`recommend`** and **`manual`** (the old `auto` mode is gone).
- **RECOMMEND**: RoutePilot 1) evaluates possible first service stops, 2) calculates complete-route
  outcomes for the candidates, 3) ranks them, 4) shows the recommended candidate and top-K
  alternatives, 5) **does not automatically apply any candidate**, 6) waits for the driver to choose
  explicitly, and 7) only then finalizes the full working route.
- **MANUAL**: the driver selects the first service stop directly, without needing a ranked
  recommendation.
- In **both** modes the final first service stop is a **driver decision**.
- Input changes that alter the ranking (`departure_time`, `departure_location`, active stop set,
  service windows, priorities, finish location, travel matrix, traffic later) are reported as
  **"recommendation has changed"** and must **never** replace an already driver-selected first stop.
  The driver may keep the current first stop, accept the new recommendation, or choose another stop.
- `inputs_fingerprint` is retained, but its meaning changes: it detects that the **recommendation**
  is stale, not that an applied choice should be invalidated.
- **Revoked invariant:** "AUTO always returns a committed full route without driver confirmation."
  Replaced by the invariant in D10/I4.
- Status: `amended` (supersedes the earlier AUTO semantics, and the AUTO part of spec §3).
  Spec: §3, §5.

## D5 — Pinning follows an explicit driver choice *(amended)*

- `pinned = true` arises **only** from an explicit driver selection of the first service stop.
- Selecting the first service stop **always pins it**: `selected_stop_id != None` with
  `pinned = false` is invalid (v2 §5). There is no "chosen but unlocked" state; the driver cancels
  the selection instead. The optimizer must never silently change a selected first stop.
- Nothing automatic produces a selection at all, so nothing automatic can produce `pinned = true`.
- Status: `amended`. Spec: §3, §4, §18; v2 §4, §5.

## D6 — Choice provenance *(amended)*

- `selection_source` records **how the driver arrived at the choice**; `pinned` records that the
  choice is locked. They remain separate concepts.
- Values:

| Driver action | selection_source | pinned |
|---|---|---|
| Pressed "start from this stop" on the recommended candidate | `accepted_recommendation` | `true` |
| Chose a different stop from the alternatives | `manual_choice` | `true` |
| Chose directly in MANUAL mode | `manual_choice` | `true` |
| No choice yet | — (`None`) | `false` |

- A recommendation carries **no** provenance: it is not a choice (see D32).
- `pinned_via` is **removed** (proposal, awaiting confirmation): with no automatically applied
  selection there is no "Lock" action left, so `pinned_via` would only duplicate `selection_source`.
  The old values `lock` / `override` / `manual_mode` no longer exist.
- Status: `amended`. Spec: §4, §27.6.

## D7 — MANUAL mode *(amended)*

- The driver selects the first service stop directly; no ranked recommendation is required.
  The choice is `selection_source = manual_choice`, `pinned = true`.
- The optimizer preserves that first stop and optimizes all remaining stops around it; the system
  never silently replaces it.
- On clearing the choice: the plan returns to `awaiting_first_stop_choice`. The domain never chooses
  a stop on the driver's behalf — in either mode.
- Status: `amended`. Spec: §3, §6, §18.

## D8 — Clearing the first stop *(amended)*

- Clearing the first stop (any mode) → `status = awaiting_first_stop_choice`,
  `selected_stop_id = None`.
- A recommendation may still be computed and displayed; nothing is applied and no working route is
  committed.
- Status: `amended`. Spec: §6.

## D9 — `selected_stop_id` may legitimately be `None` *(amended)*

- `selected_stop_id: StopId | None` is a legal and *normal* state: it is the state before the driver
  chooses. A fake `StopId` is never substituted.
- Distinct states are distinguished, not collapsed into one `None`:
  `awaiting_first_stop_choice`, `unresolved_empty_plan`, `no_active_stops`,
  `no_fully_feasible_route`; after a choice the state is `first_stop_selected`.
- `recommended_stop_id` is a **separate** field with a separate lifecycle. This is valid:

  ```
  recommended_stop_id = S73
  selected_stop_id    = None
  status              = awaiting_first_stop_choice
  ```

- `no_fully_feasible_route` (v2 §14) must carry diagnostics: which stops violate which hard window,
  and why. An infeasible candidate is never labelled a valid recommended route.
- Status: `amended`. Spec: §6, §7, §10.

## D10 — Model invariants *(amended)*

- **I1** — the departure location (START) is never treated as a service stop and never appears in the
  order or in timelines. *(unchanged)*
- **I2** — FINISH is fixed and is never reordered as a normal stop. *(unchanged)*
- **I3** — a driver-selected first stop stays first through any optimization and is never replaced by
  a recomputation, a changed input or a changed recommendation. Only an explicit driver action
  changes it. *(strengthened)*
- **I4** *(replaced)* — in RECOMMEND mode, when at least one feasible candidate exists, the system
  always returns ranked recommendations, but a **committed first service stop** — and therefore a
  committed working route — requires an **explicit driver choice**. Before that choice the plan is in
  `awaiting_first_stop_choice`, which is a valid state and not an error.
- **I5** *(new)* — a recommendation never implies selection: `recommended_stop_id` and
  `selected_stop_id` are separate fields with separate lifecycles (D32).
- **I6** *(new, proposed)* — a candidate's previewed "complete route" figure must be produced by the
  same objective and the same optimizer that builds the final route; preview and commit never
  diverge.
- Status: `amended`. Spec: §1, §2, §3, §18, §27.

## D11 — Intent vs recommendation *(amended, restructured)*

- **intent** (persisted — the driver's decision): `mode`, `selected_stop_id` (or `None`), `pinned`.
- **recommendation** (derived, cached, recomputable — never a decision): `recommended_stop_id`,
  the ranked top-K complete-route candidates, `inputs_fingerprint`, `resolved_at`, `status`,
  diagnostics.
- The driver's choice is **intent**, not a derived value: recomputation may change the
  recommendation and can never change the selection.
- This supersedes the previous split, where the applied selection was treated as derived.
- Status: `amended`. Spec: §4, §5.

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

- Every cost component, route mode and provider feature declares its implementation status, with
  exactly four values (v2 §23): `implemented`, `planned`, `requires_provider`, `unsupported`.
- Unimplemented functionality is never presented as working, and an `unsupported` capability is
  never weightable.
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
- Note: first-stop mode (`recommend` / `manual`, §3) and route mode (§19) are **different axes** and
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

## D32 — Recommendation is not selection

- In RECOMMEND mode, before the driver chooses, the plan's first-stop state is
  **`awaiting_first_stop_choice`**.
- The application may calculate and display: the recommended candidate, top-K alternatives,
  complete-route previews, ETA, waiting, expected finish, total travel, route cost and violations —
  but there is **no committed working route** yet.
- `recommended_stop_id` and `selected_stop_id` are separate fields; a recommendation does not imply
  selection (I5).
- For every candidate the engine computes the **complete route outcome**:
  `START → candidate → optimized remaining stops → FINISH`. The driver chooses between complete
  outcomes, not between first-leg distances.
- The top 3–5 candidates are shown and **alternatives are never hidden**; the driver must make the
  selection.
- A candidate whose *complete* route contains an infeasible hard window is excluded from the ranking
  and reported explicitly with its violations, exactly like a first-leg infeasibility.
- Status: `approved`. Spec: §3, §5, §6, §9, §25.

## D33 — `input_position` is immutable input-order provenance

- `RouteStop.input_position: int` records the position the stop had in the user-supplied or imported
  list (v2 §25, §30). It is **not** route order.
- `input_position >= 0`, and it must be **unique within a plan**. Gaps are allowed and must not be
  renumbered (`0, 1, 4, 7` is valid).
- RouteStop is frozen and nothing in the domain rewrites the field: optimization, route reordering,
  order overrides and drag/reorder never change it. Future manual reordering uses the order-constraint
  / order-override model (D21), never `input_position`.
- Appending a later stop takes `RoutePlan.next_input_position()` = `max(input_position) + 1`, so
  historical positions stay stable instead of being rewritten.
- The **USER baseline** (v2 §30) is `START -> enabled stops sorted by input_position -> FINISH`.
  Disabled stops are omitted, but their existence never renumbers the remaining positions.
- A plan holds its stops in input order regardless of the order the caller passed them, and the
  **recommendation fingerprint is independent of input order**: swapping positions does not report
  the recommendation as stale (the recommendation does not depend on arrival order — v2 §7).
- Status: `approved`. v2 §25, §30.

---

## Stage gates

| Gate | Requirement |
|---|---|
| Stage 0 completion | all tests pass · `doctor` runs · domain model reviewed · `STORAGE_SCHEMA.md` reviewed · `git status` clean after one commit |
| Stage 0 → Stage 1 | explicit human approval of Stage 0 **and** of the storage schema |
| Storage implementation | only after `docs/STORAGE_SCHEMA.md` is explicitly approved |

---

## Open items (recorded, not decided)

1. **Overnight service windows** (`end_local <= start_local`, e.g. 22:00–02:00) are not supported:
   such a window is rejected as `InvalidServiceWindowError` rather than silently interpreted as a
   next-day close. Needs a decision before real customers with night hours are imported.
2. **Soft / preferred windows** — lateness penalties may only be introduced together with an explicit
   soft-window concept (D13 amendment, v2 §11).
3. **`algorithm_baseline` definition** (which heuristic, which tie-breaking) — Stage 2, v2 §30.
4. **Active-leg protection** implementation — Stage 5, v2 §18.
5. **Which capabilities are genuinely `unsupported`** rather than `requires_provider` — e.g. whether
   any future component is expected never to exist. Decide when the component is first requested.

*Resolved since the last revision:* the AUTO semantics (revoked, D4); `pinned_via` (removed, D6);
the unlocked-choice question (v2 §5 makes that state invalid, so D5 now says a selection is always
pinned); order-override persistence (D30, versioned JSON); the specification conflict (v2 published
as the current Source of Truth, v1 kept unchanged as history).

## Stage 2 change set — required by v2, not yet implemented

Recorded so nothing is silently dropped; each item is a real model or engine change:

1. **Complete-route candidate metrics** (v2 §12): first-leg travel, first-stop ETA, waiting and
   service start, complete travel time, complete waiting time, total service time, complete route
   duration, estimated final arrival at FINISH, hard-window violations, objective breakdown. This
   also changes `feasible` from first-leg feasibility to **complete-route** feasibility (v2 §14).
2. **`no_fully_feasible_route`** must be produced with the rejected candidates and their violating
   stops and reasons (v2 §14). The status exists in the domain already; the engine does not yet
   compute complete routes, so nothing produces it.
3. **Route fingerprint** (v2 §7, §35): `RoutePlan.inputs_fingerprint()` deliberately excludes the
   driver's decision; the committed route needs its own fingerprint that *does* depend on the
   selected first stop. Add it to the plan/run model and to `route_optimization_runs`.
4. ~~**`input_position` on `RouteStop`** (v2 §25, §30)~~ **Done** in the spec-alignment commit
   (D33): the field exists, is unique per plan, allows gaps, is never rewritten, and
   `RoutePlan.user_baseline_order()` provides the BEFORE baseline.
5. **Objective model** (v2 §16): the real model is elapsed time (travel + waiting + service), with
   any preference for less idle waiting expressed as a configurable soft preference rather than a
   universal multiplier. The provisional `travel_time = 1 / waiting_time = 2` demo policy stays
   marked provisional until then.
6. **Exhaustive candidate evaluation with measured performance** (v2 §20): evaluate the complete route
   for **every** feasible candidate, with no fixed K prefilter, and a deterministic benchmark harness.
   Targets for ~100 stops: ≤ ~3 s preferred, ≤ ~5 s acceptable. If the budget is exceeded: measure
   the bottleneck, improve caching/reuse/algorithm, benchmark again, and only then propose
   prefiltering or approximation as an explicit decision.
7. **Optimizer guarantees** (v2 §21): START fixed, FINISH fixed, driver-selected first stop fixed,
   every enabled stop exactly once, disabled stops excluded, hard-window feasibility explicit, no
   accepted local-search move may worsen the accepted objective, deterministic tie-breaking.
8. **Three baselines** (v2 §30): USER (`input_position` order), OPTIMIZED (around the driver's
   selection), ALGORITHM (greedy seed before local improvement). Keep the algorithm baseline out of
   user-facing BEFORE/AFTER.

## Environment notes (machine-specific, not product decisions)

- This development machine has **no working network** (the configured SOCKS proxy
  `127.0.0.1:10801` refuses connections and direct HTTPS fails), so `python -m pip install tzdata`
  cannot currently succeed.
- `zoneinfo` therefore has no IANA database by default, but a valid **TZif** tree exists at
  `C:\Program Files\Git\mingw64\share\zoneinfo` (IANA version **2026a**, from Git for Windows).
- Mechanism used: the standard `PYTHONTZPATH` search path (or `zoneinfo.reset_tzpath()`).
  Production code never activates it silently; the test bootstrap does, and prints a warning.
  `doctor` detects and reports it, and always prints the real fix: `python -m pip install tzdata`.
