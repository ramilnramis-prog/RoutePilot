# RoutePilot

RoutePilot is a route planning and route optimization application for drivers, couriers,
delivery workers and dispatchers who may need to visit dozens to 100+ service locations
during one working route.

RoutePilot is **not** a turn-by-turn navigator. Its job is to understand a set of service
stops, where and when the driver starts, where the driver must finish, what constraints
each stop carries, and to produce a practical visiting order with a deterministic
explanation of why that order was chosen — while leaving the driver in control.

The current product specification is [`docs/PRODUCT_SPEC_v2.md`](docs/PRODUCT_SPEC_v2.md)
(Source of Truth). [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md) is the historical v1, kept
unchanged for traceability. Core product principle since v2: **RoutePilot recommends, the driver
decides.**

## Status: Stage 4 (API transport + web demo workspace)

Implemented so far:

- domain skeleton (value objects, `RouteStop`, `RoutePlan`, `ServiceWindow` with an explicit
  `window_kind` and `window_end_policy`, first-stop intent/recommendation, cost policy, route modes,
  order overrides, `input_position` provenance);
- time layer: UTC storage, explicit IANA time zone per plan, **strict DST validation**;
- timeline arithmetic: ETA, waiting time, service start, estimated departure, lateness and the
  `service_finish_before_end` / `service_start_before_end` window-end policies;
- **complete-route evaluation** (`START → all enabled stops → FINISH`, FINISH leg included) with
  explicit hard-window violations and the three baselines (USER / OPTIMIZED / ALGORITHM);
- **deterministic optimizer**: constraint-aware greedy seed → 2-opt/Or-opt local improvement, a
  shared and measured leg cache, a prepared problem with a precomputed service-window table, and a
  **route fingerprint** for the committed route;
- **first-stop recommendation over complete routes**: every enabled stop is optimized as a
  candidate and ranked by the **complete elapsed route duration** (travel + waiting + service,
  equivalently the FINISH arrival time for a fixed departure) with a **zero** default waiting
  preference, using the owner's deterministic 5-key order (complete elapsed duration, complete
  travel, complete waiting, `input_position`, `stop_id`); rejected candidates are reported with
  their violating stops, and the driver still decides — nothing is applied automatically (D4/D32,
  D35);
- **deterministic demo scenario**: ~30 synthetic stops (31 enabled + 1 disabled), departure 04:00,
  many customers opening 08:00, one early-closing customer that makes some first-stop choices
  genuinely infeasible, and a complete-route report (`python -m demo.report`) that shows the
  ranking, the recommended candidate's complete route, the rejected candidates with their violating
  stops, the baselines, the departure-time sweep, the objective-alignment audit of the D35 change,
  the non-default waiting-preference sensitivity study, both fingerprints and the scale/performance
  block;
- **scale: ~50 enabled service stops is the primary MVP target** (D36). 100 stops is future scale /
  an engineering **stress** reference and is **not performance-qualified** in this MVP: failing the
  old ≤ ~5 s target at 100 stops does **not** block the portfolio MVP. The domain has no hard
  50-stop maximum and the architecture stays able to evolve beyond it;
- **measured performance**: `tools/benchmark_optimizer.py` runs the exhaustive first-stop loop over
  the ~30-stop demo plan, the **~50-enabled-stop portfolio fixture** (the primary MVP target, with
  the preferred ≤ ~3 s / acceptable ≤ ~5 s targets reported) and the **~100-stop stress reference**,
  labelling each with its exact enabled count and DEMO/SYNTHETIC provenance. The measured
  ~100-stop latency is an accepted interim limitation (D34); the incremental/delta complete-route
  evaluator is implemented (Stage 2.2 U7, `core.engine.optimizer.local_search`) and makes the
  exhaustive loop about **2.5x** faster at the portfolio and stress scales and about **2.2x** faster
  on the ~30-stop demo plan, with identical results, and
  the ~50-stop figure is measured and **reported honestly** as it is (~8.0-8.5 s warm on the
  development machine, i.e. outside the ≤ ~5 s target and just above the owner's ≤ ~8 s
  "good enough" target), never improved with a prefilter, a
  shortlist, an approximate ranking or any other quality-degrading shortcut, and never
  asserted at a `≤ ~5 s` wall-clock second - the asserted guard at every measured scale, the primary
  MVP scale included, is the generous owner-accepted regression bound of D34 (~150 s);
- error taxonomy split from violations, `tools/doctor.py`, and a deterministic offline test suite;
- **storage is implemented** (Stage 3, U9–U12, D38) behind the approved
  [`docs/STORAGE_SCHEMA.md`](docs/STORAGE_SCHEMA.md): the ordered, idempotent migration runner and
  the byte-unchanged approved DDL (`storage/sqlite/migrations/0001_init.sql`,
  `storage/sqlite/database.py`), and three stdlib-`sqlite3` repositories implementing the pure
  Protocols in `core/repositories.py` - plan and stops (`SqliteRoutePlanRepository`), append-only
  immutable run history (`SqliteRouteOptimizationRunRepository`) and settings
  (`SqliteAppSettingsRepository`). `python -m demo.storage_roundtrip` saves the shipped demo plan,
  reloads it, re-runs the real engine on both, appends and reads back a real run and two settings,
  all in an in-memory database. The D38 non-negotiables hold: `core/` never imports storage, there
  is no ORM, no database file is committed, and a recommendation is never plan state;
- **the API and the web workspace are implemented** (Stage 4, U13–U16, D39): `api/` is a stdlib
  `http.server` transport plus a framework-agnostic service layer (no FastAPI, no other web
  framework, no new dependency) serving `GET /api/health`, `GET|POST /api/plans`,
  `GET|PUT /api/plans/{id}`, the engine-facing `GET /api/plans/{id}/recommendation`,
  `POST|DELETE /api/plans/{id}/selection`, `GET /api/plans/{id}/route`,
  `POST /api/plans/{id}/optimize` (the only endpoint that appends a run), `GET /api/plans/{id}/runs`,
  `GET /api/runs/{run_id}` and `GET|PUT /api/settings/{key}`; `web/` is a static HTML/CSS/JS
  workspace served by the same process. Recommendation, selection and route are **synchronous with
  per-plan single-flight** protection (no job queue), every number comes from `core`/`storage`, and
  a recommendation is still never plan state (D4/D32).

Not implemented yet (by design): real routing, geocoding and traffic providers, side-of-road logic,
active-leg protection, drag/reorder (D21), reoptimization after a served stop, any route mode other
than `SMART_ROUTE`, and any automatic application of a recommendation. No retention policy exists for
stored runs (every run is kept, D38), and the ~50-stop latency limitation of D36/D37 is unchanged.

**Every travel time and distance in the demo is synthetic** and is labelled as such. It is not road
routing and must never be shown as such.

## Requirements

- Python 3.11+ (developed and tested on 3.13)
- Git
- IANA time zone database — install it once:

```bash
python -m pip install tzdata
```

`tzdata` is the only runtime dependency and it is a pure data package. Windows has no
system IANA database, so without it `zoneinfo.ZoneInfo("Europe/Moscow")` fails.

### No-network environments

If `tzdata` cannot be installed (offline / restricted machine), `zoneinfo` can read any
compiled TZif tree through the standard `PYTHONTZPATH` variable, for example the tree that
ships with Git for Windows:

```powershell
$env:PYTHONTZPATH = "C:\Program Files\Git\mingw64\share\zoneinfo"
```

`python tools/doctor.py` detects and reports such a tree but never activates it silently.
The test bootstrap does use it as an explicit fallback and prints a warning when it does.

## Quickstart

```bash
python tools/doctor.py                  # dev mode: reports environment, WARN on missing tzdata
python tools/doctor.py --mode strict    # release/CI mode: missing tzdata is a FAIL (exit 1)

python -m unittest discover -s tests -t . -v

python -m demo.report                   # the demo scenario: complete-route recommendation
python -m demo.storage_roundtrip        # save/reload/run/read-back: the storage round trip
python tools/benchmark_optimizer.py --stop-count 100   # the ~100-stop exhaustive loop (minutes)
```

Tests are deterministic and require no network, no browser and no external service.

`python -m demo.report` prints, deterministically and labelled DEMO/SYNTHETIC: the plan and status,
the recommended first stop with its complete-route metrics, the top-5 ranking, the recommended
candidate's complete route stop by stop, the nearest and the farthest candidate with their complete
outcomes and their rank, USER vs OPTIMIZED vs the internal ALGORITHM baseline, the departure-time
sweep (04:00-08:00, where the recommendation changes), the objective-alignment audit of the D35
change (per departure hour, the previous provisional recommendation against the new elapsed-duration
one, with FINISH, complete travel, waiting, service and feasibility), the **non-default** study of
what a non-zero waiting preference would do, the rejected-candidate diagnostics grouped by candidate
with their violating stop ids, both fingerprints, the work counters, the measured ~30-stop evaluation
runtime and the **scale/performance block** — the ~30-stop demo plan, the ~50-enabled-stop portfolio
fixture (the primary MVP scale target, with its own live measurement) and the ~100-stop stress
reference, which is relabelled as future scale / **not performance-qualified** while keeping its
honest recorded number and the owner-accepted bound. The demo plan's BEFORE route is itself
infeasible (it serves the early-closing customer too late), which is what the optimizer's AFTER route
fixes.

`python -m demo.report` needs a time zone database. On an offline machine where `tzdata` cannot be
installed, either set `PYTHONTZPATH` (see above) or use the explicit development flag, which prints
a warning:

```bash
python -m demo.report --allow-system-tzdata
```

## Run the demo workspace (Stage 4)

`web/` is a static HTML/CSS/JS workspace served by the same local Python process: no npm, no bundler,
no build step and no frontend framework. One command starts everything:

```bash
python -m api.serve
```

It prints the URL it bound and where it is serving from, for example:

```
RoutePilot API serving on http://127.0.0.1:8000
  database: var\routepilot.db (schema version 1)
  static root: <repo>\web
  stop with Ctrl+C
```

- it binds **loopback only** by default (`--host`, default `127.0.0.1` - there is no authentication);
- `--port` sets the TCP port (default `8000`; `--port 0` lets the operating system choose and prints
  the chosen port);
- `--db` sets the SQLite database (default `var/routepilot.db`, a **gitignored** path whose directory
  is created on first start, so no database artifact can be committed); `--static-root` and `--quiet`
  also exist;
- **open the printed URL** - `http://127.0.0.1:8000` by default. The server root *is* the workspace:
  `GET /` serves `index.html`.

Engine endpoints need a time zone database exactly as the rest of the project does (see
Requirements): without `tzdata`, or `PYTHONTZPATH` on an offline machine, `GET /api/health` still
answers but the recommendation and route endpoints return `503 timezone_data_unavailable`.

### Portfolio walkthrough

1. **Start it**: `python -m api.serve`, then open the printed URL in a browser.
2. **Open or create the DEMO/SYNTHETIC plan**: choose it in "Open plan", or press
   "Create / open the DEMO plan" (the same deterministic fixture, never duplicated). The first-stop
   state must read `awaiting_first_stop_choice` with no selected stop and no provenance.
3. **Request a recommendation**: press "Get recommendation". A computing state appears while the
   request is in flight and the measured `computation_seconds` is shown when it returns.
4. **Understand that it is only a recommendation**: the advisory banner and the payload say it is
   *not an applied decision* and *not plan state*, and nothing was applied to the plan.
5. **Inspect the alternatives and the rejected candidates**: the ranked candidates with their
   complete-route metrics, and the rejected candidates with the stops whose hard window their
   complete route misses.
6. **Accept it or choose another stop**: press "Accept the recommendation", or pick another enabled
   stop in "First stop (manual)" and press "Use this stop".
7. **See the selection pinned with provenance**: state `first_stop_selected`, the selected stop,
   provenance `accepted_recommendation` or `manual_choice`, and `pinned: true` - all re-read from the
   server.
8. **See the full ordered route**: the route panel and the timeline show the engine's own order with
   the per-stop ETA, waiting, service start and duration, departure, local service window and
   lateness.
9. **See ETA / waiting / service / FINISH**: those timeline columns plus the summary's FINISH arrival.
10. **Compare BEFORE vs AFTER**: the driver's own input order against the RoutePilot order, with the
    saved duration and distance the API reports and the explicit violations; the route panel reports
    both engine fingerprints.
11. **Disable / restore a stop or change a priority and recalculate**: each stop row's own controls
    send `PUT /api/plans/{id}` with exactly one change, then "Recalculate" appends exactly one
    immutable run row and re-reads the route, the selection state and the run history from the server.
    There is deliberately no drag/reorder control (D21).
12. **Inspect the immutable run history**: the read-only run list plus "Show run detail"; nothing in
    the workspace edits, reorders or deletes a run, and a stored recommendation is never presented as
    the plan's current decision.
13. Finally read "What this build does NOT do", which is rendered from the API's own capability
    report instead of being restated in the page.

### Honesty list for this demo

- All shipped data is **DEMO/SYNTHETIC**. It is not real addresses, opening hours, routing or traffic,
  and it is labelled as such in the page (a static banner that is on screen before any script runs)
  and in the API's plan, route, run and health payloads.
- **`SMART_ROUTE` is the only implemented route mode.** The plan's own mode is displayed from the
  payload and there is no route-mode control.
- There is **no real routing, no geocoding and no traffic**: every travel time and distance comes from
  the deterministic synthetic matrix, and the straight line drawn between stops is synthetic
  geometry, **never road routing**.
- The exhaustive first-stop recommendation is computed synchronously, and its **~8 s worst case at
  the ~50-enabled-stop portfolio scale is an ACCEPTED MVP limitation** (D36/D37), reported honestly
  as `computation_seconds` instead of hidden or papered over with a prefilter. There is no background
  job queue; a request answers synchronously or is refused.
- The **map needs network in the browser**: Leaflet and the OSM-compatible tiles are fetched by the
  browser. When either is unreachable the workspace **degrades honestly** to a labelled notice plus
  the synthetic straight-line geometry, and the timeline, route, summary, recommendation and run
  history stay fully usable.
- Browser layout and tiles are **human-verified**, not machine-verified here: this environment has no
  working network, so no test loads Leaflet, fetches a tile or checks a rendered map.
  `tests/web/test_web_executed_dom.py` executes the **served** `web/app.js` in a strict DOM stub
  driven by payloads recorded from the real API, which is what covers rendering exceptions and
  rendered state offline.

### Layers and dependencies

- `core/` imports **only the Python standard library** - no HTTP, no UI, no storage, no network.
  Enforced by an automated check (`tests/test_core_isolation.py`, `tools/isolation_check.py`,
  `python tools/doctor.py`).
- `storage/`, `api/` and `demo/` depend on `core`; nothing in `core/` imports them.
- `web/` is **static** HTML/CSS/JS served as bytes: no Python module imports it, and it contains no
  business formula - every value it shows is rendered from an API payload unchanged.

## Layout

```
core/     domain model, time layer, engine (cost scoring, complete-route optimizer, exhaustive
          first-stop recommendation) — no HTTP, no UI, no storage, no network
demo/     deterministic demo dataset, synthetic travel matrix, scale fixtures (portfolio ~50 enabled
          stops and the ~100-stop stress reference), complete-route demo report
storage/  SQLite persistence on the approved schema: ordered migrations, the plan/stop repository,
          the immutable run-history repository and the settings repository (Stage 3)
api/      HTTP transport on the stdlib http.server, the framework-agnostic service layer, the JSON
          serialisation contracts, the map configuration and the `python -m api.serve` entry point
          (Stage 4) — no FastAPI, no new dependency, no business formula
web/      static demo workspace: index.html, styles.css, app.js, map.js — Leaflet + OSM-compatible
          tiles from configured settings, map, timeline panel, recommendation/alternatives, rejected
          candidates, BEFORE vs AFTER summary, override controls and read-only run history
          (Stage 4) — served as-is, never imported by Python
tools/    doctor, the exhaustive first-stop benchmark (demo / portfolio / stress) and other
          developer utilities
docs/     PRODUCT_SPEC_v2.md (current), PRODUCT_SPEC.md (historical v1), ARCHITECTURE.md,
          DECISIONS.md, STORAGE_SCHEMA.md
tests/    deterministic offline unittest suite
```

## Principles

1. **The specification is the Source of Truth.** `docs/PRODUCT_SPEC.md` is stored verbatim
   and is never rewritten as a summary.
2. **Decisions are documented, not remembered.** `docs/DECISIONS.md` holds the approved
   registry D1–D38 and is the only place a decision is considered settled.
3. **Start is not a service stop.** The departure location is where driving begins; it is
   never a customer task.
4. **No invented business hours.** An unknown service window stays explicitly unknown.
5. **No silent timezone arithmetic.** DST gaps and ambiguous wall-clock times are validation
   errors, never silently shifted.
6. **Hard infeasibility is never a big number.** A missed hard service window is an explicit
   violation, not a large penalty hidden inside a cost sum.
7. **Capability honesty.** Unimplemented features (side-of-road logic, route modes, real
   routing) are declared as unimplemented and are never presented as working.
8. **Synthetic data is labelled.** DEMO/SYNTHETIC travel data is never displayed as real
   road routing.
9. **Core stays pure.** `core/` must not import HTTP, UI, storage or network modules.

## Roadmap

| Stage | Scope |
|---|---|
| 0 ✅ | foundation: docs, domain skeleton, time layer, strict DST, error taxonomy, doctor, tests, storage schema proposal |
| 1 ✅ | cost scoring over implemented components, deterministic demo dataset (~30 stops), synthetic matrix, 04:00 / 08:00 scenario, candidate evaluation, numeric demo report |
| 1.5 ✅ | semantics migration off the revoked AUTO model: RECOMMEND/MANUAL, recommendation vs driver decision, `awaiting_first_stop_choice` (D4–D11, D32) |
| 2 ✅ | complete-route evaluation (FINISH leg included), deterministic optimizer with a measured leg cache, **exhaustive** complete-route first-stop recommendation with top-K and rejected-candidate diagnostics, recommendation and route fingerprints, the three baselines, the **complete elapsed-duration default objective with the owner's deterministic 5-key ranking** (D35), the **scale decision: ~50 enabled stops is the primary MVP target** with its portfolio fixture and the ~100-stop stress benchmark (D36), and the complete-route demo narrative (U1–U6, U6b) |
| 2.2 ✅ | the **exact incremental / delta complete-route evaluator** (U7): prefix reuse plus the FINISH leg, identical semantics, about 2.5x lower latency at the portfolio and stress scales and about 2.2x on the ~30-stop demo plan; the reference full pass stays the comparison baseline and the opt-in slow equivalence gate proves it move by move |
| 3 ✅ | SQLite storage behind the approved schema (D38, U9–U12): ordered idempotent migrations, plan/stop persistence with an exact round-trip, append-only immutable run history, the settings store, the pure `core/repositories.py` ports, and the `python -m demo.storage_roundtrip` end-to-end demo |
| 4 ✅ | API + web UI: stdlib `http.server` transport behind a framework-agnostic service layer with the documented JSON contracts and error mapping (U13), the recommendation/selection/route/optimize/run-history endpoints with the synchronous single-flight contract (U14), the static `web/` workspace with map, timeline, summary and honest degradation (U15) and the override controls with the read-only run history (U16); U17 is the documentation and acceptance sweep |
| 5 | reoptimization after each served stop + active-leg protection groundwork |
