# RoutePilot — Storage Schema (PROPOSAL ONLY)

> **Status: proposal, refined during Stage 1.** Per D14, **no SQLite code may be written until this
> document is reviewed and explicitly approved.** Nothing here is implemented.

Target: SQLite, stdlib `sqlite3` (no ORM). Storage lives in `storage/` and depends on `core/`;
`core/` never imports storage. Repository interfaces are defined as Protocols in `core/` and
implemented in `storage/sqlite/`.

---

## 1. Conventions

- **Time**: every absolute timestamp is stored as UTC ISO-8601 text with a trailing `Z`
  (`2026-09-11T01:00:00Z`). No local timestamps in storage.
- **Time zone**: IANA identifier text on the plan (`Europe/Moscow`). Never a numeric offset (D2).
- **Durations**: integer seconds (`*_sec`).
- **Booleans**: `INTEGER` 0/1.
- **Service windows**: stored as `window_kind` + **local wall-clock** start/end (`HH:MM:SS`), never
  as resolved instants. The window is a property of the customer's local opening hours; it is
  resolved per service date at compute time, and strict DST validation (D3) happens there. Storing
  resolved instants would bake in both a date and a DST decision.
- **Derived data is not stored**: timelines are recomputable from plan + matrix + policy, so they are
  not persisted (see open question 2).
- **JSON columns** hold domain value objects that are not queried relationally; they are validated by
  the domain on load, not trusted.
- Columns named `*_json` are `TEXT` containing JSON.

## 2. `schema_migrations`

| column | type | notes |
|---|---|---|
| `version` | INTEGER | PK, sequential |
| `applied_at_utc` | TEXT | NOT NULL |

Migrations are ordered files under `storage/sqlite/migrations/` (`0001_init.sql`, ...), applied in
order, never edited after release, never destructive by default.

## 3. `route_plans`

| column | type | notes |
|---|---|---|
| `id` | TEXT | PK (uuid text) |
| `name` | TEXT | NULL |
| `timezone` | TEXT | NOT NULL, IANA, e.g. `Europe/Moscow` |
| `departure_label` | TEXT | NOT NULL — START label |
| `departure_latitude` | REAL | NOT NULL |
| `departure_longitude` | REAL | NOT NULL |
| `departure_time_utc` | TEXT | NOT NULL |
| `finish_label` | TEXT | NOT NULL — FINISH label |
| `finish_latitude` | REAL | NOT NULL |
| `finish_longitude` | REAL | NOT NULL |
| `route_mode` | TEXT | NOT NULL, CHECK in (`FASTEST`,`SHORTEST`,`MINIMUM_TURNS`,`ON_THE_WAY`,`START_TO_FINISH`,`SMART_ROUTE`) |
| `first_stop_mode` | TEXT | NOT NULL, CHECK in (`recommend`,`manual`) — the old `auto` value no longer exists (D4) |
| `first_stop_selected_stop_id` | TEXT | NULL, FK → `route_stops(id)` — the **driver's** choice; NULL means `awaiting_first_stop_choice` (D11/D32) |
| `first_stop_selection_source` | TEXT | NULL, CHECK in (`accepted_recommendation`,`manual_choice`) — how the driver chose; NULL while nothing is selected (D6) |
| `first_stop_pinned` | INTEGER | NOT NULL DEFAULT 0 — set when the driver selects (pinned by default, D5) |
| `window_end_policy` | TEXT | NOT NULL DEFAULT `'service_finish_before_end'`, CHECK in (`service_start_before_end`,`service_finish_before_end`) — what the end of a fixed window means for this plan (D29) |
| `order_overrides_json` | TEXT | NOT NULL DEFAULT `'{"version": 1, "constraints": []}'` — **versioned** envelope of user ordering constraints (D21/D30) |
| `cost_policy_json` | TEXT | NOT NULL — policy in force for the plan |
| `default_service_duration_sec` | INTEGER | NULL — used when a stop's duration is unknown |
| `inputs_fingerprint` | TEXT | NULL — fingerprint of the last computed solution (D4) |
| `data_provenance` | TEXT | NOT NULL, CHECK in (`DEMO_SYNTHETIC`,`REAL_ROUTING`) |
| `created_at_utc` | TEXT | NOT NULL |
| `updated_at_utc` | TEXT | NOT NULL |

Notes:

- START and FINISH are **not** rows in `route_stops` (I1/I2). Enforcing this structurally removes the
  possibility of a start location being served.
- The plan stores the driver's **decision** (mode, selected stop, provenance, pinned). It never
  stores `recommended_stop_id`: a recommendation is derived, recomputable, and must never be mistaken
  for a decision (D4/D11/D32).
- `first_stop_selected_stop_id` is NULL in the normal pre-choice state
  `awaiting_first_stop_choice`; no placeholder stop is ever written (D9).
- `first_stop_selection_source` records **how the driver chose** and is never written by the engine
  (D6); a recommendation carries no provenance at all.

Indexes: none beyond the PK (plans are few).

### 3.1 Versioned order-override envelope (D30)

For the MVP, general order overrides are stored as a structured, **versioned** JSON envelope rather
than a normalized table, because only first-stop pinning is implemented and the final constraint
vocabulary for arbitrary drag/reorder is not known yet:

```json
{
  "version": 1,
  "constraints": [
    {"kind": "first_stop", "stop_id": "S07", "position": null}
  ]
}
```

Rules:

- `version` is mandatory; readers reject an unknown version instead of guessing;
- the envelope is validated by the domain on load, and a `first_stop` constraint must agree with
  `first_stop_pinned_stop_id` (one source of truth, D21);
- because the format is versioned, it can migrate to a normalized `route_stop_order_constraints`
  table later **without changing the core domain model**.

## 4. `route_stops`

| column | type | notes |
|---|---|---|
| `id` | TEXT | PK (uuid text) |
| `plan_id` | TEXT | NOT NULL, FK → `route_plans(id)` ON DELETE CASCADE |
| `input_position` | INTEGER | NOT NULL — the order **as supplied by the user** |
| `raw_address` | TEXT | NOT NULL |
| `normalized_address` | TEXT | NULL |
| `latitude` | REAL | NULL |
| `longitude` | REAL | NULL |
| `geocode_status` | TEXT | NOT NULL, CHECK in (`pending`,`resolved`,`ambiguous`,`failed`) |
| `geocode_provider` | TEXT | NULL — which provider produced the coordinates |
| `geocode_checked_at_utc` | TEXT | NULL |
| `service_window_kind` | TEXT | NOT NULL, CHECK in (`fixed`,`unrestricted`,`unknown`) |
| `service_window_start` | TEXT | NULL — local `HH:MM:SS` |
| `service_window_end` | TEXT | NULL — local `HH:MM:SS` |
| `window_end_policy` | TEXT | NULL — per-stop override; NULL = inherit the plan default (D29) |
| `service_duration_sec` | INTEGER | NULL — NULL means unknown, never invented |
| `priority` | INTEGER | NULL |
| `service_status` | TEXT | NOT NULL DEFAULT `'pending'`, CHECK in (`pending`,`in_progress`,`served`,`failed`,`skipped`) |
| `enabled` | INTEGER | NOT NULL DEFAULT 1 |
| `notes` | TEXT | NULL |
| `created_at_utc` | TEXT | NOT NULL |
| `updated_at_utc` | TEXT | NOT NULL |

Table-level CHECK constraints (mirroring the domain value objects):

```sql
-- window shape (D28)
CHECK (
  (service_window_kind = 'fixed'
     AND service_window_start IS NOT NULL AND service_window_end IS NOT NULL)
  OR
  (service_window_kind <> 'fixed'
     AND service_window_start IS NULL AND service_window_end IS NULL)
)
-- coordinates exist exactly when geocoding succeeded
CHECK (
  (geocode_status = 'resolved' AND latitude IS NOT NULL AND longitude IS NOT NULL)
  OR (geocode_status <> 'resolved')
)
-- a fixed customer window implies a resolved customer location
CHECK (service_window_kind <> 'fixed' OR latitude IS NOT NULL)
-- only a fixed window has an end whose meaning can be chosen (D29)
CHECK (window_end_policy IS NULL OR service_window_kind = 'fixed')
```

Indexes:

- `idx_route_stops_plan` on (`plan_id`, `input_position`) — reconstructs the user's BEFORE order (D22);
- `idx_route_stops_plan_enabled` on (`plan_id`, `enabled`) — active stop set.

`input_position` is the storage counterpart of the user-supplied baseline: without it, the BEFORE
metrics that the product promises (§25) cannot be reproduced after a reload.

## 5. `route_optimization_runs`

Immutable history: one row per optimize/reoptimize execution.

| column | type | notes |
|---|---|---|
| `id` | TEXT | PK (uuid text) |
| `plan_id` | TEXT | NOT NULL, FK → `route_plans(id)` ON DELETE CASCADE |
| `run_kind` | TEXT | NOT NULL, CHECK in (`optimize`,`reoptimize`,`preview`) |
| `algorithm` | TEXT | NOT NULL, e.g. `greedy_seed+2opt` |
| `algorithm_version` | TEXT | NOT NULL |
| `inputs_fingerprint` | TEXT | NOT NULL |
| `tzdata_version` | TEXT | NULL — IANA data version used (D2) |
| `cost_policy_json` | TEXT | NOT NULL — policy actually used by this run |
| `data_provenance` | TEXT | NOT NULL, CHECK in (`DEMO_SYNTHETIC`,`REAL_ROUTING`) |
| `status` | TEXT | NOT NULL, CHECK in (`ok`,`has_infeasible_windows`,`unresolved_first_stop`) |
| `order_json` | TEXT | NOT NULL — ordered stop ids |
| `first_stop_recommendation_json` | TEXT | NOT NULL — the recommendation the run showed: recommended id, ranked top-K outcomes, status, diagnostics. Never a decision (D32) |
| `top_k_json` | TEXT | NULL — ranked candidates with explanations (§9) |
| `violations_json` | TEXT | NOT NULL — explicit infeasibilities (D13 amendment) |
| `metrics_json` | TEXT | NOT NULL — see below |
| `created_at_utc` | TEXT | NOT NULL |

`metrics_json` shape:

```json
{
  "user_baseline":      { "distance_m": 0, "duration_sec": 0, "waiting_sec": 0, "feasible": true },
  "algorithm_baseline": { "distance_m": 0, "duration_sec": 0, "waiting_sec": 0, "feasible": true },
  "after":              { "distance_m": 0, "duration_sec": 0, "waiting_sec": 0, "feasible": true },
  "saved_distance_m": 0,
  "saved_duration_sec": 0
}
```

`user_baseline` is captured here too (as well as in `input_position`) so a historical run stays
interpretable even if stops are later reordered or disabled. `algorithm_baseline` is stored as a
separate key and is never presented as the user's BEFORE route (D22).

Index: `idx_runs_plan_created` on (`plan_id`, `created_at_utc`).

## 6. `app_settings`

| column | type | notes |
|---|---|---|
| `key` | TEXT | PK |
| `value_json` | TEXT | NOT NULL |
| `updated_at_utc` | TEXT | NOT NULL |

Intended keys: `default_timezone`, `doctor_mode`, `default_data_provenance`, `tile_url`,
`tile_attribution`, `tile_max_zoom`. Tile configuration lives here so the map vendor stays
configuration-isolated (D15) and never leaks into `core/`.

## 7. Repository interfaces (to be implemented in Stage 3)

Proposed Protocols in `core/` (implemented in `storage/sqlite/`):

- `RoutePlanRepository`: `save(plan)`, `get(plan_id)`, `list()`, `delete(plan_id)`
- `RouteOptimizationRunRepository`: `append(run)`, `list_for_plan(plan_id)`, `latest(plan_id)`
- `AppSettingsRepository`: `get(key)`, `set(key, value)`

Save/load must round-trip a plan exactly (`RoutePlan == loaded RoutePlan`) — spec §27.21. Loading is
where the domain re-validates every value object, so a hand-edited database fails loudly instead of
producing a half-valid plan.

## 8. Deliberately not stored

| Data | Reason |
|---|---|
| Timelines, ETA, waiting | derived from plan + matrix + policy; recomputable, and storing them invites stale truth |
| Matrices | large, provider-owned, cacheable separately later |
| `recommended_stop_id` on the plan | it is derived and recomputable; storing it would turn an advisory recommendation into apparent plan truth (D32) |
| Route geometry | provider data, requested in chunks later (D18) |

## 9. Open questions for the schema review

1. ~~**Order overrides**: normalized table vs JSON.~~ **Resolved (D30):** versioned JSON envelope
   now; a normalized table only when drag/reorder and position constraints actually appear.
2. **Timeline snapshot for audit**: store per-run timelines (JSON) or rely on recomputation with the
   run's `inputs_fingerprint` + `cost_policy_json` + `tzdata_version`? Proposal: recompute; add a
   snapshot only if audit requirements demand it.
3. **Multi-plan / multi-driver future**: `driver_id`, `vehicle_id`, org scoping — out of scope now,
   but the plan table should not need restructuring to add them.
4. **Settings scope**: global `app_settings` now; per-user/per-organization later.
5. **Retention**: how many optimization runs to keep per plan (proposal: keep all in the demo, add a
   retention policy when real volumes appear).

## 10. Implementation status

This document is a **design proposal only**. Per D14, no SQLite code exists and none will be written
until this schema is explicitly approved. The Stage 1 refinements recorded above — the window end
policy on the plan and per stop (D29) and the versioned order-override envelope (D30) — are part of
the proposal, not of an implementation.
