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

## Status: Stage 2 (complete-route optimizer + first-stop recommendation)

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
  candidate, ranked by the configured objective, rejected candidates are reported with their
  violating stops, and the driver still decides — nothing is applied automatically (D4/D32);
- **deterministic demo scenario**: ~30 synthetic stops (31 enabled + 1 disabled), departure 04:00,
  many customers opening 08:00, one early-closing customer that makes some first-stop choices
  genuinely infeasible, and a complete-route report (`python -m demo.report`) that shows the
  ranking, the recommended candidate's complete route, the rejected candidates with their violating
  stops, the baselines, the departure-time sweep, the provisional weights' sensitivity and both
  fingerprints;
- **measured performance**: `tools/benchmark_optimizer.py` runs the exhaustive first-stop loop over
  a ~100-stop synthetic fixture and reports the numbers (the ~100-stop latency is an accepted
  interim limitation, D34; the incremental/delta evaluator that would remove it is deferred);
- error taxonomy split from violations, `tools/doctor.py`, and a deterministic offline test suite;
- [`docs/STORAGE_SCHEMA.md`](docs/STORAGE_SCHEMA.md) — **proposal only**, no storage code.

Not implemented yet (by design): SQLite persistence, web UI, map and routing providers, traffic,
side-of-road logic, active-leg protection, and the deferred incremental/delta evaluator.

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
python tools/benchmark_optimizer.py --stop-count 100   # the ~100-stop exhaustive loop (minutes)
```

Tests are deterministic and require no network, no browser and no external service.

`python -m demo.report` prints, deterministically and labelled DEMO/SYNTHETIC: the plan and status,
the recommended first stop with its complete-route metrics, the top-5 ranking, the recommended
candidate's complete route stop by stop, the nearest and the farthest candidate with their complete
outcomes and their rank, USER vs OPTIMIZED vs the internal ALGORITHM baseline, the departure-time
sweep (04:00-08:00, where the recommendation changes), the sensitivity of the provisional waiting
weight, the rejected-candidate diagnostics grouped by candidate with their violating stop ids, both
fingerprints, the work counters, the measured ~30-stop evaluation runtime and the recorded ~100-stop
benchmark with its command. The demo plan's BEFORE route is itself infeasible (it serves the
early-closing customer too late), which is what the optimizer's AFTER route fixes.

`python -m demo.report` needs a time zone database. On an offline machine where `tzdata` cannot be
installed, either set `PYTHONTZPATH` (see above) or use the explicit development flag, which prints
a warning:

```bash
python -m demo.report --allow-system-tzdata
```

## Layout

```
core/     domain model, time layer, engine (cost scoring, complete-route optimizer, exhaustive
          first-stop recommendation) — no HTTP, no UI, no storage, no network
demo/     deterministic demo dataset, synthetic travel matrix, complete-route demo report
storage/  SQLite persistence (later, proposal only)
api/      transport layer: stdlib http.server now, FastAPI later (later)
web/      HTML/CSS/JS frontend with Leaflet + OSM tiles (later)
tools/    doctor, the ~100-stop benchmark and other developer utilities
docs/     PRODUCT_SPEC_v2.md (current), PRODUCT_SPEC.md (historical v1), ARCHITECTURE.md,
          DECISIONS.md, STORAGE_SCHEMA.md
tests/    deterministic offline unittest suite
```

## Principles

1. **The specification is the Source of Truth.** `docs/PRODUCT_SPEC.md` is stored verbatim
   and is never rewritten as a summary.
2. **Decisions are documented, not remembered.** `docs/DECISIONS.md` holds the approved
   registry D1–D34 and is the only place a decision is considered settled.
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
| 2 ✅ | complete-route evaluation (FINISH leg included), deterministic optimizer with a measured leg cache, **exhaustive** complete-route first-stop recommendation with top-K and rejected-candidate diagnostics, recommendation and route fingerprints, the three baselines, the ~100-stop benchmark, and the complete-route demo narrative |
| 2 (deferred) | incremental / delta complete-route evaluator to remove the ~100-stop latency accepted in D34 — recorded, not implemented; no prefilter or approximation meanwhile |
| 3 | SQLite storage + schema implementation + round-trip tests |
| 4 | API + web UI (map, timeline panel, route summary, top-K, override) |
| 5 | reoptimization after each served stop + active-leg protection groundwork |
