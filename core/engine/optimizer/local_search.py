"""Deterministic local improvement (Stage 2 unit U2; v2 section 21, decisions D17/D22).

The seed gives a route; this module makes it better without ever making it worse. Four rules
carry the whole unit:

* **the driver's first stop stays first** (I3, D10). No move is even generated that would move
  position 0;
* **every enabled stop stays in the order exactly once** (v2 section 21, spec section 27.3/27.17)
  - moves are permutations, so nothing can be duplicated, dropped or replaced by a disabled stop;
* **acceptance is lexicographic**: fewer hard-window violations is always better, and among
  routes with the same number of violations the earlier finish wins. No accepted move may worsen
  the accepted objective or increase the violation count, so the result is monotone by
  construction;
* **hard infeasibility is never converted into a numeric penalty** (D13 amendment). Violations
  stay a count and the objective stays the measured elapsed time; there is no dominance factor
  anywhere in this module.

Two neighbourhoods are searched, in a fixed order: **2-opt** (reverse the segment between two
positions) and **Or-opt / relocate** (move a segment of one or two consecutive stops to another
position, preserving its internal order). Each pass takes the *best* improving move, so the
result does not depend on iteration order inside a pass; ties on the objective are broken by the
input position and then the stop ids of the resulting order.

The number of evaluations is bounded by ``max_evaluations``. The bound is a fixed count of
route evaluations, never a wall-clock deadline, so a slower machine produces the same route
(no wall-clock, no randomness - the optimized path stays deterministic).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from core.engine.optimizer.route_problem import (
    FastEvaluation,
    RouteProblem,
    fast_evaluate,
    fast_evaluation_key,
)
from core.model.ids import StopId
from core.validation.errors import InvalidOrderError, InvalidRoutePlanError

__all__ = [
    "LocalSearchResult",
    "RouteMove",
    "accepted",
    "candidate_moves",
    "improve",
]

#: Evaluations allowed per improvement pass by default. Each pass already scans every move in
#: both neighbourhoods, so a small number of passes is enough; the bound keeps the worst case
#: (a pathologically large plan) deterministic instead of open-ended.
DEFAULT_MAX_EVALUATIONS = 20_000

#: Improvement passes allowed by default.
DEFAULT_MAX_PASSES = 8


@dataclass(frozen=True)
class RouteMove:
    """One deterministic neighbourhood move of the route after position 0."""

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


@dataclass(frozen=True)
class LocalSearchResult:
    """The improved route and the evidence that improvement was monotone and bounded.

    ``accepted_moves`` is empty when the seed was already locally optimal in both neighbourhoods
    - which is a real, reportable outcome, not a failure.
    """

    order: tuple[StopId, ...]
    seed_objective: int
    final_objective: int
    accepted_moves: tuple[RouteMove, ...]
    evaluations: int
    seed_violations: int = 0
    final_violations: int = 0
    passes: int = 0

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
# the acceptance rule (shared with the seed and with the tests)
# --------------------------------------------------------------------------- #
def accepted(candidate: FastEvaluation, current: FastEvaluation) -> bool:
    """Whether ``candidate`` is accepted over ``current``: lexicographic, never a penalty.

    Fewer hard-window violations wins first; among equally violating routes the earlier finish
    wins. Nothing else can make a move acceptable, so a move can never trade a violation for
    seconds and can never worsen ``current``'s objective.
    """
    return fast_evaluation_key(candidate) < fast_evaluation_key(current)


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


def candidate_moves(count: int, *, max_segment: int = 2) -> Iterator[RouteMove]:
    """Every move of both neighbourhoods that keeps position 0 fixed, in a fixed order."""
    if count < 3:
        return
    for start in range(1, count - 1):
        for end in range(start + 1, count):
            yield RouteMove(kind="2-opt", start=start, end=end)
    for length in range(1, max_segment + 1):
        for start in range(1, count - length + 1):
            for anchor in range(1, count):
                if anchor != start and not (start < anchor < start + length):
                    yield RouteMove(kind="relocate", start=start, anchor=anchor, length=length)


# --------------------------------------------------------------------------- #
# the search
# --------------------------------------------------------------------------- #
def improve(
    problem: RouteProblem,
    order: Sequence[StopId],
    *,
    max_passes: int = DEFAULT_MAX_PASSES,
    max_evaluations: int = DEFAULT_MAX_EVALUATIONS,
) -> LocalSearchResult:
    """Improve ``order`` deterministically, never accepting a worse route.

    ``order`` must already start with the driver's selected first stop and contain every enabled
    stop exactly once; anything else is rejected instead of repaired (v2 section 21).

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

    current = fast_evaluate(problem, sequence)
    seed_objective = current.finish_elapsed_sec
    seed_violations = current.violations
    accepted_moves: list[RouteMove] = []
    evaluations = 0
    passes = 0

    while passes < max_passes and evaluations < max_evaluations:
        passes += 1
        best_move: RouteMove | None = None
        best_evaluation: FastEvaluation | None = None
        best_key: tuple[int, int, str, str] | None = None

        for move in candidate_moves(len(sequence)):
            if evaluations >= max_evaluations:
                break
            candidate = apply_move(sequence, move)
            evaluation = fast_evaluate(problem, candidate)
            evaluations += 1
            if not accepted(evaluation, current):
                continue
            key = _move_key(evaluation, candidate, problem)
            if best_key is None or key < best_key:
                best_key = key
                best_move = move
                best_evaluation = evaluation

        if best_move is None or best_evaluation is None:
            break
        sequence = best_evaluation.order
        current = best_evaluation
        accepted_moves.append(best_move)

    return LocalSearchResult(
        order=sequence,
        seed_objective=seed_objective,
        final_objective=current.finish_elapsed_sec,
        accepted_moves=tuple(accepted_moves),
        evaluations=evaluations,
        seed_violations=seed_violations,
        final_violations=current.violations,
        passes=passes,
    )


def _move_key(
    evaluation: FastEvaluation, order: tuple[StopId, ...], problem: RouteProblem
) -> tuple[int, int, str, str]:
    """A deterministic tie-break for equally good moves, independent of generation order.

    Ranked by (violations, elapsed seconds), then the resulting route's input positions, then its
    stop ids. Two moves that would produce the same route never compete, because the route itself
    is the tie-break.
    """
    positions = ".".join(str(problem.input_position_of(stop_id)) for stop_id in order)
    return (
        evaluation.violations,
        evaluation.finish_elapsed_sec,
        positions,
        ",".join(order),
    )
