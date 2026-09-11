"""Deterministic local improvement (Stage 2 units U2/U3 recovery; v2 sections 20, 21, D17/D22).

The seed gives a route; this module makes it better without ever making it worse. Five rules
carry the whole unit:

* **the driver's first stop stays first** (I3, D10). No move is even generated that would move
  position 0;
* **every enabled stop stays in the order exactly once** (v2 section 21, spec section 27.3/27.17)
  - moves are permutations, so nothing can be duplicated, dropped or replaced by a disabled stop;
* **acceptance is lexicographic**: fewer hard-window violations is always better, and among
  routes with the same number of violations the earlier finish wins. No accepted move may worsen
  the accepted objective or increase the violation count, so the result is monotone by
  construction;
* **acceptance is decided by a full evaluation of the true objective** - never by the cheap
  ranking below. The ranking only decides the *order* in which moves are evaluated;
* **hard infeasibility is never converted into a numeric penalty** (D13 amendment). Violations
  stay a count and the objective stays the measured elapsed time; there is no dominance factor
  anywhere in this module.

Two neighbourhoods are searched, in a fixed order: **2-opt** (reverse the segment between two
positions) and **Or-opt / relocate** (move a segment of one or two consecutive stops to another
position, preserving its internal order). The neighbourhood is the **full U2 neighbourhood**
(``max_span=None``): no position is out of reach.

What U3 changed is *how the search spends its evaluations*, not what it accepts. The earlier U3
revision also truncated the neighbourhood to ``max_span=4`` and evaluated only the eight
best-ranked moves of each pass, which lost long-range improving moves and moves that trade driving
for fewer hard-window violations; that is the approximation the U3 recovery removes. Every pass now
walks the **whole U2 neighbourhood** - no span cut, no shortlist, nothing removed from it:

1. **rank**: price every move of both neighbourhoods with :func:`move_travel_delta_sec`, the exact
   number of seconds of driving the move adds or removes, read from the route's own frozen legs in
   time proportional to the edges the move touches (never to the whole route);
2. **verify**: fully evaluate the true objective of the neighbourhood's moves, most promising travel
   delta first, until the deterministic evaluation bound is reached;
3. **accept**: the best improving move, under the same lexicographic rule as before.

Because every move of the neighbourhood is ranked and every one of them is a verification
candidate, the ordering can never cost the search a move: it only decides which moves are reached
first when the deterministic evaluation bound below truncates a pass. The search therefore
reproduces the U2 search exactly whenever the bound does not bind - same neighbourhood, same
acceptance, same tie-break - and never *claims* more: a pass the bound cut short is reported
through :attr:`LocalSearchResult.budget_exhausted`, so the report can say "not every move was
examined" instead of asserting local optimality (v2 section 20).

The number of evaluations is bounded by ``max_evaluations``. The bound is a fixed count of route
evaluations, never a wall-clock deadline, so a slower machine produces the same route (no
wall-clock, no randomness - the optimized path stays deterministic). It is the same bound U2
already carried, and at ~50 enabled stops it **binds**: one pass is more moves than the bound
allows, so the last pass of every candidate is truncated and ``budget_exhausted`` is ``True``. That
is the interim limitation the owner accepted (decision D34) - the **candidate set** stays exhaustive
and the neighbourhood is never cut, and the truncated search is reported rather than hidden.

**How a route is priced (U7)** - the incremental / delta evaluator. Nothing about *which* routes are
priced, in *which* order, or which one is accepted changed; only the cost of pricing one route did:

* each pass computes the base route's own evaluated state at **every position** once
  (:func:`prepare_prefix_states`): the prepared index the driver stands at, the elapsed seconds, the
  violations so far and the cumulative travel, waiting, service and distance;
* a move's **divergence** - the first position it changes - is derived from the move itself
  (:func:`move_divergence`), never from a scan: ``start`` for a 2-opt reversal, ``start`` or
  ``anchor + 1`` for a relocate depending on the direction;
* the candidate is then priced by **resuming** from the base state at that divergence and walking
  only the runs the move reorders (:func:`move_index_runs`), plus the FINISH leg. The prefix is not
  re-priced at all.

The reuse is exact **by construction**, not by tolerance. The forward pass is a deterministic pure
function of the route prefix over integer seconds - the same frozen travel table, the same
``bisect_right`` over the prepared local midnights, the same prepared window-bounds row, the same
``WindowEndPolicy.SERVICE_FINISH_BEFORE_END`` lateness arithmetic - so a reused prefix is
bit-identical to recomputing it. That equality is not asserted in prose: the default test suite
compares every move of a whole neighbourhood, and a sampled set on the demo plan, against the
reference full pass field by field (violations, finish elapsed, travel, waiting, service,
distance), and the opt-in slow comparison checks the whole search - ranked and rejected candidates,
top-K order, recommended stop, per-candidate duration, violations, FINISH arrival and stop set -
against the reference path. ``evaluations`` keeps its meaning exactly: one unit per complete-route
evaluation, so the same ``max_evaluations`` ceiling truncates at the same point.

Three profile-driven costs of the *same* arithmetic were removed as well, each one exact and each
one documented where it lives (the measured figures are the benchmark's, and the equivalence gate
above covers all three):

* the day-offset step is **carried** along a candidate instead of bisected at every stop
  (:meth:`PreparedSearch.finish_elapsed`), because the arrival clock only moves forward, so
  ``bisect_right`` can only return the same step or a later one;
* a stop with **no fixed window** skips the resolver call entirely (``PreparedSearch.has_window``):
  ``RouteProblem.window_for`` returns ``None`` for every arrival of such a stop, from that very flag;
* a pass the evaluation budget **cannot truncate** does not price the travel delta of its moves at
  all (:func:`improve`), because that number is only an evaluation *order*, and such a pass reaches
  every move in the neighbourhood's own fixed generation order anyway.

The reference path is untouched and still callable: :func:`apply_move` produces the candidate order,
:meth:`RouteProblem.route_objective_key` prices it, and :meth:`RouteProblem._route_from` is the full
forward pass those two rest on.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from core.engine.optimizer.route_problem import (
    RouteProblem,
    require_complete_route,
)
from core.model.ids import StopId
from core.model.service_window import WindowEndPolicy
from core.validation.errors import InvalidOrderError, InvalidRoutePlanError

__all__ = [
    "DEFAULT_MAX_PASSES",
    "DEFAULT_MAX_SPAN",
    "IncrementalEvaluation",
    "LocalSearchResult",
    "PreparedSearch",
    "PrefixState",
    "RouteMove",
    "RouteTravel",
    "accepted",
    "apply_move",
    "candidate_moves",
    "improve",
    "move_divergence",
    "move_index_runs",
    "move_travel_delta_sec",
    "prepare_prefix_states",
    "prepare_route",
    "relocate_delta_sec",
    "reverse_delta_sec",
]

#: Evaluations allowed per improvement pass by default. The bound keeps the worst case (a
#: pathologically large plan) deterministic instead of open-ended. It is cumulative over the
#: passes, exactly as it was in U2, so the shipped search is the U2 search whenever it does not
#: bind; when it does bind, the pass it cut short is reported by
#: :attr:`LocalSearchResult.budget_exhausted`.
DEFAULT_MAX_EVALUATIONS = 20_000

#: Improvement passes allowed by default (U2's own bound). Each pass verifies the whole
#: neighbourhood, so the search converges by itself long before this on real plans; the bound only
#: keeps a pathologically large plan deterministic instead of open-ended.
DEFAULT_MAX_PASSES = 8

#: How far a move may reach within the route (see :func:`candidate_moves`). ``None`` is the
#: default and means the **full U2 neighbourhood**: every 2-opt reversal and every relocate of one
#: or two consecutive stops to every other anchor. A finite span remains available to a caller
#: that has benchmarked one for its own plan, but the shipped optimizer never truncates the
#: neighbourhood (v2 section 20: no unapproved approximation).
DEFAULT_MAX_SPAN: int | None = None


@dataclass(frozen=True)
class RouteMove:
    """One deterministic neighbourhood move of the route after position 0.

    ``start``, ``end`` and ``anchor`` are **stop positions**: ``0`` is the driver's first stop,
    ``1`` the second, and so on. That is the convention :func:`apply_move` applies and the one
    every delta below uses. The route's START sits one position before stop ``0`` and FINISH one
    position after the last stop.
    """

    #: ``"2-opt"`` (reverse the segment ``start..end``) or ``"relocate"`` (move ``length`` stops
    #: starting at ``start`` so that they sit immediately after ``anchor``).
    kind: str
    start: int = 0
    end: int = 0
    anchor: int = 0
    length: int = 0

    def describe(self) -> str:
        if self.kind == "2-opt":
            return f"2-opt reverse positions {self.start}..{self.end}"
        return f"relocate positions {self.start}..{self.start + self.length - 1} after {self.anchor}"

    @property
    def sort_key(self) -> tuple[str, int, int, int, int]:
        """A tie-break that does not depend on the order the moves were generated in."""
        return (self.kind, self.start, self.end, self.anchor, self.length)


@dataclass(frozen=True)
class LocalSearchResult:
    """The improved route and the evidence that improvement was monotone and bounded.

    ``accepted_moves`` is empty when no fully evaluated move of any pass improved the true
    objective of the seed - which is a real, reportable outcome, not a failure.

    ``evaluations`` counts complete-route objective evaluations: the seed's own evaluation and the
    verified moves of every pass. ``screened_moves`` counts the moves of the full neighbourhood the
    travel-delta ranking priced. ``budget_exhausted`` is ``True`` when ``max_evaluations`` stopped a
    pass before the whole neighbourhood had been evaluated: the route is then the best of the moves
    that *were* examined and must never be reported as locally optimal over the neighbourhood
    (v2 section 20).
    """

    order: tuple[StopId, ...]
    seed_objective: int
    final_objective: int
    accepted_moves: tuple[RouteMove, ...]
    evaluations: int
    seed_violations: int = 0
    final_violations: int = 0
    passes: int = 0
    screened_moves: int = 0
    budget_exhausted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", tuple(self.order))
        object.__setattr__(self, "accepted_moves", tuple(self.accepted_moves))
        # Monotone by construction, and re-checked here: the accepted route is never worse than
        # the seed. Acceptance is lexicographic, so "not worse" means fewer violations, or the
        # same number of violations and no later finish. It is deliberately *not* "seconds never
        # increase": a route that removes a hard-window violation is better even when it is
        # longer, because keeping a promise to a customer is not something seconds can buy
        # (v2 section 21, D13 amendment - infeasibility is never priced).
        if (self.final_violations, self.final_objective) > (
            self.seed_violations,
            self.seed_objective,
        ):
            raise InvalidRoutePlanError(
                "local improvement must never accept a route that is worse than the seed under "
                "the lexicographic rule (fewer hard-window violations first, then the earlier "
                "finish) - v2 section 21, D17"
            )

    @property
    def objective_improvement(self) -> int:
        """Seconds saved when the violation count did not change; negative otherwise.

        Read it together with :attr:`violations_removed`: a route that removes a hard-window
        violation is an improvement even if it is longer, and this number then reports the price
        instead of hiding it.
        """
        return self.seed_objective - self.final_objective

    @property
    def violations_removed(self) -> int:
        return self.seed_violations - self.final_violations


# --------------------------------------------------------------------------- #
# the acceptance rule (shared with the tests)
# --------------------------------------------------------------------------- #
def accepted(candidate: tuple[int, int], current: tuple[int, int]) -> bool:
    """Whether ``candidate`` is accepted over ``current``: lexicographic, never a penalty.

    Both arguments are the acceptance key ``(violations, elapsed seconds)`` of a complete route
    (:func:`core.engine.optimizer.route_problem.fast_objective`). Fewer hard-window violations
    wins first; among equally violating routes the earlier finish wins. Nothing else can make a
    move acceptable, so a move can never trade a violation for seconds and can never worsen
    ``current``'s objective.
    """
    return candidate < current


# --------------------------------------------------------------------------- #
# neighbour generation
# --------------------------------------------------------------------------- #
def apply_move(order: Sequence[StopId], move: RouteMove) -> tuple[StopId, ...]:
    """Apply one move. Position 0 is never touched, so the first stop always survives."""
    sequence = list(order)
    if move.kind == "2-opt":
        return tuple(
            sequence[: move.start]
            + list(reversed(sequence[move.start : move.end + 1]))
            + sequence[move.end + 1 :]
        )
    if move.kind == "relocate":
        segment = sequence[move.start : move.start + move.length]
        rest = sequence[: move.start] + sequence[move.start + move.length :]
        # ``anchor`` always refers to an element that stays in place, so no index bookkeeping is
        # needed after the segment is lifted out.
        position = rest.index(sequence[move.anchor])
        return tuple(rest[: position + 1] + segment + rest[position + 1 :])
    raise InvalidRoutePlanError(f"unknown local-search move kind {move.kind!r}")


def candidate_moves(
    count: int, *, max_segment: int = 2, max_span: int | None = DEFAULT_MAX_SPAN
) -> Iterator[RouteMove]:
    """Every move of both neighbourhoods that keeps position 0 fixed, in a fixed order.

    ``max_span`` bounds how far a move may reach: a 2-opt reversal spans at most ``max_span``
    positions and a relocate moves its segment to an anchor within ``max_span`` positions of where
    it was. The default is ``None`` - the **full U2 neighbourhood**, which is what the shipped
    search uses - and a finite span is an explicit choice a caller must make and justify on its own
    plan (v2 section 20).

    This is the *neighbourhood* of the search, not a prefilter of anything: :func:`improve` ranks
    every move it yields and evaluates them most promising first, up to its deterministic evaluation
    bound (which binds from ~50 enabled stops, D34); no move is ever removed from the neighbourhood, every
    enabled stop is still a candidate for the first stop, and no candidate first stop is dropped.
    """
    if count < 3:
        return
    span = count if max_span is None else max(1, max_span)
    # The moves are built with positional arguments: the search enumerates the whole neighbourhood
    # once per pass - tens of thousands of moves on the ~50-stop fixture - and the constructor is
    # the only per-move allocation left in that loop (U7, item 4). The fields are unchanged.
    for start in range(1, count - 1):
        for end in range(start + 1, min(count, start + span)):
            yield RouteMove("2-opt", start, end)
    for length in range(1, max_segment + 1):
        for start in range(1, count - length + 1):
            lower = max(1, start - span)
            upper = min(count - 1, start + length - 1 + span)
            for anchor in range(lower, upper + 1):
                if anchor != start and not (start < anchor < start + length):
                    yield RouteMove("relocate", start, 0, anchor, length)


# --------------------------------------------------------------------------- #
# the screen: the exact travel delta of a move
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteTravel:
    """One route prepared for O(1) travel deltas: its leg costs and its edge table.

    Route positions are ``0`` (START), ``1..count`` (the stops of the route) and ``count + 1``
    (FINISH); ``points[i]`` is the prepared-problem index of position ``i`` and ``table`` is the
    prepared problem's own extended leg table, so ``edge(a, b)`` is ``table[points[a] * sizes +
    points[b]]`` - one list index, no dictionary and no ``GeoPoint`` hashing. Built once per
    improvement pass.

    ``reverse_deltas[i]`` is the internal part of the travel delta of reversing positions
    ``i .. e``, accumulated once per pass, so a 2-opt move's delta is two lookups and one
    subtraction instead of a loop over the reversed segment.
    """

    points: tuple[int, ...]
    table: tuple[int, ...]
    sizes: int
    reverse_deltas: tuple[int, ...]

    @property
    def count(self) -> int:
        """The number of service stops in the route."""
        return len(self.points) - 2


def prepare_route(problem: RouteProblem, order: Sequence[StopId]) -> RouteTravel:
    """Prepare a candidate route for O(1) travel deltas (see :class:`RouteTravel`).

    Every leg and every point index is read from the prepared problem's frozen tables, so this
    costs one O(n) pass per improvement pass instead of one route re-pricing per move.
    """
    table = problem._extended_travel_table()
    sizes = problem.stop_count + 2
    points = [problem.start_index]
    points.extend(problem.index_of(stop_id) for stop_id in order)
    points.append(problem.finish_index)
    reverse_deltas: list[int] = [0] * len(points)
    running = 0
    for position in range(1, len(points) - 2):
        origin = points[position]
        destination = points[position + 1]
        running += (
            table[destination * sizes + origin] - table[origin * sizes + destination]
        )
        reverse_deltas[position + 1] = running
    return RouteTravel(
        points=tuple(points),
        table=table,
        sizes=sizes,
        reverse_deltas=tuple(reverse_deltas),
    )


def reverse_delta_sec(route: RouteTravel, start: int, end: int) -> int:
    """The exact travel delta of reversing stop positions ``start..end`` (2-opt).

    A reversal reverses the segment's internal edges and replaces the two edges joining the
    segment to its neighbours. ``start`` and ``end`` are stop positions, so the segment is
    ``points[start + 1] .. points[end + 1]`` - position ``0`` is START, which a move can never
    touch. The internal part comes from the pass's accumulated ``reverse_deltas``.
    """
    points = route.points
    table = route.table
    sizes = route.sizes
    before = points[start]
    first = points[start + 1]
    last = points[end + 1]
    after = points[end + 2]
    return (
        table[before * sizes + last]
        - table[before * sizes + first]
        + table[first * sizes + after]
        - table[last * sizes + after]
        + route.reverse_deltas[end + 1]
        - route.reverse_deltas[start + 1]
    )


def relocate_delta_sec(route: RouteTravel, start: int, length: int, anchor: int) -> int:
    """The exact travel delta of moving stop positions ``start..start + length - 1`` after ``anchor``.

    Moving a contiguous segment preserves its internal order, so only the legs joining the segment
    to its neighbours can change, and there are exactly three of them. Lifting the segment out of
    the route replaces ``prev -> segment_first`` and ``segment_last -> next`` with
    ``prev -> next``; inserting it after the anchor replaces ``anchor -> anchor_next`` with
    ``anchor -> segment_first`` and ``segment_last -> anchor_next``. The same three substitutions
    describe a segment moving backwards and a segment moving forwards, and the result is the move's
    own travel delta by construction - no leg of the rest of the route can change.

    That is what makes the full U2 neighbourhood affordable to rank: the delta is six table reads
    and five subtractions, independent of the route length and of how far the segment travels, so
    the cost of adapting the search to a plan does not grow into the number of moves. The route's
    points are read once per improvement pass from the prepared problem's frozen extended travel
    table (see :class:`RouteTravel`), so this never asks the leg cache a question.

    Route positions are the :class:`RouteMove` convention: ``0`` is the first service stop, START
    sits one position before it and FINISH one position after the last stop. ``anchor == start - 1``
    is the no-op - the segment already sits immediately after its anchor - and an anchor inside the
    segment is rejected, because "after itself" is not a move (the generator never emits one).
    ``tests/engine/test_optimizer_performance.py`` checks the result against a full evaluation of
    the move's own route for every move of the neighbourhood.
    """
    if anchor < 0 or anchor > route.count - 1:
        raise InvalidRoutePlanError(
            f"a relocate anchor must be a stop position of the route, got {anchor}"
        )
    if start < 1 or start + length > route.count:
        raise InvalidRoutePlanError(
            f"a relocate segment must be inside the route after its first stop, got "
            f"{start}..{start + length - 1} of {route.count} stops"
        )
    if anchor == start - 1:
        return 0
    if start <= anchor <= start + length - 1:
        raise InvalidRoutePlanError(
            f"a relocate anchor must be outside the segment it moves, got anchor {anchor} inside "
            f"{start}..{start + length - 1}"
        )
    points = route.points
    table = route.table
    sizes = route.sizes
    previous = points[start]
    segment_first = points[start + 1]
    segment_last = points[start + length]
    following = points[start + length + 1]
    anchor_point = points[anchor + 1]
    anchor_next = points[anchor + 2]
    return (
        table[previous * sizes + following]
        + table[anchor_point * sizes + segment_first]
        + table[segment_last * sizes + anchor_next]
        - table[previous * sizes + segment_first]
        - table[segment_last * sizes + following]
        - table[anchor_point * sizes + anchor_next]
    )


def move_travel_delta_sec(
    problem: RouteProblem, order: Sequence[StopId], move: RouteMove
) -> int:
    """How much driving ``move`` adds (positive) or removes (negative) from ``order``, in seconds.

    This is the number the screen of v2 section 20 step 2 ranks on: it is the **exact** change the
    move makes to the route's total driving time, computed from the same frozen legs a full
    evaluation prices and in time proportional to the edges the move touches, never to the whole
    route. ``tests/engine/test_optimizer_performance.py`` checks it against
    ``fast_evaluate(after).travel_sec - fast_evaluate(before).travel_sec`` for every move of the
    neighbourhood, so a screen that ranked anything other than the move it was given would fail
    that test.

    It is exact about travel and deliberately silent about waiting and hard windows: a move that
    saves driving can still be a worse complete route, which is exactly why **acceptance is
    decided by a full complete-route evaluation** and never by this number (v2 section 21, D13
    amendment).
    """
    return _delta_with_edges(prepare_route(problem, order), move)


def _delta_with_edges(route: RouteTravel, move: RouteMove) -> int:
    """The exact travel delta of ``move`` on a prepared route, by the move's own convention."""
    if move.kind == "2-opt":
        return reverse_delta_sec(route, move.start, move.end)
    if move.kind != "relocate":
        raise InvalidRoutePlanError(f"unknown local-search move kind {move.kind!r}")
    return relocate_delta_sec(route, move.start, move.length, move.anchor)


# --------------------------------------------------------------------------- #
# the neighbourhood moves, as index permutations (no tuple copy in the hot loop)
# --------------------------------------------------------------------------- #
def _route_indices(problem: RouteProblem, order: Sequence[StopId]) -> list[int]:
    """The prepared-table index of every stop of ``order``, in route order.

    One O(n) pass over the route, and the only place the hot loop resolves a stop id to a prepared
    index. Every move is then a permutation of a *list of integers*, so a candidate order costs one
    list build instead of a tuple of ids plus one dictionary lookup per stop.
    """
    index_of = problem.stop_index.get
    resolved: list[int] = []
    for stop_id in order:
        index = index_of(stop_id)
        if index is None:
            raise InvalidRoutePlanError(
                f"stop {stop_id!r} is not an enabled stop of plan {problem.plan.id!r}"
            )
        resolved.append(index)
    return resolved


def move_index_runs(indices: Sequence[int], move: RouteMove) -> tuple[Sequence[int], ...]:
    """The stopping sequence ``move`` produces, as the runs of ``indices`` it is made of.

    Same permutation :func:`apply_move` produces, on prepared indices instead of stop ids, but
    expressed as contiguous **runs** of the base order instead of a freshly built ``n``-element list.
    The evaluator walks the runs directly, so a candidate order costs the slice objects it is made of
    rather than a copy of the whole route - which is what the verification loop of
    :func:`improve` pays for every one of its ~20 000 moves, on every pass.

    A relocation is written as the stops between ``start`` and ``anchor`` in their new order (the
    segment lands immediately after the anchor), and a 2-opt reversal as the reversed segment plus
    the untouched tail. ``tests/engine/test_optimizer_performance.py`` checks the runs against
    :func:`apply_move` for every move of the full neighbourhood, so the two cannot drift apart.
    """
    start = move.start
    if move.kind == "2-opt":
        return (
            indices[start : move.end + 1][::-1],
            indices[move.end + 1 :],
        )
    if move.kind == "relocate":
        length = move.length
        end = start + length
        if move.anchor > start:
            # The segment moves forward: the stops it passed keep their order, then the segment,
            # then everything after the anchor.
            return (
                indices[end : move.anchor + 1],
                indices[start:end],
                indices[move.anchor + 1 :],
            )
        # The segment moves backward: it lands immediately before the stops it passed, which keep
        # their order behind it, then everything after the segment.
        return (
            indices[start:end],
            indices[move.anchor + 1 : start],
            indices[end:],
        )
    raise InvalidRoutePlanError(f"unknown local-search move kind {move.kind!r}")


def _move_permutation(indices: Sequence[int], move: RouteMove) -> list[int]:
    """The whole index permutation of ``move``: the base prefix, then the runs of the reordering.

    The same permutation :func:`apply_move` produces, on prepared indices instead of stop ids. The
    evaluator itself walks the runs and never builds this list (see
    :meth:`PreparedSearch.evaluate_segments`); it exists for a caller or a test that wants the
    candidate order as one sequence.
    """
    divergence = move_divergence(move)
    permutation = list(indices[:divergence])
    for run in move_index_runs(indices, move):
        permutation.extend(run)
    return permutation


def move_divergence(move: RouteMove) -> int:
    """The first stop position ``move`` changes - the length of the prefix that is left untouched.

    The forward pass of a complete route is a deterministic pure function of the route prefix over
    integer seconds, so a candidate that serves the same first ``d`` stops as the base order must
    produce a bit-identical prefix state up to position ``d``. That is what makes prefix reuse
    exact, and this is the ``d`` of each neighbourhood:

    * a **2-opt** reversal of positions ``start..end`` leaves ``0..start-1`` in place, so ``d`` is
      ``start``;
    * a **relocate** of ``start..start + length - 1`` to the position after ``anchor`` leaves
      ``0..d-1`` in place for ``d = start`` when the segment moves forward and ``d = anchor + 1``
      when it moves backward: a forward move first replaces the stop it was lifted from, and a
      backward move first replaces the stop right after the anchor with the moved segment.

    Position 0 is never moved by either neighbourhood (I3/D10), so ``d`` is at least ``1`` for every
    move :func:`candidate_moves` yields and the driver's first stop is never treated as
    re-evaluated state that could differ.
    """
    if move.kind == "2-opt":
        return move.start
    if move.kind == "relocate":
        return move.start if move.anchor > move.start else move.anchor + 1
    raise InvalidRoutePlanError(f"unknown local-search move kind {move.kind!r}")


@dataclass(frozen=True)
class IncrementalEvaluation:
    """One complete route priced by prefix reuse, with every metric the reference pass reports.

    The fields are exactly the ones :func:`core.engine.optimizer.route_problem._route_cost` returns
    (plus the objective and the violation count themselves), so an incremental evaluation and a
    full one can be compared field by field in a test. ``reused_positions`` is the length of the
    prefix that was resumed from - it is evidence about *how* the route was priced, never part of
    the answer.
    """

    violations: int
    finish_elapsed_sec: int
    travel_sec: int
    waiting_sec: int
    service_sec: int
    distance_m: float
    reused_positions: int

    @property
    def key(self) -> tuple[int, int]:
        """The acceptance key, exactly as :meth:`RouteProblem.route_objective_key` returns it."""
        return (self.violations, self.finish_elapsed_sec)


@dataclass(frozen=True)
class PrefixState:
    """The evaluated state of a base route after a given number of stops, for prefix reuse.

    ``previous_index`` and ``elapsed`` are exactly the two arguments
    :meth:`RouteProblem._route_from` receives when it is asked to price the suffix that starts at
    this position; the other five fields are the cumulative violations, travel, waiting, service and
    distance the route has already accumulated by then. Both halves are **sums over the same
    prefix**, so resuming from them is bit-identical to recomputing the prefix - which is the whole
    exactness argument of the incremental evaluator.
    """

    previous_index: int
    elapsed: int
    violations: int
    travel_sec: int
    waiting_sec: int
    service_sec: int
    distance_m: float


def prepare_prefix_states(
    problem: RouteProblem,
    context: "PreparedSearch",
    indices: Sequence[int],
) -> list[PrefixState]:
    """The exact evaluated state of the base route at every position, for prefix reuse.

    ``states[i]`` is the state **after** the first ``i`` stops of ``indices`` have been served, so
    ``states[divergence]`` is where a candidate that shares those ``i`` positions resumes. ``states[0]``
    is START at elapsed ``0`` with every metric at zero.

    It is computed by the very same arithmetic the full forward pass uses - the frozen travel table,
    ``bisect_right`` over the prepared local midnights, the prepared window-bounds row of the arrival
    date, the arrival-clock waiting and the metric accumulation - over the same integer seconds, so
    resuming from ``states[d]`` is bit-identical to recomputing the prefix, not an approximation of
    it. That equality is what :meth:`PreparedSearch.evaluate_move` rests on, and it is what
    ``tests/engine/test_optimizer_performance.py`` proves move by move.
    """
    table = context.table
    sizes = context.sizes
    count = context.count
    durations = context.durations
    finish_before = context.finish_before
    bisect_right = bisect.bisect_right
    cutoffs = context.cutoffs
    step_rows = context.step_rows
    row_count = len(step_rows)
    bounds_row = context.bounds_row
    distances = context.distances
    starts_to_stop = context.starts_to_stop
    stop_ids = problem.stop_ids

    states: list[PrefixState] = [
        PrefixState(context.start_index, 0, 0, 0, 0, 0, 0.0)
    ]
    previous = context.start_index
    elapsed = 0
    travel_sec = 0
    waiting_sec = 0
    service_sec = 0
    distance_m = 0.0
    violations = 0
    finish_before_end = WindowEndPolicy.SERVICE_FINISH_BEFORE_END
    for index in indices:
        leg_travel = table[previous * sizes + index]
        travel_sec += leg_travel
        elapsed += leg_travel
        duration = durations[index]
        # The same day offset ``RouteProblem.day_offset_of`` computes, and the same prepared row it
        # selects - read as one list index per bisect step instead of a dictionary lookup.
        step = bisect_right(cutoffs, elapsed) - 1
        bounds = None
        if step >= 0:
            row = step_rows[step] if step < row_count else None
            if row is None:
                row = bounds_row(step)
            if row is not None:
                bounds = row[index]
        waiting = 0
        if bounds is None:
            window = problem.window_for(stop_ids[index], elapsed)
            if window is not None:
                opened = problem.elapsed_of(window.open_at)
                waiting = opened - elapsed if opened > elapsed else 0
                service_start = elapsed + waiting
                closed = problem.elapsed_of(window.close_at)
                if finish_before[index] is finish_before_end:
                    lateness = service_start + duration - closed
                else:
                    lateness = service_start - closed
                if lateness > 0:
                    violations += 1
        else:
            opened, closed = bounds
            waiting = opened - elapsed if opened > elapsed else 0
            service_start = elapsed + waiting
            if finish_before[index] is finish_before_end:
                lateness = service_start + duration - closed
            else:
                lateness = service_start - closed
            if lateness > 0:
                violations += 1
        waiting_sec += waiting
        service_sec += duration
        distance_m += (
            distances[previous * count + index] if previous < count else starts_to_stop[index]
        )
        elapsed += waiting + duration
        previous = index
        states.append(
            PrefixState(
                previous,
                elapsed,
                violations,
                travel_sec,
                waiting_sec,
                service_sec,
                distance_m,
            )
        )
    return states


class PreparedSearch:
    """One improvement pass' pricing tables, so a candidate route costs only its changed suffix.

    Everything a complete-route evaluation reads - the frozen travel table and its stride, the
    per-stop service durations and window-end policies, the arrival-clock local-midnight cutoffs and
    the prepared window-bounds row of each day offset, and the metric tables a distance breakdown
    needs - is resolved **once per pass** here and then read by index. That is the whole point of
    the class: :func:`_route_from` re-reads a dozen ``self`` attributes per stop, and the search
    prices ~20 000 complete routes per candidate, so the per-stop attribute reads dominated the
    measured cost (the U7 profile).

    The two evaluations it offers are the same arithmetic:

    * :meth:`evaluate_move` returns the **full** metric breakdown, for tests and for any caller that
      wants travel/waiting/service/distance as well;
    * :meth:`finish_elapsed` returns only what acceptance is decided on - ``(violations,
      finish_elapsed)`` - and stops early as soon as the candidate's violations exceed the current
      route's, which is sound because a violation count never decreases along a route and acceptance
      is lexicographic (fewer violations first), so such a move can never be accepted. It prices the
      same route and evaluates the same stops; only the pointless tail of a rejected candidate is
      not walked.

    Neither of them can change *which* routes the search prices, in *which* order, or which one it
    accepts - see :func:`improve`.
    """

    __slots__ = (
        "problem",
        "table",
        "sizes",
        "finish_base",
        "count",
        "start_index",
        "durations",
        "finish_before",
        "has_window",
        "cutoffs",
        "cutoff_count",
        "steps",
        "step_rows",
        "distances",
        "starts_to_stop",
        "to_finish_distance",
    )

    def __init__(self, problem: RouteProblem) -> None:
        self.problem = problem
        self.table = problem._extended_travel_table()
        count = problem.stop_count
        self.count = count
        self.sizes = count + 2
        self.finish_base = count + 1
        self.start_index = problem.start_index
        self.durations = problem.duration_by_stop
        self.finish_before = problem.window_end_policies
        #: ``has_window[index]`` is whether that stop has a fixed service window at all. When it
        #: does not, :meth:`RouteProblem.window_for` returns ``None`` for **every** arrival without
        #: touching the timezone (it tests this very flag first), so the hot loop can skip the call
        #: instead of asking a question whose answer cannot depend on the arrival. That is the same
        #: answer, reached one attribute read earlier - never a different one.
        self.has_window = tuple(window is not None for window in problem.fixed_windows)
        self.cutoffs = problem.cutoff_elapsed
        self.cutoff_count = len(self.cutoffs)
        self.steps = problem.step_offsets
        #: The prepared window-bounds row of each bisect step, filled in as steps are first reached.
        self.step_rows = _window_bounds_rows_by_step(problem)
        self.distances = problem.distance_table
        starts_to_stop, _finish_to_stop = problem._distance_ends_row()
        self.starts_to_stop = starts_to_stop
        self.to_finish_distance = problem.distance_to_finish

    def bounds_row(
        self, step: int
    ) -> tuple[tuple[int, int] | None, ...] | None:
        """The prepared window-bounds row of one bisect step, built once when it is first reached.

        ``None`` means the step selects no usable day offset (the arrival is before the first
        prepared local midnight, or its date is the last prepared one): the caller falls back to the
        authoritative resolver, exactly as :meth:`RouteProblem.day_offset_of` returning ``None``
        does. A row that is present but has a ``None`` entry for a stop means that stop's window has
        no resolution for that date - the DST-gap case - and the caller falls back for it too.

        A step at or past the last prepared one is answered with ``None`` rather than raising: every
        step ``bisect_right`` over :attr:`cutoffs` can return is inside the table
        (:attr:`step_rows` has one entry per offset, and an offset is at least the step's own index),
        so this is a guard, not a case the search reaches.
        """
        if step >= len(self.step_rows):
            return None
        row = self.step_rows[step]
        if row is None:
            offset = self.steps[step]
            if offset is not None and offset < len(self.step_rows):
                row = self.problem.window_bounds_row(offset)
                self.step_rows[step] = row
        return row

    # ---- the hot path: the acceptance key only --------------------------- #
    def finish_elapsed(
        self,
        runs: Sequence[Sequence[int]],
        divergence: int,
        base_states: Sequence[PrefixState],
        current_key: tuple[int, int],
    ) -> tuple[int, int] | None:
        """``(violations, finish_elapsed)`` of a candidate order, priced by prefix reuse.

        ``runs`` is the candidate order's changed region, as the runs of base indices
        :func:`move_index_runs` produced; ``base_states`` are the base route's states (see
        :func:`prepare_prefix_states`), and the first ``divergence`` positions are not re-priced at
        all - they are resumed from the base's own state at that position.

        The violation count starts from the prefix's own count (``PrefixState.violations``), exactly
        as the reference pass's does, so the returned pair is the key the reference pass computes -
        never a smaller count. The early exit below is what prefix reuse buys on top: once the count
        has passed ``current_key``'s it can only grow, and acceptance is lexicographic, so the rest
        of a route that can no longer win is not walked.
        """
        table = self.table
        sizes = self.sizes
        durations = self.durations
        finish_before = self.finish_before
        has_window = self.has_window
        cutoffs = self.cutoffs
        cutoff_count = self.cutoff_count
        step_rows = self.step_rows
        bounds_row = self.bounds_row
        window_for = self.problem.window_for
        elapsed_of = self.problem.elapsed_of
        stop_ids = self.problem.stop_ids
        finish_before_end = WindowEndPolicy.SERVICE_FINISH_BEFORE_END

        resumed = base_states[divergence]
        previous_row = resumed.previous_index * sizes
        elapsed = resumed.elapsed
        violations = resumed.violations
        limit = current_key[0]
        # The day-offset step of the first arrival is the base route's own step at the divergence:
        # the candidate serves the very same prefix, so it arrives there at the very same second.
        # From that point on the step is carried instead of bisected, because the arrival clock only
        # moves forward along a route (legs, waiting and service are all non-negative), so the step
        # ``bisect_right`` would return can only stay or advance. Advancing it is the same lookup,
        # one comparison at a time - not a cheaper or coarser one. ``-1`` is carried by the same
        # loop, which is why it needs no separate case: ``cutoffs[0]`` is simply the first boundary
        # the arrival has to reach.
        step = bisect.bisect_right(cutoffs, elapsed) - 1

        for run in runs:
            for index in run:
                elapsed += table[previous_row + index]
                duration = durations[index]
                while step + 1 < cutoff_count and cutoffs[step + 1] <= elapsed:
                    step += 1
                bounds = None
                if step >= 0:
                    # ``step`` is at most ``len(cutoffs) - 1``, and ``step_rows`` holds one entry per
                    # prepared day offset (at least as many as there are cutoffs), so this index is
                    # always inside the list - see :meth:`bounds_row` for the guard on the other path.
                    row = step_rows[step]
                    if row is None:
                        row = bounds_row(step)
                    if row is not None:
                        bounds = row[index]
                if bounds is None:
                    # A stop without a fixed window has no window at any arrival, so the prepared
                    # rows and the authoritative resolver agree on ``None`` before any lookup: the
                    # call is skipped rather than answered differently (see ``has_window``).
                    if has_window[index]:
                        # The prepared rows cannot answer for this stop: the authoritative resolver
                        # decides, and raises for exactly the arrivals ``evaluate_order`` rejects.
                        window = window_for(stop_ids[index], elapsed)
                        if window is not None:
                            opened = elapsed_of(window.open_at)
                            if opened > elapsed:
                                elapsed = opened
                            closed = elapsed_of(window.close_at)
                            if finish_before[index] is finish_before_end:
                                if elapsed + duration > closed:
                                    violations += 1
                            elif elapsed > closed:
                                violations += 1
                else:
                    opened, closed = bounds
                    if opened > elapsed:
                        elapsed = opened
                    if finish_before[index] is finish_before_end:
                        if elapsed + duration > closed:
                            violations += 1
                    elif elapsed > closed:
                        violations += 1
                if violations > limit:
                    # A violation count never decreases along a route and acceptance is lexicographic,
                    # so this candidate can never beat the current route: the rest of the route cannot
                    # change that. Nothing is invented either - ``None`` is only returned once the
                    # count really has passed the current route's.
                    return None
                elapsed += duration
                previous_row = index * sizes

        return (violations, elapsed + table[previous_row + self.finish_base])

    # ---- the full breakdown, for equivalence testing --------------------- #
    def evaluate_move(
        self,
        runs: Sequence[Sequence[int]],
        divergence: int,
        base_states: Sequence[PrefixState],
    ) -> IncrementalEvaluation:
        """The **complete** metrics of a candidate order, priced by prefix reuse.

        ``runs`` is what :func:`move_index_runs` produced for the move. The travel, waiting, service
        and distance terms are accumulated exactly as
        :func:`core.engine.optimizer.route_problem._route_cost` accumulates them, so the two agree
        field by field. It is the form the equivalence tests compare against the reference path; the
        search's own hot loop uses :meth:`finish_elapsed`, which computes the same arithmetic
        without the metric terms acceptance never reads.
        """
        table = self.table
        sizes = self.sizes
        count = self.count
        durations = self.durations
        finish_before = self.finish_before
        has_window = self.has_window
        cutoffs = self.cutoffs
        cutoff_count = self.cutoff_count
        step_rows = self.step_rows
        bounds_row = self.bounds_row
        distances = self.distances
        starts_to_stop = self.starts_to_stop
        to_finish_distance = self.to_finish_distance
        window_for = self.problem.window_for
        elapsed_of = self.problem.elapsed_of
        stop_ids = self.problem.stop_ids
        finish_before_end = WindowEndPolicy.SERVICE_FINISH_BEFORE_END

        resumed = base_states[divergence]
        previous = resumed.previous_index
        previous_row = previous * sizes
        elapsed = resumed.elapsed
        travel_sec = resumed.travel_sec
        waiting_sec = resumed.waiting_sec
        service_sec = resumed.service_sec
        distance_m = resumed.distance_m
        violations = resumed.violations
        # The carried day-offset step; see :meth:`finish_elapsed` for why advancing it is the same
        # lookup ``bisect_right`` performs, not a coarser one.
        step = bisect.bisect_right(cutoffs, elapsed) - 1

        for run in runs:
            for index in run:
                leg_travel = table[previous_row + index]
                travel_sec += leg_travel
                elapsed += leg_travel
                duration = durations[index]
                while step + 1 < cutoff_count and cutoffs[step + 1] <= elapsed:
                    step += 1
                bounds = None
                if step >= 0:
                    row = step_rows[step]
                    if row is None:
                        row = bounds_row(step)
                    if row is not None:
                        bounds = row[index]
                waiting = 0
                if bounds is None:
                    # No fixed window means no window at any arrival: ``window_for`` would return
                    # ``None`` from this same flag without resolving anything.
                    window = window_for(stop_ids[index], elapsed) if has_window[index] else None
                    if window is None:
                        service_start = elapsed
                    else:
                        opened = elapsed_of(window.open_at)
                        waiting = opened - elapsed if opened > elapsed else 0
                        service_start = elapsed + waiting
                        closed = elapsed_of(window.close_at)
                        if finish_before[index] is finish_before_end:
                            lateness = service_start + duration - closed
                        else:
                            lateness = service_start - closed
                        if lateness > 0:
                            violations += 1
                else:
                    opened, closed = bounds
                    waiting = opened - elapsed if opened > elapsed else 0
                    service_start = elapsed + waiting
                    if finish_before[index] is finish_before_end:
                        lateness = service_start + duration - closed
                    else:
                        lateness = service_start - closed
                    if lateness > 0:
                        violations += 1
                waiting_sec += waiting
                service_sec += duration
                distance_m += (
                    distances[previous * count + index]
                    if previous < count
                    else starts_to_stop[index]
                )
                elapsed = service_start + duration
                previous = index
                previous_row = index * sizes

        finish_travel = table[previous_row + self.finish_base]
        distance_m += to_finish_distance[previous] if previous < count else 0.0
        return IncrementalEvaluation(
            violations=violations,
            finish_elapsed_sec=elapsed + finish_travel,
            travel_sec=travel_sec + finish_travel,
            waiting_sec=waiting_sec,
            service_sec=service_sec,
            distance_m=distance_m,
            reused_positions=divergence,
        )

    def evaluate_segments(
        self,
        indices: Sequence[int],
        divergence: int,
        base_states: Sequence[PrefixState],
    ) -> IncrementalEvaluation:
        """The full metrics of the candidate order ``indices``, priced over its changed suffix.

        The convenience form of :meth:`evaluate_move` for a caller (and a test) that holds the
        candidate order as one sequence: its first ``divergence`` positions are the base order's, and
        everything after them is walked. It therefore also **checks the shape of the reuse**: reading
        the tail from the candidate order itself means the reused prefix cannot be a different prefix.
        """
        return self.evaluate_move((indices[divergence:],), divergence, base_states)


def _window_bounds_rows_by_step(problem: RouteProblem) -> list[tuple[tuple[int, int] | None, ...] | None]:
    """The prepared window-bounds rows, as one list **indexed by the bisect step**.

    ``window_bounds_rows`` is a ``dict`` keyed by day offset; the hot loop needs the row of the step
    ``bisect_right(cutoff_elapsed, arrival) - 1`` selected, and every offset the search can select is
    inside ``problem.cutoff_offsets``. Indexing by step replaces one dictionary lookup per stop with
    one list index. Every entry starts as ``None`` and is filled in by
    :meth:`PreparedSearch.bounds_row` when a step is first reached - exactly as
    :meth:`RouteProblem.window_bounds_row` fills the dict today. Building every offset's row up front
    would resolve service windows for days no arrival of this search visits, and a window that falls
    into a DST gap on such a day must stay unanswered rather than raise (D3/D29, U2 review).
    """
    offsets = problem.cutoff_offsets
    if not offsets:
        return []
    return [None] * (max(offsets) + 1)


# --------------------------------------------------------------------------- #
# the search
# --------------------------------------------------------------------------- #
def improve(
    problem: RouteProblem,
    order: Sequence[StopId],
    *,
    max_passes: int = DEFAULT_MAX_PASSES,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
    max_span: int | None = DEFAULT_MAX_SPAN,
) -> LocalSearchResult:
    """Improve ``order`` deterministically, never accepting a worse route.

    Every pass walks the whole neighbourhood and fully evaluates its moves, most promising travel
    delta first, accepting the best improving move, and stops when the deterministic evaluation
    bound is reached. U2's acceptance rule, U2's tie-break and U2's neighbourhood are therefore all
    preserved: when ``max_evaluations`` does not bind, this is the exhaustive U2 search. The
    travel-delta ranking is only an evaluation *order*, so it cannot drop a move that would have
    been looked at; it decides which moves are reached first, and it is what keeps the search usable
    when the bound truncates the last pass - a truncation that is reported through
    :attr:`LocalSearchResult.budget_exhausted` rather than hidden. From ~50 enabled stops the bound
    binds and truncates the last pass of a candidate, which is the interim limitation the owner
    accepted (D34).

    ``order`` must already start with the driver's selected first stop and contain every enabled
    stop exactly once; anything else is rejected instead of repaired (v2 section 21).

    Every move is a permutation, so once the seed order has been checked against
    :func:`core.engine.optimizer.route_problem.require_complete_route`, no move can make a stop
    unserviceable; the inner loop therefore prices candidates with the unchecked forward pass. The
    committed route is still checked: :func:`core.engine.optimizer.optimize.optimize` evaluates the
    result with the authoritative engine and refuses any disagreement with the fast path.

    Raises:
        InvalidRoutePlanError: ``order`` is empty or does not start with ``problem.first_stop_id``.
        InvalidOrderError: ``order`` is not exactly the enabled stops, each exactly once.
    """
    sequence = tuple(order)
    if not sequence or sequence[0] != problem.first_stop_id:
        first = sequence[0] if sequence else None
        raise InvalidRoutePlanError(
            f"local improvement must start from an order beginning with the driver-selected first "
            f"stop {problem.first_stop_id!r}; got {first!r} (I3, D10)"
        )
    problem.plan.validate_order(sequence)
    if len(sequence) != problem.stop_count:
        raise InvalidOrderError(
            f"local improvement needs exactly the {problem.stop_count} enabled stops of plan "
            f"{problem.plan.id!r}, got {len(sequence)}"
        )
    # One authoritative serviceability check for the whole search: it covers the stop set, and
    # every move only reorders that set, so the inner loop may use the unchecked pass.
    require_complete_route(problem, sequence)

    current = problem.route_objective_key(sequence, require_serviceable=False)
    seed_objective = current[1]
    seed_violations = current[0]
    accepted_moves: list[RouteMove] = []
    evaluations = 1
    passes = 0
    screened_moves = 0
    budget_exhausted = False
    context = PreparedSearch(problem)
    stop_ids = problem.stop_ids

    while passes < max_passes and evaluations < max_evaluations:
        passes += 1
        moves = list(candidate_moves(len(sequence), max_span=max_span))
        screened_moves += len(moves)
        if not moves:
            break
        # Order the whole neighbourhood by the exact travel delta, then by the move's own fixed
        # order, so the evaluation order never depends on the order the moves were generated in.
        # The search is exhaustive whenever the evaluation budget can cover the neighbourhood; the
        # ordering only matters for the moves the budget does not reach, and it reaches the most
        # promising ones first. When the budget *does* cover the whole neighbourhood the ranking
        # cannot decide anything - every move is reached, in the neighbourhood's own fixed
        # generation order - so the exact travel delta is not priced at all on such a pass. That
        # removes work, never a move: ``screened_moves`` still counts the whole neighbourhood and
        # the evaluation order of an untruncated pass is the one it always was.
        if len(moves) > max_evaluations - evaluations:
            route = prepare_route(problem, sequence)
            ordered = sorted(
                moves,
                key=lambda move: (_delta_with_edges(route, move), move.sort_key),
            )
        else:
            ordered = moves

        # The base order's own evaluated state at every position, computed once per pass by the same
        # forward pass the candidate pricing uses (``prepare_prefix_states``). A candidate resumes
        # from the state of the last position its move left untouched, so only the changed suffix and
        # the FINISH leg are recomputed - the reuse is exact by construction, and
        # ``tests/engine/test_optimizer_performance.py`` proves it move by move.
        base_indices = _route_indices(problem, sequence)
        base_states = prepare_prefix_states(problem, context, base_indices)

        best_move: RouteMove | None = None
        best_indices: list[int] | None = None
        best_objective: tuple[int, int] | None = None
        best_key: tuple[tuple[int, int], tuple[int, ...], tuple[str, ...]] | None = None
        for move in ordered:
            if evaluations >= max_evaluations:
                # The neighbourhood was not fully evaluated: the route below is the best of the
                # moves that were examined, and the result must say so (v2 section 20).
                budget_exhausted = True
                break
            # Only the runs the move changes are handed to the evaluator: the candidate order is
            # never materialised as a whole tuple just to be priced (v2 section 20: same moves, same
            # order, same decisions; only *how* a route is priced changed). The move's divergence is
            # derived from the move itself, so it costs nothing on a pass whose ranking is not
            # needed.
            divergence = move_divergence(move)
            runs = move_index_runs(base_indices, move)
            objective = context.finish_elapsed(runs, divergence, base_states, current)
            evaluations += 1
            if objective is None:
                # The candidate already carries more hard-window violations than the current route,
                # so the lexicographic key can never accept it. The move is still evaluated - it is
                # the same move, in the same order, with the same evaluation counted - only the
                # remainder of a route that cannot win is not walked.
                continue
            if not accepted(objective, current):
                continue
            # Two moves that produce routes with the same true objective are separated by the
            # resulting route itself: its stops' prepared indices, then its stop ids (v2 section
            # 30). The winner therefore never depends on the order the moves were ranked in.
            # The candidate's own points are only built when the move can still win - the condition
            # below is the first two terms of the same lexicographic comparison.
            if best_key is not None and objective > best_key[0]:
                continue
            indices = _move_permutation(base_indices, move)
            points = tuple(indices)
            candidate = tuple(stop_ids[index] for index in indices)
            key = (objective, points, candidate)
            if best_key is None or key < best_key:
                best_key = key
                best_move = move
                best_indices = indices
                best_objective = objective

        if best_move is None or best_indices is None or best_objective is None:
            break
        sequence = tuple(stop_ids[index] for index in best_indices)
        current = best_objective
        accepted_moves.append(best_move)

    return LocalSearchResult(
        order=sequence,
        seed_objective=seed_objective,
        final_objective=current[1],
        accepted_moves=tuple(accepted_moves),
        evaluations=evaluations,
        seed_violations=seed_violations,
        final_violations=current[0],
        passes=passes,
        screened_moves=screened_moves,
        budget_exhausted=budget_exhausted,
    )
