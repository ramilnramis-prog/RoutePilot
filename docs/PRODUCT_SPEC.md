# RoutePilot — Base Product Specification
# Source of Truth v1

## 0. Purpose

RoutePilot is a route planning and route optimization application for drivers,
couriers, delivery workers and dispatchers who may need to visit dozens or
approximately 100+ different service locations during one working route.

The product solves the problem of manually entering many addresses into maps
and manually deciding the order in which they should be visited.

RoutePilot is NOT intended to replace a turn-by-turn navigation application.

Its primary job is:

1. understand a set of service stops;
2. understand where and when the driver starts;
3. understand where the driver should finish;
4. understand time constraints and stop properties;
5. determine a practical order in which the stops should be visited;
6. explain why that order was selected;
7. allow the driver to override important decisions;
8. later hand off individual route legs to an external navigator.

The final system should behave closer to an experienced delivery driver than
to a simple "shortest line between points" optimizer.


==================================================
1. CORE REAL-WORLD SCENARIO
==================================================

Example:

A driver loads the vehicle at 04:00.

The departure location may be a warehouse in or near a city.

Many customer locations only open at 08:00.

It may be irrational to drive 30 minutes to the closest customer,
arrive at 04:30 and wait for 3 hours 30 minutes.

A farther customer may require approximately 3 hours 50 minutes of driving,
allowing the driver to arrive close to 08:00 and begin service immediately.

Therefore:

DEPARTURE LOCATION
is NOT the same concept as
FIRST SERVICE STOP.

The optimizer must never implement a rule such as:

"always visit the nearest stop first"

or:

"always visit the farthest stop first".

The first service stop must be selected by considering the complete route
context.


==================================================
2. START, FIRST STOP AND FINISH
==================================================

The domain must represent separately:

- departure_location
- departure_time
- first_service_stop
- service stops
- finish_location

START / departure_location:
- is the physical place where driving begins;
- is never treated as a service stop;
- must never appear as a customer/service task.

FIRST SERVICE STOP:
- is the first actual stop where service is performed.

FINISH:
- may be a warehouse, office, depot, city center, parking location or another
  user-defined destination;
- is fixed and must not be reordered as a normal stop.

A common use case may be:

warehouse at 04:00
→ first customer around 08:00
→ all remaining stops
→ finish in central Moscow.


==================================================
3. FIRST STOP MODES
==================================================

RoutePilot supports two first-stop modes:

AUTO
MANUAL


### AUTO

The system:

1. evaluates possible first service stops;
2. automatically selects the best candidate;
3. immediately applies that candidate;
4. builds the complete route without requiring confirmation.

The driver must still be able to:

- see why the stop was selected;
- inspect alternative candidates;
- manually select another stop;
- lock the current recommendation.

An automatically selected stop is dynamic by default:

selection_source = auto_recommendation
pinned = false

If relevant inputs change, AUTO may choose a different first stop.


### MANUAL

The driver explicitly chooses the first service stop.

That selection becomes:

selection_source = driver
pinned = true

The optimizer must preserve that first stop and optimize all remaining stops
around it.

The system must never silently replace a manually pinned first stop.


==================================================
4. FIRST STOP PROVENANCE AND PINNING
==================================================

Selection provenance and pinning are separate concepts.

Examples:

AUTO selected:
selection_source = auto_recommendation
pinned = false
pinned_via = None

AUTO selected and driver presses Lock:
selection_source = auto_recommendation
pinned = true
pinned_via = lock

Driver overrides AUTO:
selection_source = driver
pinned = true
pinned_via = override

MANUAL selection:
selection_source = driver
pinned = true
pinned_via = manual_mode

Locking an automatically recommended stop MUST NOT rewrite its selection
provenance to "driver".


==================================================
5. AUTO RECOMPUTATION
==================================================

An unpinned AUTO recommendation must be recalculated when meaningful inputs
change.

Examples include:

- departure_time
- departure_location
- active stop set
- service windows
- priorities
- finish location
- route/travel matrix
- traffic data when traffic support exists later

Use an inputs_fingerprint or equivalent deterministic mechanism so that AUTO
does not accidentally retain a stale recommendation.

If the first stop is explicitly pinned, route reoptimization must preserve it
until the user explicitly unpins it.


==================================================
6. UNPIN BEHAVIOR
==================================================

AUTO:

unpin
→ recompute first-stop recommendation
→ selection_source = auto_recommendation
→ pinned = false
→ rebuild the route


MANUAL:

unpin
→ first stop becomes unresolved
→ do NOT automatically choose a stop
→ user must choose another first stop or explicitly switch to AUTO.


==================================================
7. SERVICE WINDOWS
==================================================

Every service stop must be able to support:

- service_window_start
- service_window_end
- service_duration

These values may initially be optional.

If business hours are unknown, the system must represent them as unknown or
unrestricted.

The application MUST NOT invent opening hours.

For every route stop the timeline should eventually be able to calculate:

- departure time from previous location
- travel time
- ETA
- opening time
- waiting time
- service start
- service duration
- estimated departure
- lateness
- time-window feasibility


Example:

Departure: 04:00
Travel: 3h55m
Arrival: 07:55
Opens: 08:00
Waiting: 5m
Service start: 08:00


==================================================
8. SMART FIRST STOP
==================================================

The first-stop selector must evaluate more than distance.

It should eventually consider at least:

- travel time from departure location
- distance
- ETA
- waiting time
- lateness
- time-window feasibility
- priority
- direction relative to the finish
- estimated cost of the remaining route

The important requirement is:

the quality of a first-stop candidate includes the route AFTER that candidate.

A candidate should not be considered good only because its initial leg looks
good.


==================================================
9. TOP-K FIRST STOP EXPLANATION
==================================================

In AUTO mode the application should be able to show a small ranked set of
alternatives, for example 3–5 candidates.

Example UI concept:

Recommended first stop

1. Stop #73 — selected
   travel 3h52m
   ETA 07:52
   opens 08:00
   waiting 8m
   estimated complete route 6h21m

2. Stop #51
   travel 3h31m
   ETA 07:31
   waiting 29m
   estimated complete route 6h34m

3. Stop #88
   travel 4h04m
   ETA 08:04
   late 4m
   estimated complete route 6h27m

The explanation should primarily come from deterministic cost breakdowns,
not invented LLM prose.


==================================================
10. ROUTE COST POLICY
==================================================

Create a RouteCostPolicy abstraction.

The architecture must eventually support components such as:

- travel_time
- distance
- waiting_time
- early_arrival_penalty
- late_arrival_penalty
- time_window_violation_penalty
- u_turn_penalty
- wrong_side_penalty
- backtracking_penalty
- priority_penalty
- finish_direction_penalty
- first_stop_remaining_route_weight

Weights must be configurable.

Do not hardcode arbitrary weights as product truth during the early stages.

Early arrival is allowed and normally creates waiting_time.

A stop cannot begin service before service_window_start.

A fundamentally impossible service window must be represented explicitly and
must not be hidden by ordinary optimization weights.


==================================================
11. DRIVER LOGIC
==================================================

A long-term differentiator of RoutePilot is DRIVER LOGIC.

Experienced drivers often optimize differently from generic waypoint
optimization.

Example:

On a road with right-hand traffic, if several stops can conveniently be
serviced while moving in one direction, a driver may prefer to service those
stops first, continue to a practical turnaround point, and later service
locations on the opposite side while returning.

The system should eventually account for:

- direction of travel
- side of road
- U-turn cost
- unnecessary backtracking
- one-way streets
- inconvenient approaches
- practical turnaround behavior

CRITICAL:

Do not infer the true side of road from latitude/longitude alone.

Real side-of-road logic requires road geometry, road direction and routing
provider information.

The MVP must not pretend to solve this using fake geometry.


==================================================
12. ACTIVE LEG PROTECTION — FUTURE REQUIREMENT
==================================================

When RoutePilot later supports active driving and dynamic reoptimization,
the system must not silently change the leg that the driver is already
executing.

Example:

Driver is already travelling:

Stop 17 → Stop 18

Traffic or other inputs change.

The application may recompute the future route AFTER the committed active
leg, but must not unexpectedly tell the driver to reverse direction before
reaching the committed next stop.

This requirement should be recorded now, but does not need full implementation
in the first MVP.


==================================================
13. SCALE
==================================================

The product must be designed for:

- dozens of stops
- approximately 100 stops
- eventually more than 100 stops

Do NOT architect the system under the assumption that a third-party routing
API can optimize all stops in one request.

The conceptual flow is:

addresses
→ coordinates
→ travel-time/distance matrix
→ RoutePilot optimizer
→ ordered stops
→ route geometry
→ external navigator / map display

Route geometry may later need to be requested in chunks.


==================================================
14. OPTIMIZATION ENGINE
==================================================

The optimization algorithm must be isolated from UI, storage and routing API
implementations.

Create an optimizer abstraction so that algorithms can evolve.

For an early MVP, a reasonable deterministic baseline is:

greedy / nearest-neighbor seed
→ local improvement such as 2-opt

But nearest-neighbor MUST NOT be treated as the final product algorithm.

The architecture should permit later replacement or augmentation with:

- richer local search
- VRP algorithms
- constraint solvers
- OR-Tools or equivalent
- time-window optimization
- multi-vehicle optimization

without rewriting the domain model.


==================================================
15. PROVIDER ABSTRACTIONS
==================================================

External map and routing vendors must be isolated behind interfaces.

At minimum plan for:

GeocodingProvider
RoutingProvider
TravelMatrixProvider
Map/Tile configuration

Business/domain logic must not depend directly on Google Maps, Yandex,
OpenStreetMap or another specific vendor.


==================================================
16. ADDRESS INPUT
==================================================

Initial product direction:

The driver should eventually be able to paste a large list of addresses.

Future import methods may include:

- pasted text
- CSV
- Excel
- PDF
- photographed document / OCR

The application should:

1. parse candidate addresses;
2. geocode them;
3. identify ambiguous or failed addresses;
4. require correction when necessary;
5. only then optimize the route.

Do not silently guess an ambiguous address.


==================================================
17. ROUTE STOP MODEL
==================================================

A route stop should be designed to eventually support at least:

- id
- raw_address
- normalized_address
- latitude
- longitude
- service_duration
- service_window_start
- service_window_end
- priority
- status
- enabled/disabled
- notes

Future versions may add customer-specific metadata.

Do not overload coordinates with routing-specific information.


==================================================
18. MANUAL CONTROL
==================================================

The driver must remain in control.

Future UI should allow:

- disable stop
- restore stop
- change priority
- drag/reorder stop
- select first stop
- lock first stop
- unpin first stop
- recalculate route

A manually pinned first stop must survive route optimization.


==================================================
19. ROUTE MODES
==================================================

The architecture should support future route modes such as:

FASTEST
SHORTEST
MINIMUM_TURNS
ON_THE_WAY
START_TO_FINISH
SMART_ROUTE

The initial implementation may only implement SMART_ROUTE or a deterministic
demo approximation.

Do not present unimplemented modes as working functionality.


==================================================
20. TIME MODEL
==================================================

All absolute timestamps must be stored in UTC.

Each RoutePlan has an explicit IANA timezone.

Example:

Europe/Moscow

Local input/output must be resolved through that timezone.

Use:

datetime
zoneinfo
tzdata

Do not build custom timezone or DST tables.

Fixed UTC offsets are not an acceptable long-term timezone model.


==================================================
21. DST POLICY
==================================================

The default policy is STRICT VALIDATION.

If a local service-window time is:

- nonexistent because of a DST gap, or
- ambiguous because the same wall-clock time occurs twice,

the domain must return an explicit validation/disambiguation error.

Do NOT:

- silently shift the time forward;
- silently choose fold=0;
- silently choose fold=1.

The domain must never silently change a customer's service window.

Configurable DST resolution policies may be added later.


==================================================
22. TECHNOLOGY STACK
==================================================

Backend, domain model, optimization and calculations:

Python

Initial preference:

stdlib-first

Future:

FastAPI may replace the transport/API layer WITHOUT rewriting core/domain.

Frontend:

HTML
CSS
JavaScript

Core must not import HTTP/UI-specific modules.


==================================================
23. LLM / AI POLICY
==================================================

An LLM is NOT required to calculate the route.

Core route decisions must be deterministic and algorithmic.

DeepSeek Harness is currently being used to DEVELOP RoutePilot.

The finished application should not depend on an LLM unless a future feature
specifically benefits from it.

Possible future AI use cases:

- unstructured address extraction
- OCR cleanup
- natural-language import
- explanation assistance

The optimizer itself must remain testable without an LLM.


==================================================
24. DEMO MVP
==================================================

The first implementation should demonstrate the product without requiring
paid map APIs.

Create a deterministic demo dataset representing approximately 30 realistic
service stops.

The demo must include:

- departure location
- finish location
- departure time
- several service windows
- service durations
- priorities
- deterministic synthetic travel costs

Include the specific demonstration scenario:

departure around 04:00
several customer locations open around 08:00

The first-stop selector must demonstrate that:

- nearest is not always selected;
- farthest is not always selected;
- departure time can change the recommended first stop.

All synthetic travel data must be clearly labelled DEMO/SYNTHETIC.

Never display synthetic distance or time as real road routing data.


==================================================
25. DEMO USER EXPERIENCE
==================================================

The demo web application should visually resemble a polished commercial
route planning product rather than a coding tutorial.

Primary workspace:

large map area
+
route/timeline panel
+
route summary

Show:

- number of active stops
- departure time
- start
- first stop
- finish
- ordered route
- ETA
- waiting time when applicable
- service windows
- before/after optimization estimate
- estimated distance
- estimated duration
- saved distance
- saved time

For AUTO:

show the automatically applied first stop plus "Why this stop?"
and alternative top-K candidates.

The user must be able to override it.


==================================================
26. STORAGE
==================================================

Use SQLite for the initial version.

At minimum plan entities for:

- route plans
- route stops
- route optimization runs
- application settings

The exact schema should be proposed before implementation.

Database/storage code must not leak into optimization logic.


==================================================
27. TESTABILITY
==================================================

Core must be testable without:

- network
- paid map API
- browser
- external LLM

Use deterministic tests.

At minimum the eventual test suite must verify:

1. START is not treated as a service stop.
2. FINISH remains fixed.
3. Every enabled service stop is visited exactly once.
4. Disabled stops are excluded.
5. A manually pinned first stop stays first.
6. A locked AUTO recommendation keeps its original selection provenance.
7. AUTO produces a complete route without driver confirmation.
8. AUTO is not simply nearest-stop selection.
9. AUTO is not simply farthest-stop selection.
10. Changing departure time may change the first-stop ranking.
11. Waiting time is calculated correctly.
12. Service cannot start before the opening time.
13. ETA calculations are deterministic.
14. Time-window violations are identified.
15. Local times in DST gaps fail strict validation.
16. Ambiguous DST local times require explicit disambiguation.
17. Route optimization does not duplicate or lose stops.
18. Local improvement must not worsen the accepted baseline cost.
19. Reoptimization preserves user pinning.
20. Demo results are deterministic.
21. SQLite route save/load round-trips correctly.


==================================================
28. DEVELOPMENT SAFETY
==================================================

Use Git from the beginning.

Never commit:

- secrets
- API keys
- local databases
- logs
- caches
- temporary artifacts

Create:

.env.example
.gitignore
README.md
docs/ARCHITECTURE.md
docs/DECISIONS.md

Important decisions must be preserved in documentation rather than relying
only on chat/session history.


==================================================
29. FUTURE CAPABILITIES — NOT MVP
==================================================

Record but do not implement prematurely:

- real geocoding
- real road travel matrix
- traffic
- side-of-road routing
- U-turn modelling
- one-way road behaviour
- real working hours integrations
- CSV/Excel/PDF import
- OCR
- live navigation hand-off
- active-leg locking
- multi-vehicle routing
- dispatcher mode
- route sharing
- driver mobile UI
- route history and analytics


==================================================
30. IMPORTANT PRODUCT PRINCIPLE
==================================================

Do not optimize only for mathematical shortest distance.

RoutePilot's intended differentiation is:

"A route that is practical for the driver."

Therefore architecture must preserve the ability to model real operational
costs and constraints instead of reducing the domain to coordinates plus
distance.


==================================================
31. CURRENT TASK FOR DEEPSEEK HARNESS
==================================================

DO NOT CREATE FILES YET.

First:

1. Treat this specification as the base Source of Truth.
2. Compare it against the already approved decision registry D1–D13.
3. Produce a structured diff containing:
   - fully consistent items;
   - additions in this specification;
   - terminology differences;
   - genuine contradictions;
   - decisions still missing.
4. Do NOT reinterpret D1–D13.
5. Do NOT change approved decisions without explicitly identifying a conflict.
6. Propose the final domain model and project directory structure.
7. Propose Stage 0 scope.
8. Identify only architectural questions that genuinely block implementation.

Then STOP.

Do not start implementation until I explicitly approve the reconciliation.
