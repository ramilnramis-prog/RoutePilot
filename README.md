# RoutePilot

RoutePilot is a route planning and route optimization application for drivers, couriers,
delivery workers and dispatchers who may need to visit dozens to 100+ service locations
during one working route.

RoutePilot is **not** a turn-by-turn navigator. Its job is to understand a set of service
stops, where and when the driver starts, where the driver must finish, what constraints
each stop carries, and to produce a practical visiting order with a deterministic
explanation of why that order was chosen — while leaving the driver in control.

The product specification is the Source of Truth: [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md).

## Status: Stage 1 (foundation + demo scenario)

Implemented so far:

- domain skeleton (value objects, `RouteStop`, `RoutePlan`, `ServiceWindow` with an explicit
  `window_kind` and `window_end_policy`, first-stop intent/resolution, cost policy, route modes,
  order overrides);
- time layer: UTC storage, explicit IANA time zone per plan, **strict DST validation**;
- timeline arithmetic: ETA, waiting time, service start, estimated departure, lateness and the
  `service_finish_before_end` / `service_start_before_end` window-end policies;
- **cost scoring** over the implemented components only (`travel_time`, `waiting_time`, `distance`),
  with a capability gate that refuses weights for anything unimplemented;
- **first-stop recommendation**: every possible first stop timed and priced, feasible ones ranked
  deterministically, infeasible ones reported explicitly. The engine **recommends**; the **driver
  decides** — nothing is applied automatically (D4/D32);
- **deterministic demo scenario**: ~30 synthetic stops, departure 04:00, customers opening 08:00,
  and a numeric report (`python -m demo.report`);
- error taxonomy split from violations, `tools/doctor.py`, and a deterministic offline test suite;
- [`docs/STORAGE_SCHEMA.md`](docs/STORAGE_SCHEMA.md) — **proposal only**, no storage code.

Not implemented yet (by design): optimizer / route selection, pinning and AUTO recomputation, web
UI, SQLite persistence, map and routing providers, traffic, side-of-road logic.

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

python -m demo.report                   # the demo scenario with candidate costs
```

Tests are deterministic and require no network, no browser and no external service.

`python -m demo.report` needs a time zone database. On an offline machine where `tzdata` cannot be
installed, either set `PYTHONTZPATH` (see above) or use the explicit development flag, which prints
a warning:

```bash
python -m demo.report --allow-system-tzdata
```

## Layout

```
core/     domain model, time layer, engine (cost scoring, first-stop evaluation) — no HTTP, no UI,
          no storage, no network
demo/     deterministic demo dataset, synthetic travel matrix, numeric report
storage/  SQLite persistence (later, proposal only)
api/      transport layer: stdlib http.server now, FastAPI later (later)
web/      HTML/CSS/JS frontend with Leaflet + OSM tiles (later)
tools/    doctor and other developer utilities
docs/     PRODUCT_SPEC.md, ARCHITECTURE.md, DECISIONS.md, STORAGE_SCHEMA.md
tests/    deterministic offline unittest suite
```

## Principles

1. **The specification is the Source of Truth.** `docs/PRODUCT_SPEC.md` is stored verbatim
   and is never rewritten as a summary.
2. **Decisions are documented, not remembered.** `docs/DECISIONS.md` holds the approved
   registry D1–D28 and is the only place a decision is considered settled.
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
| 2 | first-stop selection (AUTO/MANUAL provenance, pinning, `inputs_fingerprint` recomputation) + optimizer + remaining-route term + top-K explanation |
| 3 | SQLite storage + schema implementation + round-trip tests |
| 4 | API + web UI (map, timeline panel, route summary, top-K, override) |
| 5 | reoptimization after each served stop + active-leg protection groundwork |
