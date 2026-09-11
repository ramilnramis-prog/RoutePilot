# RoutePilot — Decision Registry

This file is the **only** place where a decision counts as settled. Chat/session history is
not a decision log. The current Source of Truth for *what* the product must do is
[`PRODUCT_SPEC_v2.md`](PRODUCT_SPEC_v2.md); [`PRODUCT_SPEC.md`](PRODUCT_SPEC.md) v1 is historical
and unchanged. Precedence (v2 section 37): the current specification plus explicitly approved later
decisions in this registry control future implementation; this registry records *how* we decided to
do it.

- Registry revision: **D1–D36**, approved 2026-09-11 (Stage 0, extended during Stage 1; D4–D11
  amended when the AUTO semantics were revoked; D5/D9/D16 aligned with v2 sections 5, 14 and 23;
  D33 added for `input_position`; D34 records the owner-accepted interim ~100-stop latency and is
  amended by D36, which moves the performance target to ~50 enabled stops; D35 settles the default
  SMART_ROUTE objective as the complete elapsed route duration with the owner's deterministic 5-key
  ranking, and supersedes D31 for the default; D36 records the owner's Stage 2.1 scale decision).
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

## D31 — Provisional demo weights: the waiting-preference sensitivity study *(superseded for the default SMART_ROUTE objective; retained as the waiting-preference sensitivity study)*

> **Amended 2026-09-11 (D35): superseded for the default SMART_ROUTE objective; retained as the
> waiting-preference sensitivity study.** The objective in force is no longer this policy: it is the
> **complete elapsed route duration** (D35). `demo_provisional_v1` is kept, still marked
> `provisional` in code and in the report, purely as the labelled **non-default** study that shows
> what a *non-zero* waiting preference would do. Nothing in D31 is the product objective any more.

- The project now ships **two** weighted policies, and only one of them is a default:
  `smart_route_elapsed_v1` (`travel_time = 1`, `waiting_time = 1`, **not** provisional) is the
  default SMART_ROUTE objective (D35); `demo_provisional_v1` (`travel_time = 1`, `waiting_time = 2`)
  is the **non-default** provisional study policy described by this decision.
- These numbers are **not product truth**: they exist to demonstrate the architecture and to show
  what a non-zero waiting preference does. The demo report shows their sensitivity **over the
  complete-route outcomes** - travel weight fixed at 1, waiting weight 1.0 / 1.5 / 2.0 / 3.0, each
  row the recommended first stop of an exhaustive evaluation of the same candidate set under that
  policy (`demo.report.weight_sensitivity`).
- The **degenerate 1:1 case** is reported, and what it shows on the demo fixture is recorded here so
  the claim is not stale. At waiting weight 1.0 four candidates (`S09-ALWAYS-OPEN`,
  `S23-UNKNOWN-HOURS2`, `S08-UNKNOWN-HOURS`, `S19-ALWAYS-OPEN2`) tie at the winning objective
  30 840, and the deterministic ranking key of D35 - `(complete elapsed duration, complete travel
  time, complete waiting time, input_position, stop_id)`, **not** the objective - hands the
  recommendation to `S23-UNKNOWN-HOURS2` (all four tie on duration 38 940; it drives least). This
  1:1 row is numerically the shipped default objective (D35), so it is also a restatement of it.
- At 1.5, 2.0 and 3.0 the **weighted objective alone** would prefer `S25-ON-OPENING`
  (`scores 34 980 / 38 340 / 45 060` against `S23-UNKNOWN-HOURS2`'s `35 550 / 40 260 / 49 680`),
  while the shipped recommendation stays `S23-UNKNOWN-HOURS2`, because the D35 key puts complete
  elapsed duration first. The report says exactly that in each row's note, so the study still shows
  a real sensitivity - of the objective, and of the fact that the shipped ranking does not follow a
  non-zero waiting preference.
- The capability gate still applies: a weight may only be set for a component whose status is
  `implemented`, so nothing unimplemented can be scored silently.
- Status: `approved` (amended by D35 for the default objective). Spec: §10, §16, §24.

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

## D34 — Interim ~100-stop exhaustive-latency limitation (owner decision) *(amended by D36)*

> **Amended 2026-09-11 (D36): this decision is restated under the new scale target and is no longer
> an MVP performance gate at 100 stops.** The primary MVP performance target is now approximately
> **50 enabled service stops** (D36). The measured ~100-stop exhaustive latency recorded here, and
> the **deferred incremental / delta complete-route evaluator** that was scheduled to remove it, are
> both restated under that target: at ~100 stops the loop is the **engineering stress reference**,
> which is **not performance-qualified** in this MVP, and **failing the old ≤ ~5 s target at 100
> stops does NOT block the portfolio MVP**. Nothing else in D34 changes: the full U2 neighbourhood,
> the restored search quality, the exhaustive candidate set with no prefilter/shortlist/approximation
> and the deterministic per-candidate evaluation ceiling all stay exactly as written below, and the
> ~100-stop fixture and its tests stay in the repository. The deferred evaluator becomes an
> engineering-scale improvement rather than a gate on the portfolio MVP.

- **Owner decision (governing).** The measured latency of the exhaustive first-stop candidate loop
  at ~100 stops (measured warm **~63–76 s** at **97 enabled stops**) is **accepted as an explicit,
  recorded interim limitation**.
- The v2 section 20 targets (preferred ≤ ~3 s, acceptable ≤ ~5 s for ~100 stops) are **not** met at
  that scale. They remain *reported* engineering targets — v2 section 20 calls them **"engineering
  targets, not correctness rules"** — and are never asserted as satisfied by the shipped loop. Under
  D36 they are the reported targets of the **~50-enable-stop portfolio fixture** instead, and at
  ~100 stops they are reported for the stress reference only.
- **Search quality was deliberately preferred over the time target.** The full U2 neighbourhood and
  the restored search quality stay: no approximation, **no candidate prefilter, no neighbourhood
  span cut, no shortlist, no weight tuning**.
- The **candidate set stays exhaustive**: the complete route of **every** eligible first-stop
  candidate is evaluated, with no fixed-K prefilter. The per-candidate local search keeps its
  **deterministic evaluation ceiling** (`max_evaluations` / `budget_exhausted`), which binds at
  ~100 stops and truncates pass 1; a truncated pass is **reported** and is never claimed as
  exhaustive verification of the search.
- The 30-stop product demo stays inside the ≤ ~5 s acceptable target.
- **Scheduled follow-up:** a dedicated later Stage 2 work unit builds an **incremental / delta
  complete-route evaluator** to remove the ~100-stop latency. This decision is superseded when that
  unit lands and the loop is re-benchmarked; meanwhile the interim latency is the accepted state.
  **Amended by D36:** this follow-up is no longer a condition of the portfolio MVP, and at the
  ~50-stop primary target the shipped exact loop is measured and reported honestly rather than
  optimized with any quality-degrading shortcut.
- Status: `approved` (amended by D36: the ~5 s-at-100-stops requirement is no longer an MVP gate,
  and the interim latency and the deferred evaluator are restated under the ~50-stop target).
  Spec: §20.

---

## D35 — SMART_ROUTE default objective: complete elapsed duration (owner decision)

- **Owner decision.** The default SMART_ROUTE objective is the **complete elapsed route duration**:
  travel + waiting + service over the whole route, the FINISH leg included. For a fixed departure
  that is equivalent to the estimated **FINISH arrival time**, so the product objective is "finish
  the whole route sooner" - not "drive less" and not "wait less".
- Implemented as `core.model.cost_policy.smart_route_elapsed_policy()`, policy name
  `smart_route_elapsed_v1`, weights `travel_time = 1`, `waiting_time = 1`, `provisional = False`.
  It is the default of `demo.dataset.build_demo_plan`, `demo.scale_dataset.build_scale_plan` and the
  test plan helper.
- **Service time is reported, never scored.** Every candidate of one plan serves exactly the same
  enabled stops, so `total_service_time` is constant across the candidates of that plan. Scoring
  travel and waiting at 1:1 therefore **is** the elapsed-duration objective and is **not** a hidden
  weight: with those weights the reported score equals travel + waiting and equals the complete
  duration minus that constant service time.
- **The default additional waiting preference is zero.** No weight was tuned to favour any
  candidate. A preference for less idle waiting may only be introduced later as an explicit,
  configurable **soft** preference (v2 section 16), never as a silent universal multiplier.
- **Deterministic ranking key - the owner's 5-tuple:** (1) complete elapsed duration, (2) complete
  travel time, (3) complete waiting time, (4) `input_position`, (5) `stop_id`, all taken from the
  candidate's **complete-route** metrics, FINISH leg included. The weighted objective stays a
  **reported** figure and is deliberately not a key component, so no weighting is hidden in the
  tie-break. `input_position` remains the immutable input-order provenance of D33.
- **The optimizer's own acceptance key already matched.** `core.engine.optimizer` accepts a move
  lexicographically on `(violations, elapsed seconds)`, so the ranking and the optimizer optimize
  the same quantity; this decision aligns the stated objective with the engine that already ships.
- **Audit trail.** The demo report prints an `OBJECTIVE ALIGNMENT` table for departures 04:00-08:00
  showing, per hour, the previous D31-provisional recommendation and the new elapsed-duration
  recommendation with its FINISH, complete travel, complete waiting, total service and feasibility.
  The previous column is reconstructed with the **pre-D35** key
  `(score, complete duration, input_position, stop_id)` so the comparison is like for like.
- **Measured consequence (recorded, not tuned).** At 04:00 / 05:00 / 06:00 the shipped
  recommendation moves from `S25-ON-OPENING` / `S25-ON-OPENING` / `S08-UNKNOWN-HOURS` to
  `S23-UNKNOWN-HOURS2` (at 04:00: complete duration 10h49m, FINISH 14:49 local, complete travel
  5h57m, waiting 2h37m, service 2h15m, fully feasible). 07:00 (`S14-PRIORITY-2`) and 08:00
  (`S01-NEAR`) are unchanged. Nothing was re-tuned to preserve the previous winner, and the raw
  fixture was not re-calibrated to keep it either.
- D31 is superseded **for the default objective only**; D13 (configurable policy), D16 (capability
  gate), D32 (recommendation is not selection), D33 (`input_position` provenance) and D34 (interim
  ~100-stop latency) are unaffected.
- Status: `approved`. Spec: §10, §12, §13, §16, §30, §33.

---

## D36 — Scale: ~50-stop portfolio MVP target (owner decision)

> **Authorization.** This decision records the owner's **Stage 2.1 authorization, sections A, E and
> F** (G applies). It restores the scale decision that an earlier cycle dropped from the registry
> because a reviewer brief wrongly omitted those sections from the authorized scope. Section A sets
> the primary product scale target at approximately **50 service stops** and demotes 100 stops;
> section E requires a deterministic ~50-stop benchmark fixture, an exhaustive complete-route
> first-stop measurement with preferred ≤ ~3 s / acceptable ≤ ~5 s, no prefilter, no approximate
> ranking, no quality-degrading shortlist, no fake performance claims and no over-optimization when
> the exact implementation already meets the target; section F keeps the ~100-stop benchmark as an
> honestly reported stress benchmark whose old ≤ ~5 s target is **not** an MVP requirement. Nothing
> here is a new product scope: it is the continuation of unit U6 after a Supervisor contract defect.

- **Primary MVP target: up to approximately 50 enabled service stops.** The primary product scale
  target is now approximately 50 service stops, and the exhaustive complete-route first-stop loop is
  performance-qualified at that scale.
- **100 stops is no longer a hard MVP performance requirement.** 100-stop support is documented as
  **future scale / stress benchmark / not performance-qualified in this MVP**, and failure to meet
  ≤ ~5 seconds at 100 stops does **not** block the portfolio MVP. The ~100-stop fixture, its tests
  and its recorded measurement are **retained**, and the ~100-stop benchmark keeps reporting its
  **honest measured number** and the owner-accepted bound of D34.
- The owner's sentence, verbatim, is recorded here and printed by the demo report and the benchmark:
  **"Portfolio MVP performance target: ~50 stops. 100-stop exhaustive optimization is supported as
  an engineering stress scenario but is not yet performance-optimized."**
- **~50-stop targets:** preferred **≤ ~3 s**, acceptable **≤ ~5 s** for the exhaustive complete-route
  first-stop evaluation over every enabled candidate of the portfolio fixture. Both are *reported*
  engineering targets (v2 section 20), never correctness rules, and the benchmark prints the measured
  number with its honest verdict rather than guarding it by a flaky exact ≤ 5 s assertion. What the
  benchmark **does** assert at the primary MVP scale is the **same generous owner-accepted regression
  bound D34 named** (`ACCEPTED_INTERIM_LOOP_LIMIT_SEC`, ~150 s - roughly seven times the measured
  ~19–25 s at 50 enabled stops), so the primary MVP scale has a real regression guard and the stress
  scale keeps the identical bound (U6b review fix).
- **The fixture.** `demo.scale_dataset.build_portfolio_plan` builds the deterministic portfolio
  fixture: `PORTFOLIO_STOP_COUNT = 55` stops of which `PORTFOLIO_DISABLED_STOP_COUNT = 5` are
  disabled by the fixture's own deterministic policy (every `PORTFOLIO_DISABLED_EVERY = 10`-th
  stop), so it holds exactly **`PORTFOLIO_ENABLED_STOP_COUNT = 50` enabled stops** and is labelled
  with that enabled count. The ~100-stop default and its tests are untouched: the fixture's disabled
  policy is a parameter, and the ~100-stop plan is unchanged and byte-identical. A fixture with
  materially fewer enabled stops is never labelled a "50-stop" fixture without stating its enabled
  count.
- **The measured number is reported, never faked.** Under the shipped exact implementation the
  ~50-stop warm exhaustive loop on this development machine measures **~19–25 s** (about 0.39–0.49 s
  per candidate, every candidate hitting the deterministic per-candidate evaluation ceiling), i.e.
  it is **outside** the ≤ ~5 s acceptable target. That number is printed as it is by the demo report
  and by `tools/benchmark_optimizer.py` (measured 2026-09-11: 50 enabled stops, 24.49 s warm,
  489.89 ms per candidate; the ~100-stop stress reference measured 85.46 s in the same run). It was
  **not** improved by a candidate prefilter, an approximate ranking, a quality-degrading shortlist, a
  weight change or a shortened neighbourhood - all of which remain forbidden - and no figure is
  estimated or borrowed from another scale. The asserted bound at this scale is the generous
  owner-accepted regression bound of D34 (~150 s), not the ≤ ~5 s engineering target, and no
  approximation was introduced. The profiling shows the cost is inside the complete-route
  evaluations themselves (one full
  `RouteProblem` evaluation per candidate move, one million evaluations for the 50-candidate loop),
  i.e. it needs the **incremental / delta complete-route evaluator** that D34 already defers; that is
  an algorithmic rewrite and is therefore out of scope here.
- **The architecture must not be redesigned around a hard 50-stop maximum.** The scale decision
  changes the *performance target*; it introduces **no new hard validation limit** and no hard
  maximum stop count. The domain, the optimizer and the fixture generator stay able to evolve to
  dozens → ~100 → 100+ stops (D18), and the ~50-stop fixture is a **scale subset** of the same
  deterministic generator as the ~100-stop one.
- **D34 is amended by this decision:** its interim 100-stop latency and its deferred
  incremental/delta evaluator are restated under the new ~50-stop target, and the ~5 s-at-100-stops
  requirement is explicitly **no longer an MVP gate**.
- **Where the scale decision is visible:** the registry revision (D1–D36), the Stage 2 change-set
  item 6 note, this entry, `docs/ARCHITECTURE.md`, `README.md`, the demo report's scale/performance
  block, the benchmark tool's per-fixture profiles and the scale tests.
- Status: `approved`. Spec: §19, §20, §13 (D18). Owner authorization: Stage 2.1 sections A, E, F.

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

## Stage 2 change set — status

Recorded so nothing is silently dropped. Each item is a real model or engine change. **Stage 2 is
complete** (units U1–U6); the status of every item is recorded here, and the one item that is still
open is marked OPEN.

1. **Complete-route candidate metrics** (v2 §12): first-leg travel, first-stop ETA, waiting and
   service start, complete travel time, complete waiting time, total service time, complete route
   duration, estimated final arrival at FINISH, hard-window violations, objective breakdown. This
   also changes `feasible` from first-leg feasibility to **complete-route** feasibility (v2 §14).
   **DONE** (U1/U4; `core.engine.first_stop.evaluation`, `core.model.first_stop.FirstStopCandidate`).
2. **`no_fully_feasible_route`** produced with the rejected candidates, their violating stops and
   their reasons (v2 §14). **DONE** (U4; `FirstStopEvaluationReport.rejected` + `diagnostics` +
   `reasons_for`). The demo report prints them grouped by candidate with the violating stop ids, and
   the shipped fixture exercises the path (`S32-EARLY-CLOSE` rejects 5 of 31 candidates at 04:00).
3. **Route fingerprint** (v2 §7, §35): `RoutePlan.inputs_fingerprint()` deliberately excludes the
   driver's decision; the committed route has its own fingerprint that *does* depend on the selected
   first stop. **DONE** (U2; `core.engine.optimizer.route_fingerprint`).
4. **`input_position` on `RouteStop`** (v2 §25, §30). **DONE** in the spec-alignment commit (D33).
5. **Objective model** (v2 §16) — **DONE** (U6; `core.model.cost_policy.smart_route_elapsed_policy`,
   **D35**). The default SMART_ROUTE objective is now the **complete elapsed route duration** (travel
   + waiting + service, equivalently the FINISH arrival time for a fixed departure) with a **zero**
   default waiting preference, and the deterministic ranking key is the owner's 5-tuple (complete
   elapsed duration, complete travel, complete waiting, `input_position`, `stop_id`). Service time is
   constant across the candidates of one plan and is reported, never scored, so travel + waiting at
   1:1 **is** the elapsed-duration objective rather than a hidden weight - and the optimizer's own
   acceptance key was already `(violations, elapsed seconds)`. The historical `demo_provisional_v1`
   weights of **D31** are **not** the default any more: D31 is superseded for the default objective
   and survives only as the labelled non-default waiting-preference sensitivity study. A preference
   for less idle waiting remains available only as an explicit configurable **soft** preference,
   never as a universal multiplier, and no weight was tuned to produce a demo winner.
6. **Exhaustive candidate evaluation with measured performance** (v2 §20): evaluate the complete
   route for **every** feasible candidate, with no fixed-K prefilter, and a deterministic benchmark
   harness. **DONE** (U3/U4, extended by U6b; `tools/benchmark_optimizer.py`). **The scale target is
   ~50 enabled stops (D36).** The benchmark measures three fixtures and labels each with its exact
   enabled count and DEMO/SYNTHETIC provenance: the ~30-stop demo plan, the ~50-enabled-stop
   **portfolio fixture** (`demo.scale_dataset.build_portfolio_plan`: 55 stops, 50 enabled - the
   **primary MVP scale target**, preferred ≤ ~3 s / acceptable ≤ ~5 s reported), and the ~100-stop
   **stress fixture** (97 enabled stops, **engineering stress reference, not performance-qualified**;
   the ≤ ~5 s target at that scale is not an MVP gate). The ~100-stop loop exceeds the ≤ ~5 s
   acceptable target and is **accepted as an interim limitation** (D34, amended by D36); the
   ~50-stop loop is measured and reported honestly (warm ~19–21 s on this machine) rather than
   optimized with any quality-degrading shortcut, because closing that gap needs the **incremental /
   delta complete-route evaluator** - the one **DEFERRED** Stage 2 deliverable - and a real
   algorithmic rewrite is out of scope for this unit. Until it lands, no prefilter, no shortlist and
   no approximation is authorized, and the candidate set stays exhaustive at every scale.
7. **Optimizer guarantees** (v2 §21): START fixed, FINISH fixed, driver-selected first stop fixed,
   every enabled stop exactly once, disabled stops excluded, hard-window feasibility explicit, no
   accepted local-search move may worsen the accepted objective, deterministic tie-breaking.
   **DONE** (U2/U3; `core.engine.optimizer`).
8. **Three baselines** (v2 §30): USER (`input_position` order), OPTIMIZED (around the driver's
   selection), ALGORITHM (greedy seed before local improvement). **DONE** (U2/U3; the algorithm
   baseline stays out of user-facing BEFORE/AFTER, and the demo report shows all three).

## Environment notes (machine-specific, not product decisions)

- This development machine has **no working network** (the configured SOCKS proxy
  `127.0.0.1:10801` refuses connections and direct HTTPS fails), so `python -m pip install tzdata`
  cannot currently succeed.
- `zoneinfo` therefore has no IANA database by default, but a valid **TZif** tree exists at
  `C:\Program Files\Git\mingw64\share\zoneinfo` (IANA version **2026a**, from Git for Windows).
- Mechanism used: the standard `PYTHONTZPATH` search path (or `zoneinfo.reset_tzpath()`).
  Production code never activates it silently; the test bootstrap does, and prints a warning.
  `doctor` detects and reports it, and always prints the real fix: `python -m pip install tzdata`.
