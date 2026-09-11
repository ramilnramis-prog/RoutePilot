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
already carried, and at ~100 enabled stops it **binds**: one pass is more moves than the bound
allows, so pass 1 of every candidate is truncated and ``budget_exhausted`` is ``True``. That is the
interim limitation the owner accepted (decision D34) - the **candidate set** stays exhaustive and
the neighbourhood is never cut, and the truncated search is reported rather than hidden.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from core.engine.optimizer.route_problem import (
    RouteProblem,
    require_complete_route,
)
from core.model.ids import StopId
from core.validation.errors import InvalidOrderError, InvalidRoutePlanError

__all__ = [
    "DEFAULT_MAX_PASSES",
    "DEFAULT_MAX_SPAN",
    "LocalSearchResult",
    "RouteMove",
    "RouteTravel",
    "accepted",
    "apply_move",
    "candidate_moves",
    "improve",
    "move_travel_delta_sec",
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
    bound (which binds at ~100 stops, D34); no move is ever removed from the neighbourhood, every
    enabled stop is still a candidate for the first stop, and no candidate first stop is dropped.
    """
    if count < 3:
        return
    span = count if max_span is None else max(1, max_span)
    for start in range(1, count - 1):
        for end in range(start + 1, min(count, start + span)):
            yield RouteMove(kind="2-opt", start=start, end=end)
    for length in range(1, max_segment + 1):
        for start in range(1, count - length + 1):
            lower = max(1, start - span)
            upper = min(count - 1, start + length - 1 + span)
            for anchor in range(lower, upper + 1):
                if anchor != start and not (start < anchor < start + length):
                    yield RouteMove(kind="relocate", start=start, anchor=anchor, length=length)


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
    :attr:`LocalSearchResult.budget_exhausted` rather than hidden. At ~100 enabled stops the bound
    does bind and truncates pass 1, which is the interim limitation the owner accepted (D34).

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

    while passes < max_passes and evaluations < max_evaluations:
        passes += 1
        route = prepare_route(problem, sequence)
        ranked: list[tuple[int, RouteMove]] = []
        for move in candidate_moves(len(sequence), max_span=max_span):
            screened_moves += 1
            ranked.append((_delta_with_edges(route, move), move))
        if not ranked:
            break
        # Order the whole neighbourhood by the exact travel delta, then by the move's own fixed
        # order, so the evaluation order never depends on the order the moves were generated in.
        # The search is exhaustive whenever the evaluation budget can cover the neighbourhood; the
        # ordering only matters for the moves the budget does not reach, and it reaches the most
        # promising ones first.
        if len(ranked) > max_evaluations - evaluations:
            ranked.sort(key=lambda entry: (entry[0], entry[1].sort_key))

        best_move: RouteMove | None = None
        best_order: tuple[StopId, ...] | None = None
        best_objective: tuple[int, int] | None = None
        best_key: tuple[tuple[int, int], tuple[int, ...], tuple[str, ...]] | None = None
        for _delta, move in ranked:
            if evaluations >= max_evaluations:
                # The neighbourhood was not fully evaluated: the route below is the best of the
                # moves that were examined, and the result must say so (v2 section 20).
                budget_exhausted = True
                break
            candidate = apply_move(sequence, move)
            objective = problem.route_objective_key(candidate, require_serviceable=False)
            evaluations += 1
            if not accepted(objective, current):
                continue
            # Two moves that produce routes with the same true objective are separated by the
            # resulting route itself: its stops' prepared indices, then its stop ids (v2 section
            # 30). The winner therefore never depends on the order the moves were ranked in.
            points = tuple(problem.index_of(stop_id) for stop_id in candidate)
            key = (objective, points, candidate)
            if best_key is None or key < best_key:
                best_key = key
                best_move = move
                best_order = candidate
                best_objective = objective

        if best_move is None or best_order is None or best_objective is None:
            break
        sequence = best_order
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
