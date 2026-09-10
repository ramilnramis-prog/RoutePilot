# RoutePilot — Product Specification v2
# Current Source of Truth

Version: 2
Status: CURRENT
Supersedes: Product Specification v1 for all future implementation decisions.

Product Specification v1 must remain unchanged as historical documentation.

==================================================
0. PURPOSE
==================================================

RoutePilot is a route planning and route optimization application for drivers,
couriers, delivery workers and dispatchers who may need to visit dozens or
100+ service locations during one working route.

The product solves the problem of manually entering many addresses into maps
and manually deciding the order in which they should be visited.

RoutePilot is NOT intended to replace a turn-by-turn navigation application.

Its primary job is to:

1. understand a set of service stops;
2. understand where and when the driver starts;
3. understand where the driver must finish;
4. understand service windows and stop properties;
5. calculate practical route alternatives;
6. recommend good first-service-stop choices;
7. let the DRIVER explicitly choose the first service stop;
8. optimize all remaining stops around that choice;
9. explain route metrics and constraints deterministically;
10. later hand individual route legs to an external navigator.

Core product principle:

ROUTEPILOT RECOMMENDS.
THE DRIVER DECIDES.

The final system should behave closer to an experienced delivery assistant
than to a simple shortest-distance waypoint optimizer.


==================================================
1. CORE REAL-WORLD SCENARIO
==================================================

Example:

A driver finishes loading a vehicle at 04:00.

The departure location may be a warehouse.

Many service locations only open at 08:00.

Driving 20 minutes to the geographically nearest customer and then waiting
3 hours 40 minutes may be a poor operational choice.

A farther customer may require approximately 4 hours of driving and allow
the driver to arrive close to its opening time.

However, RoutePilot must NOT automatically make that final choice.

The system calculates and recommends good candidates.

The driver chooses which service location will actually be first.

Therefore:

DEPARTURE LOCATION
is NOT the same concept as
FIRST SERVICE STOP.

And:

RECOMMENDED FIRST STOP
is NOT the same concept as
SELECTED FIRST STOP.


==================================================
2. START, FIRST SERVICE STOP AND FINISH
==================================================

The domain represents separately:

- departure_location
- departure_time
- recommended_first_stop
- selected_first_stop
- service stops
- finish_location

START / departure_location:

- is the physical place where driving begins;
- is not a service task;
- never appears as a customer stop in route order.

FIRST SERVICE STOP:

- is the first actual location where service is performed;
- must ultimately be chosen explicitly by the driver.

FINISH:

- may be a warehouse, depot, office, parking location, city center or another
  user-defined destination;
- is fixed;
- is never reordered like an ordinary service stop.

A complete working route is:

START
→ driver-selected first service stop
→ optimized remaining service stops
→ FINISH


==================================================
3. FIRST-STOP MODES
==================================================

RoutePilot supports two first-stop modes:

RECOMMEND
MANUAL


### RECOMMEND

RoutePilot:

1. evaluates candidate first service stops;
2. calculates complete-route outcomes for suitable candidates;
3. ranks them;
4. presents the recommended candidate;
5. presents alternative top-K candidates;
6. waits for explicit driver selection.

The recommendation is advisory.

RoutePilot MUST NOT silently commit the recommended candidate as the driver's
first stop.

Example valid state:

recommended_stop_id = S73
selected_stop_id = None

The UI may show a complete route preview for S73.

That preview is NOT yet the committed working route.


### MANUAL

The driver directly chooses a first service stop.

RoutePilot then optimizes every remaining enabled service stop around that
choice.

The system does not need to provide a recommendation before allowing MANUAL
selection.


==================================================
4. DRIVER SELECTION
==================================================

The committed first service stop always originates from an explicit driver
action.

Two selection sources exist:

accepted_recommendation
manual_choice

Example:

RoutePilot recommends S73.

Driver presses:

"Start from S73"

Result:

selected_stop_id = S73
selection_source = accepted_recommendation
pinned = true


Example:

RoutePilot recommends S73.

Driver chooses S51 instead.

Result:

selected_stop_id = S51
selection_source = manual_choice
pinned = true


MANUAL mode selection:

selected_stop_id = chosen stop
selection_source = manual_choice
pinned = true


==================================================
5. SELECTION AND PINNING INVARIANT
==================================================

A selected first stop is always considered committed and pinned.

Valid unresolved state:

selected_stop_id = None
selection_source = None
pinned = false

Valid selected state:

selected_stop_id != None
selection_source != None
pinned = true

The state:

selected_stop_id != None
pinned = false

is INVALID in Product Specification v2.

Once the driver chooses a first service stop, RoutePilot must never silently
replace it during ordinary reoptimization.


==================================================
6. UNPIN / CANCEL FIRST-STOP SELECTION
==================================================

Unpin means:

cancel the committed first-stop selection.

Therefore:

selected_stop_id → None
selection_source → None
pinned → false

In RECOMMEND mode:

- the current recommendation may remain available or be recomputed;
- recommended_stop_id remains separate from selected_stop_id;
- the application returns to awaiting_first_stop_choice.

In MANUAL mode:

- no first stop is selected;
- the application returns to awaiting_first_stop_choice;
- the domain must not choose one automatically.

The driver must explicitly choose again.


==================================================
7. RECOMMENDATION RECOMPUTATION
==================================================

Recommendations are dynamic.

An existing recommendation becomes stale when meaningful recommendation
inputs change.

Examples:

- departure_time
- departure_location
- active stop set
- service windows
- service durations
- priorities
- finish location
- travel matrix identity/version
- route mode
- supported cost policy
- timezone / relevant timezone-data version
- future traffic data

Use an inputs_fingerprint or equivalent deterministic mechanism.

Driver selection state itself must NOT invalidate the recommendation
fingerprint merely because the driver accepted or rejected a recommendation.

The committed route has its own route-dependent fingerprint because it
depends on the selected first stop.


==================================================
8. SERVICE WINDOWS
==================================================

A service stop supports an explicit ServiceWindow.

window_kind:

fixed
unrestricted
unknown

For fixed:

- start_local is required;
- end_local is required.

For unrestricted or unknown:

- both are None.

RoutePilot must never invent customer opening hours.

Every service stop may support:

- service_window_start
- service_window_end
- service_duration
- window_end_policy


==================================================
9. WINDOW END POLICY
==================================================

The meaning of a closing time is explicit.

Supported policies:

service_start_before_end
service_finish_before_end

RoutePlan defines the default.

Default:

service_finish_before_end

A fixed ServiceWindow may override the plan default.

Example:

window:
10:00–10:15

service duration:
20 minutes

service begins:
10:05

Under:

service_finish_before_end

the stop is infeasible because service completes at 10:25.

Under:

service_start_before_end

the stop may begin legally at 10:05 and finish after the nominal closing
time.

Provider/import layers may populate this stop-level override later.

There is no provider-specific window policy in the core domain.


==================================================
10. TIMELINE
==================================================

For each stop, RoutePilot should be able to calculate:

- departure time from previous location
- travel time
- estimated arrival
- opening time
- waiting time
- service start
- service duration
- estimated service finish / departure
- lateness
- finish overtime where relevant
- service-window feasibility

Example:

Departure: 04:00
Travel: 3h55m
Arrival: 07:55
Opens: 08:00
Waiting: 5m
Service start: 08:00


==================================================
11. HARD SERVICE-WINDOW FEASIBILITY
==================================================

A fixed service window is HARD unless a future product feature explicitly
introduces a different window type.

Hard infeasibility is a first-class state.

It MUST NOT be hidden inside a large numeric optimization penalty.

If a route cannot satisfy a hard service window, RoutePilot records an
explicit Violation with diagnostics.

Ordinary optimization weights only compare feasible alternatives.

A future soft/preferred-window concept may use lateness penalties, but that is
a separate feature and is not equivalent to a hard service window.


==================================================
12. FIRST-STOP RECOMMENDATION
==================================================

RoutePilot must evaluate more than the first driving leg.

A first-stop candidate must eventually be assessed using the COMPLETE
candidate route:

START
→ candidate first stop
→ optimized remaining enabled stops
→ FINISH

Candidate metrics should include:

- first-leg travel
- first-stop ETA
- first-stop waiting time
- first-stop service start
- complete travel time
- complete waiting time
- total service time
- complete route duration
- estimated final arrival at FINISH
- hard-window violations
- objective breakdown

A first stop must not be considered best only because its first leg looks
good.


==================================================
13. TOP-K RECOMMENDATIONS
==================================================

RECOMMEND mode should present approximately 3–5 strong candidates.

Example:

1. Stop #73 — Recommended
   ETA: 07:55
   Opens: 08:00
   Waiting: 5m
   Complete route: 6h21m

2. Stop #51
   Complete route: 6h28m

3. Stop #88
   Complete route: 6h31m

The driver chooses one.

The explanation must come from deterministic route metrics and cost breakdown,
not invented LLM reasoning.


==================================================
14. INFEASIBLE CANDIDATES
==================================================

A first-stop candidate whose COMPLETE route contains any hard service-window
infeasibility is not part of the valid recommendation ranking.

It is:

- excluded from feasible top-K;
- retained in diagnostics/rejected candidates;
- accompanied by explicit violating stops and reasons.

If no candidate produces a completely feasible route, RoutePilot returns:

no_fully_feasible_route

It MUST NOT label an infeasible candidate as a valid recommended route.

The driver may then inspect violations, modify constraints/stops or make a
manual operational decision.


==================================================
15. COMPLETE ROUTE DEFINITION
==================================================

For all user-facing RoutePilot metrics, a complete route means:

START
→ all enabled service stops exactly once
→ FINISH

The final leg to FINISH is INCLUDED.

Therefore:

complete route duration
estimated finish time
complete travel distance
complete travel time

all include the final leg from the last service stop to FINISH.


==================================================
16. ROUTE COST POLICY
==================================================

RouteCostPolicy must remain configurable.

The architecture supports components such as:

- travel_time
- distance
- waiting_time
- early_arrival preference
- future soft lateness
- U-turn cost
- wrong-side approach cost
- backtracking cost
- priority cost
- finish-direction cost
- first-stop remaining-route cost

Only actually supported components may participate in scoring.

Capabilities requiring real road/provider data must not be faked.

The Stage 1 policy:

travel_time = 1
waiting_time = 2

is DEMO / PROVISIONAL ONLY.

It is not RoutePilot product truth.

In a real elapsed-time model:

travel
+
waiting
+
service

naturally contribute to route completion time.

A future preference to reduce idle waiting may exist as a configurable soft
preference rather than an arbitrary universal multiplier.


==================================================
17. DRIVER LOGIC
==================================================

A long-term differentiator of RoutePilot is DRIVER LOGIC.

Experienced drivers account for operational details that generic waypoint
optimization may miss.

Future RoutePilot should be capable of considering:

- direction of travel
- side of road
- U-turn cost
- unnecessary backtracking
- one-way streets
- inconvenient approaches
- practical turnaround behavior
- parking/access characteristics when data exists

Example:

With right-hand traffic, a driver may service points convenient on one side
while moving in one direction, reach a practical turnaround location, and
service opposite-side points on the return path.

CRITICAL:

Do not infer real side-of-road behavior from latitude/longitude alone.

Road geometry and provider/routing information are required.


==================================================
18. ACTIVE LEG PROTECTION — FUTURE
==================================================

When live navigation/reoptimization exists, RoutePilot must not silently
change the route leg the driver is already executing.

Example:

Driver is travelling:

Stop 17 → Stop 18

New traffic information arrives.

RoutePilot may recompute the future route after the committed leg but must not
unexpectedly redirect the driver before reaching the already committed next
stop.

Record this requirement now.

Do not implement active navigation prematurely.


==================================================
19. SCALE
==================================================

RoutePilot must be architected for:

- dozens of stops
- approximately 100 stops
- eventually more than 100 stops

Do not assume a third-party map provider will optimize all stops in one API
call.

Conceptual architecture:

addresses
→ coordinates
→ travel-time/distance matrix
→ RoutePilot optimization
→ ordered service stops
→ route geometry
→ navigator/map display

Travel matrix and route geometry may need chunked provider requests.


==================================================
20. FIRST-STOP CANDIDATE PERFORMANCE
==================================================

Correctness comes before approximate prefiltering.

For Stage 2, the reference implementation should evaluate the complete route
for every feasible first-stop candidate.

Do NOT introduce a fixed K=5 prefilter merely to save computation before
benchmarking the exhaustive approach.

A shortlist/approximation may later be introduced only as an explicit
performance decision.

If approximation is ever used, the product must not falsely claim that an
unexamined candidate could not have been better.

Performance target after the travel matrix already exists:

approximately 100 service stops:

preferred:
<= approximately 3 seconds

acceptable for the early product:
<= approximately 5 seconds

on a typical modern desktop/laptop.

These are engineering targets, not correctness rules.

If exhaustive evaluation exceeds the budget:

1. measure the bottleneck;
2. improve caching/reuse/algorithm implementation;
3. benchmark again;
4. only then propose candidate prefiltering or approximation.


==================================================
21. OPTIMIZER
==================================================

Optimization is isolated from:

- UI
- HTTP
- storage
- map vendors

The MVP may use:

constraint-aware greedy seed
→ deterministic local improvement such as 2-opt or equivalent

Requirements:

- START fixed
- FINISH fixed
- driver-selected first stop fixed
- each enabled service stop exactly once
- disabled stops excluded
- hard-window feasibility explicit
- no accepted local-search move may worsen the accepted objective
- deterministic tie-breaking

This MVP algorithm is not the final production VRP solver.

Architecture must allow later replacement or augmentation with:

- richer local search
- OR-Tools
- VRP/TSP variants
- time-window solvers
- multi-vehicle routing

without replacing the core product model.


==================================================
22. PROVIDER ABSTRACTIONS
==================================================

External vendors must be isolated behind interfaces.

At minimum:

GeocodingProvider
RoutingProvider
TravelMatrixProvider
Map/Tile configuration

Domain logic must not depend directly on:

Google
Yandex
OpenStreetMap
or another specific provider.


==================================================
23. CAPABILITY HONESTY
==================================================

Every route feature/cost component/provider capability must clearly represent
whether it is:

implemented
planned
requires_provider
unsupported

Do not present planned functionality as working.

Do not fake:

- traffic
- side of road
- U-turn behavior
- one-way-road behavior
- real road distance
- real route geometry

when the current data is synthetic.


==================================================
24. ADDRESS INPUT
==================================================

The driver should eventually be able to paste a large list of addresses.

Future intake methods may include:

- text
- CSV
- Excel
- PDF
- photographed documents / OCR

Pipeline:

parse
→ geocode
→ identify ambiguous/failed addresses
→ require user correction
→ optimize only sufficiently resolved stops

RoutePilot must not silently guess an ambiguous address.


==================================================
25. ROUTE STOP MODEL
==================================================

A RouteStop should support at least:

- id
- raw_address
- normalized_address
- latitude
- longitude
- geocode_status
- service_status
- enabled
- service_duration
- service_window
- priority
- input_position
- notes

geocode_status and service_status are separate concepts.

geocode_status:

pending
resolved
ambiguous
failed

service_status:

pending
in_progress
served
failed
skipped

enabled is independent.

A disabled stop is excluded from optimization.


==================================================
26. MANUAL CONTROL
==================================================

The driver remains in control.

The UI should eventually allow:

- disable stop
- restore stop
- change priority
- drag/reorder stop
- choose first stop
- accept recommended first stop
- cancel/unpin first-stop selection
- recalculate route

The domain may contain a general order-constraint abstraction from the
beginning.

The MVP does not need full arbitrary-order constraint solving.

Detailed interaction history should later live in an audit/event model rather
than duplicating first-stop state fields.


==================================================
27. ROUTE MODES
==================================================

Architecture may support future route modes:

FASTEST
SHORTEST
MINIMUM_TURNS
ON_THE_WAY
START_TO_FINISH
SMART_ROUTE

Only genuinely implemented modes may be exposed as functioning.

Do not show planned modes as real features.


==================================================
28. TIME MODEL
==================================================

Absolute timestamps are stored in UTC.

Every RoutePlan has an explicit IANA timezone.

Example:

Europe/Moscow

Use:

datetime
zoneinfo
tzdata

Do not create custom timezone transition tables.

Fixed UTC offsets are not the long-term timezone architecture.


==================================================
29. DST POLICY
==================================================

Default DST policy is STRICT VALIDATION.

If a local wall-clock time is:

- nonexistent because of a DST gap;
- ambiguous because it occurs twice;

RoutePilot returns an explicit validation/disambiguation error.

Do NOT:

- silently shift the time;
- silently select fold=0;
- silently select fold=1.

Customer service-window input must never be silently changed.


==================================================
30. BASELINES AND IMPROVEMENT METRICS
==================================================

Keep three concepts separate.

USER BASELINE:

START
→ stops in exact original input_position order
→ FINISH

This is the user-facing BEFORE route.

OPTIMIZED:

RoutePilot's optimized route around the driver's selected first stop.

This is the user-facing AFTER route.

ALGORITHM BASELINE:

internal initial greedy route before local improvement.

Used for algorithm-quality tests and diagnostics.

Do not display ALGORITHM BASELINE as the user's BEFORE route.


==================================================
31. TECHNOLOGY STACK
==================================================

Backend/domain/optimizer/calculations:

Python

Initial approach:

stdlib-first where practical

Future HTTP/API layer:

FastAPI may replace the initial transport without rewriting core/domain.

Frontend:

HTML
CSS
JavaScript

Core must not import:

- API layer
- HTTP framework
- UI
- storage implementation


==================================================
32. LLM / AI POLICY
==================================================

An LLM is not required to calculate RoutePilot routes.

Route decisions must remain deterministic and algorithmic.

DeepSeek Harness is currently a DEVELOPMENT tool, not a runtime RoutePilot
dependency.

Possible future AI features:

- unstructured address extraction
- OCR cleanup
- natural-language import
- explanation assistance

The route optimizer must remain fully testable without an LLM.


==================================================
33. DEMO MODE
==================================================

The MVP may use a deterministic synthetic dataset.

Approximately 30 realistic service stops.

Include:

- departure location
- finish location
- departure time
- service windows
- service durations
- priorities
- one disabled stop
- deterministic synthetic travel matrix

Important demonstration scenario:

departure around 04:00
many customers open around 08:00

The demo should prove:

- nearest is not necessarily the strongest recommendation;
- farthest is not necessarily the strongest recommendation;
- recommendation can change when departure time changes;
- complete-route quality matters more than first-leg-only scoring.

Synthetic travel information must always be labelled:

DEMO
SYNTHETIC

Never pretend synthetic metrics are real road/traffic data.


==================================================
34. DEMO MAP / UI
==================================================

Future demo UI should use an interactive map.

Preferred initial direction:

Leaflet
+
OpenStreetMap-compatible tile provider

Requirements:

- visible attribution;
- tile provider isolated in configuration;
- no core dependency on map technology;
- offline deterministic core tests;
- synthetic route geometry never presented as true road routing.

Primary UI:

map workspace
+
route/timeline panel
+
summary

Show:

- active stops
- departure time
- START
- recommendation
- alternatives
- selected first stop
- FINISH
- route order
- ETA
- waiting
- service windows
- estimated completion
- BEFORE/AFTER
- saved time/distance when meaningful


==================================================
35. STORAGE
==================================================

Initial persistent storage:

SQLite

Entities:

- route_plans
- route_stops
- route_optimization_runs
- app_settings

START and FINISH are stored as plan locations, not fake stop rows.

route_stops keeps input_position for USER BASELINE.

Optimization runs are immutable history and may store:

- inputs fingerprint
- route fingerprint
- timezone/tzdata metadata
- output order
- violations
- route metrics
- user baseline
- algorithm baseline

Derived heavy data such as travel matrices, complete timelines and geometry
need not be permanently persisted in the initial version unless a later
requirement justifies it.

General order overrides may initially be persisted as versioned structured
JSON.

Do not prematurely normalize arbitrary order constraints before their final
domain vocabulary is known.


==================================================
36. TESTABILITY
==================================================

Core must be testable without:

- browser
- network
- paid map API
- external LLM

Tests must be deterministic.

The suite should cover at least:

- START not treated as service stop
- FINISH fixed
- each enabled stop exactly once
- disabled stops excluded
- recommendation does not imply selection
- recommended_stop_id may exist while selected_stop_id is None
- selection always comes from explicit driver action
- selected first stop is pinned
- selected + pinned=False is rejected
- unpin clears committed selection
- accepted recommendation provenance
- manual-choice provenance
- driver-selected first stop survives reoptimization
- recommendation fingerprint independent from acceptance action
- recommendation becomes stale when true recommendation inputs change
- top-K uses complete-route metrics
- complete route includes FINISH leg
- hard infeasible candidates excluded from valid ranking
- no_fully_feasible_route behavior
- no duplicate/lost stops
- deterministic ranking/tie-breaking
- waiting calculations
- service start rules
- service_finish_before_end
- service_start_before_end override
- strict DST gap handling
- strict DST ambiguous handling
- user baseline preserves input_position
- local improvement never worsens accepted algorithm baseline
- deterministic demo output
- SQLite round-trip once storage is implemented


==================================================
37. REPOSITORY AND DOCUMENTATION
==================================================

Use Git from the beginning.

Never commit:

- API keys
- secrets
- local production databases
- logs
- caches
- temporary artifacts

Documentation:

docs/PRODUCT_SPEC.md
    historical Product Specification v1, unchanged

docs/PRODUCT_SPEC_v2.md
    THIS document, current Source of Truth

docs/DECISIONS.md
    approved implementation/product decisions

docs/ARCHITECTURE.md
    technical implementation architecture

docs/STORAGE_SCHEMA.md
    proposed/approved persistence design

If documentation conflicts:

the CURRENT product specification plus explicitly approved later decisions
control future implementation.

Historical documents remain unchanged for traceability.


==================================================
38. CURRENT PRODUCT PRINCIPLE
==================================================

Do not reduce RoutePilot to:

coordinates
+
nearest-neighbor
+
shortest distance.

The intended differentiation is:

"A route that is practical for the driver."

But practical does not mean the software takes control away from the driver.

RoutePilot performs the difficult computation.

The driver makes the operational decision about where to begin.


==================================================
39. CURRENT DEVELOPMENT STATE
==================================================

Stage 0:
complete.

Stage 1:
complete.

Stage 1.5:
semantic migration from revoked AUTO behavior to RECOMMEND / MANUAL complete.

Stage 2:
not started at the time this specification becomes current.

Stage 2 should implement:

- complete route optimizer
- complete-route first-stop candidate evaluation
- top-K recommendations
- driver-choice workflow semantics already defined by the domain
- recommendation and route fingerprints
- caching
- baselines
- deterministic performance measurement

It must not introduce:

- automatic commitment of recommendations
- real map/routing APIs
- SQLite implementation
- web UI
- traffic
- side-of-road/U-turn scoring
- LLM runtime dependency

without a later approved stage.
