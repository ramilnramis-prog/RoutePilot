"""Constraint-aware greedy seed (Stage 2 unit U2; v2 section 21, decisions D17/D22).

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
which is exactly the core scenario of the product (v2 section 1). Nothing is weighted, tuned or
approximated: the comparison is the complete-route objective itself (D17: nearest-neighbour is
never the final product algorithm).
"""

from __future__ import annotations

from core.engine.optimizer.route_problem import (
    FastEvaluation,
    OpenState,
    RouteProblem,
    fast_evaluate,
)
from core.model.ids import StopId

__all__ = ["greedy_seed", "seed_choice_key"]

#: The deterministic tie-break key of one greedy step: (violations, elapsed, position, id).
SeedChoiceKey = tuple[int, int, int, str]


def seed_choice_key(
    problem: RouteProblem, evaluation: FastEvaluation, stop_id: StopId
) -> SeedChoiceKey:
    """The deterministic ordering key of one candidate next stop.

    ``evaluation`` is the complete route that results from appending ``stop_id``, so waiting and
    the final leg to FINISH already influence the choice.
    """
    return (
        evaluation.violations,
        evaluation.finish_elapsed_sec,
        problem.input_position_of(stop_id),
        stop_id,
    )


def greedy_seed(problem: RouteProblem) -> tuple[StopId, ...]:
    """Build the constraint-aware greedy seed of ``problem``.

    The route starts with ``problem.first_stop_id`` - the driver's decision, which optimization
    must never reorder (I3, D10) - and then appends the enabled stop that minimizes the resulting
    complete-route objective, breaking ties by ``input_position`` and then by stop id.

    Returns a permutation of exactly the enabled stops, each exactly once. Deterministic: the same
    problem always produces the same order.
    """
    first_stop_id = problem.first_stop_id
    order: list[StopId] = [first_stop_id]
    remaining: list[StopId] = list(problem.remaining_stop_ids)

    state = OpenState(elapsed_sec=0, point=problem.departure_point)
    state = problem.advance(state, first_stop_id)

    while remaining:
        chosen_id: StopId | None = None
        chosen_state: OpenState | None = None
        chosen_key: SeedChoiceKey | None = None

        for candidate in remaining:
            candidate_state = problem.advance(state, candidate)
            appended = (*order, candidate)
            # The complete route from here: the final leg to FINISH is part of the objective
            # (v2 section 15). This repeats the same route arithmetic on every step, so a
            # candidate is compared as "the route I would actually drive", not as "the next leg".
            evaluation = fast_evaluate(problem, appended)
            key = seed_choice_key(problem, evaluation, candidate)
            if chosen_key is None or key < chosen_key:
                chosen_key = key
                chosen_id = candidate
                chosen_state = candidate_state

        assert chosen_id is not None and chosen_state is not None
        order.append(chosen_id)
        remaining.remove(chosen_id)
        state = chosen_state

    return tuple(order)
