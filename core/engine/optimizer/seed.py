"""Constraint-aware greedy seed (Stage 2 units U2/U3; v2 sections 20, 21, decisions D17/D22).

The seed answers one question at every step: *which next stop makes the route I would have to
drive from here best?* Best means the same thing the product means everywhere else (v2 sections
12 and 15, D13 amendment):

1. fewest hard service-window violations - a violation is never traded for seconds;
2. then the earliest arrival at FINISH of the route completed from here, final leg included;
3. then the earliest ``input_position`` in the user's original list (v2 section 30, D33);
4. then the stop id.

Rules 3 and 4 exist so the seed is reproducible: two stops that make an equally good route are
never separated by accident.

This is deliberately **not** nearest-neighbour. A nearest-neighbour seed compares the *next leg*
only; here the winner is the stop that produces the better route, so a stop that arrives just in
time for its opening window beats a nearer stop that would leave the driver waiting for hours -
which is exactly the core scenario of the product (v2 section 1).

The compared route is the complete route the driver would drive from here: ``START -> the stops
already chosen -> the candidate -> FINISH``, priced by the **same forward pass**
``fast_evaluate`` runs - every leg, every waiting period, every hard-window outcome and the final
leg to FINISH included. It is **not** an approximation of the objective and it is not a
travel-delta proxy: the key is the true ``(violations, elapsed seconds)`` pair of a complete
route, exactly as acceptance is decided everywhere else (v2 section 21, D13 amendment). What the
seed adds on top is the greedy assumption that the stop worth visiting first is the one whose
complete route from here is best - the stops still to be chosen are appended after the choice, not
looked ahead over, and no weight, lookahead factor or penalty is introduced anywhere.

Unit U3 changed *how cheaply* that comparison is computed, not what it is. U2 priced the same
route through the leg cache and the per-arrival window resolver, and re-checked the whole problem's
serviceability for every candidate. U3 computes the identical number from the prepared tables
(:meth:`RouteProblem._route_from`, the unchecked table-only forward pass) and pays the
serviceability check **once** per seed. Every leg, waiting period, hard-window outcome and the
FINISH leg stay in the key, so the seed produces the order U2's seed produced at a fraction of the
cost; ``tests/engine/test_optimizer.py`` pins that equality and the measurements are in the U3
report.

Nothing is skipped and nothing is prefiltered: every remaining stop is priced at every step,
so the seed's ``evaluations`` is exactly ``n * (n - 1) / 2`` for ``n`` enabled stops. Nothing is
weighted or tuned, and nearest-neighbour is still never the product algorithm (D17).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import timedelta

from core.engine.optimizer.route_problem import (
    RouteProblem,
    require_complete_route,
)
from core.model.ids import StopId
from core.model.service_window import WindowEndPolicy

__all__ = ["SeedResult", "build_seed", "greedy_seed", "seed_choice_key"]

#: The deterministic tie-break key of one greedy step:
#: ``(violations, complete-route elapsed seconds, input_position, id)``.
#:
#: Both leading terms describe the complete route that choosing the candidate produces - the
#: stops already chosen, then the candidate, then FINISH - so the candidate's own waiting, its
#: hard-window outcome and the final leg are all part of the comparison.
SeedChoiceKey = tuple[int, int, int, str]


@dataclass(frozen=True)
class SeedResult:
    """The greedy seed and the evidence of what building it cost.

    ``evaluations`` counts the candidate stops that were priced (one per remaining stop per step,
    so exactly ``n * (n - 1) / 2`` for ``n`` enabled stops). It is reported so the benchmark can
    show that the seed prices each candidate once instead of claiming a bound the code does not
    have: every remaining stop is still scored on every step and nothing is skipped (v2 section 20:
    no candidate prefilter).
    """

    order: tuple[StopId, ...]
    evaluations: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "order", tuple(self.order))


def seed_choice_key(
    problem: RouteProblem, violations: int, elapsed_sec: int, stop_id: StopId
) -> SeedChoiceKey:
    """The deterministic ordering key of one candidate next stop.

    ``violations`` and ``elapsed_sec`` are the complete-route evaluation of the route that choosing
    ``stop_id`` produces: the stops already chosen, then ``stop_id``, then FINISH. Waiting, hard
    windows and the final leg therefore all influence the choice - a candidate is compared as "the
    route I would actually drive", never as "the next leg". The two trailing terms - the user's
    original ``input_position`` and then the stop id (v2 section 30, D33) - are only ever consulted
    when two candidates produce exactly the same route, which is why the seed's hot loop compares
    the leading terms first and asks for them only on a tie.
    """
    return (violations, elapsed_sec, problem.input_position_of(stop_id), stop_id)


def build_seed(problem: RouteProblem) -> SeedResult:
    """Build the constraint-aware greedy seed of ``problem``, with its evaluation count.

    The route starts with ``problem.first_stop_id`` - the driver's decision, which optimization
    must never reorder (I3, D10) - and then appends the enabled stop that minimizes the resulting
    complete-route objective, breaking ties by ``input_position`` and then stop id.

    Returns a permutation of exactly the enabled stops, each exactly once. Deterministic: the same
    problem always produces the same order, and every candidate is scored at every step (there is
    no prefilter of any kind).
    """
    first_stop_id = problem.first_stop_id
    order: list[StopId] = [first_stop_id]
    remaining: list[StopId] = list(problem.remaining_stop_ids)
    # A stop no complete route could serve is refused with the authoritative error, before any
    # leg is priced. This is the seed's single such check: the per-candidate pricing below is the
    # same arithmetic on a problem that has already been established serviceable, so the inner loop
    # neither repeats an O(n) scan nor stands in for a route the engine would reject.
    require_complete_route(problem, remaining)
    table = problem._extended_travel_table()

    # The service state the chosen prefix leaves the driver in. It is carried forward, so a step
    # never re-prices the stops it has already committed to.
    previous = problem.index_of(first_stop_id)
    elapsed = problem._try_advance(problem.start_index, 0, previous, require_serviceable=False)[0]
    evaluations = 0

    # The candidate loop is the seed's hot loop: it prices one candidate per remaining stop per
    # step, so every lookup it performs is hoisted out of it here. The arithmetic is exactly
    # ``RouteProblem._route_from`` for the single-stop route ``(candidate,)`` - the complete route
    # from the driver's current position to the candidate and then to FINISH - spelled inline so
    # the ~n^2/2 candidate pricings do not pay for a function call and a dozen attribute reads
    # each. ``tests/engine/test_optimizer.py`` pins the seed's order against the U2 criterion.
    count = problem.stop_count
    sizes = count + 2
    finish_cell_base = count + 1
    index_of = problem.stop_index.get
    positions = [problem.input_positions[index] for index in range(count)]
    durations = problem.duration_by_stop
    finish_before = problem.window_end_policies
    bounds_rows = problem.window_bounds_rows
    bounds_row = problem.window_bounds_row
    cutoffs = problem.cutoff_instants
    cutoff_offsets = problem.cutoff_offsets
    cutoff_count = len(cutoffs)
    utc_cutoffs = problem.utc_cutoffs
    departure_time = problem.departure_time
    elapsed_of = problem.elapsed_of
    window_for = problem.window_for
    finish_before_end = WindowEndPolicy.SERVICE_FINISH_BEFORE_END

    while remaining:
        chosen_id: StopId | None = None
        chosen_position = 0
        chosen_violations = 0
        chosen_finish = 0
        chosen_position_in_input = 0
        for position, candidate in enumerate(remaining):
            evaluations += 1
            index = index_of(candidate)
            arrival = elapsed + table[previous * sizes + index]
            duration = durations[index]
            # The service date of the arrival is found from the prepared local midnights, and the
            # window bounds it selects are the ones :meth:`RouteProblem.window_for` returns. When
            # the prepared rows cannot answer, the authoritative resolver decides - and raises for
            # exactly the arrivals ``evaluate_order`` rejects.
            step = bisect.bisect_right(cutoffs, departure_time + timedelta(seconds=arrival)) - 1
            offset = cutoff_offsets[step] if step >= 0 else None
            if offset is not None:
                if offset + 1 >= cutoff_count or utc_cutoffs[offset + 1] is None:
                    offset = None
            window_row = None if offset is None else bounds_rows.get(offset)
            if window_row is None and offset is not None:
                window_row = bounds_row(offset)
            bounds = None if window_row is None else window_row[index]
            violations = 0

            if bounds is None:
                window = window_for(candidate, arrival)
                if window is None:
                    service_end = arrival + duration
                else:
                    opened = elapsed_of(window.open_at)
                    service_start = opened if opened > arrival else arrival
                    closed = elapsed_of(window.close_at)
                    if finish_before[index] is finish_before_end:
                        if service_start + duration > closed:
                            violations = 1
                    elif service_start > closed:
                        violations = 1
                    service_end = service_start + duration
            else:
                opened, closed = bounds
                service_start = opened if opened > arrival else arrival
                if finish_before[index] is finish_before_end:
                    if service_start + duration > closed:
                        violations = 1
                elif service_start > closed:
                    violations = 1
                service_end = service_start + duration

            finish_elapsed = service_end + table[index * sizes + finish_cell_base]
            if chosen_id is None:
                replace = True
                chosen_position_in_input = positions[index]
            elif violations < chosen_violations:
                replace = True
            elif violations > chosen_violations:
                replace = False
            elif finish_elapsed < chosen_finish:
                replace = True
            elif finish_elapsed > chosen_finish:
                replace = False
            else:
                # A tie on the route itself, which is the only place the deterministic tie-break -
                # the user's original ``input_position``, then the stop id - is consulted (v2
                # section 30, D33).
                position_in_input = positions[index]
                if position_in_input < chosen_position_in_input:
                    replace = True
                elif position_in_input > chosen_position_in_input:
                    replace = False
                else:
                    replace = candidate < chosen_id
                if replace:
                    chosen_position_in_input = position_in_input
            if replace:
                chosen_violations = violations
                chosen_finish = finish_elapsed
                chosen_id = candidate
                chosen_position = position

        assert chosen_id is not None
        index = index_of(chosen_id)
        elapsed = problem._try_advance(previous, elapsed, index, require_serviceable=False)[0]
        previous = index
        order.append(chosen_id)
        remaining.pop(chosen_position)

    return SeedResult(order=tuple(order), evaluations=evaluations)


def greedy_seed(problem: RouteProblem) -> tuple[StopId, ...]:
    """Build the greedy seed of ``problem`` (the order alone).

    See :func:`build_seed` for the same seed with its evaluation evidence.
    """
    return build_seed(problem).order
