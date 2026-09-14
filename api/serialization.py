"""Domain -> JSON-ready payloads for the RoutePilot HTTP API (Stage 4 U13).

This module is the **only** place that decides what an API payload looks like. It is pure: it
takes domain objects (and the :class:`~core.repositories.RoutePlanRepository` Protocol) and returns
plain Python data - ``dict``/``list``/``str``/``int``/``float``/``bool``/``None``. There is no
``json`` call, no HTTP status code and no transport type here, so a future FastAPI transport can
reuse these functions unchanged and the JSON encoding stays a transport concern.

Serialisation contracts (documented here, exercised by ``tests/api``)
====================================================================

============================  ===================================================================
domain value                  JSON contract
============================  ===================================================================
UTC instant (``Instant``)     ISO-8601 text in UTC with a trailing ``Z``, seconds precision
                              (``2026-09-11T01:00:00Z``). Always UTC: every absolute timestamp
                              inside RoutePilot is UTC (D2), and local text exists only where the
                              domain itself holds wall-clock values (see below).
duration (``DurationSec``)    integer **SECONDS** (never a float, never a ``"1h30m"`` string).
boolean                       a real JSON boolean. Storage uses ``0``/``1`` in the DDL; the API
                              does not, because a client must not have to guess whether ``0``
                              means ``false`` or "zero".
enum                          its string value (``"fixed"``, ``"pending"``, ``"SMART_ROUTE"``).
fingerprint                   lowercase hex text (64 characters for the SHA-256 fingerprints).
service window local time     local wall-clock ``HH:MM:SS`` text, **no** date and **no** offset:
                              the plan's IANA zone resolves it under strict DST validation (D3).
                              This mirrors the storage schema, so a round-tripped plan keeps the
                              exact wall-clock value the driver entered.
============================  ===================================================================

Two rules that are not negotiable, because they are how the product stays honest (D4/D11/D32/I5):

* a **recommendation is never plan state**. No plan payload carries ``recommended_stop_id``, and
  no payload is allowed to invent one. The recommendation is derived and recomputable and arrives
  with its own endpoint in U14; it is not persisted as plan state anywhere (D38 item 8).
* ``selected_stop_id`` is the **driver's** decision. It is ``None`` in the normal
  ``awaiting_first_stop_choice`` state, and its provenance (``selection_source``, ``pinned``) is
  carried next to it so the two can never be read as one thing.

Every payload is JSON-safe by construction: plain types only, no tuples, no enums, no datetimes.
Tests pin that property directly, because a stray enum would make the transport leak the domain's
representation.

Stage 4 U14 payloads (recommendation, route, run)
=================================================

U14 adds three payload families over the same contracts, with one more rule that is not negotiable
either:

* the **recommendation** payload is advisory and says so in its own fields: it carries
  ``advisory``, ``applied_decision``, ``as_plan_state`` and a ``note`` stating plainly that this is
  a recommendation and **not** an applied decision. It never writes to the plan, and the plan
  payload of the same plan is unchanged by reading it (D4/D11/D32, I5);
* the **route** payload is the committed route of the plan's **own** selected first stop
  (``solution.first_service_stop``), so it always states which driver decision produced it and
  never claims a route the driver did not choose (I3/I4);
* the **run** payload is immutable history: what one optimize/reoptimize execution showed, with
  both fingerprints, the tzdata version, the cost policy actually used, the order, the metrics with
  both baselines and the recorded recommendation/top-K/violations as the audit of that run. A run
  is never plan state, and its recorded recommendation never becomes a selection.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from core.model.cost_policy import RouteCostPolicy
from core.model.first_stop import (
    CandidateDiagnostic,
    CandidateMetrics,
    FirstStopCandidate,
    FirstStopIntent,
)
from core.model.optimization_run import OptimizationRun, OptimizationRunRecommendation
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.solution import RouteMetrics, StopTimeline
from core.model.value_objects import DataProvenance, Instant, PlaceRef
from core.repositories import RoutePlanRepository
from core.validation.errors import InvalidRoutePlanError

__all__ = [
    "API_VERSION",
    "ERROR_CODES",
    "PLAN_PAYLOAD_TYPE",
    "PLAN_SUMMARY_TYPE",
    "RECOMMENDATION_PAYLOAD_TYPE",
    "RECOMMENDATION_ADVISORY_NOTE",
    "ROUTE_PAYLOAD_TYPE",
    "RUN_PAYLOAD_TYPE",
    "SELECTION_PAYLOAD_TYPE",
    "SETTINGS_PAYLOAD_TYPE",
    "candidate_diagnostic_payload",
    "candidate_metrics_payload",
    "candidate_payload",
    "error_code",
    "error_document",
    "instant_text",
    "is_json_safe",
    "plan_document",
    "plan_provenance",
    "plan_summary_document",
    "plan_summary_payload",
    "plan_payload",
    "recommendation_document",
    "recommendation_payload",
    "recorded_recommendation_payload",
    "route_document",
    "route_metrics_payload",
    "route_payload",
    "run_document",
    "run_list_document",
    "run_payload",
    "selection_document",
    "selection_payload",
    "settings_payload",
    "stop_payload",
    "timeline_row_payload",
    "violation_payload",
]

#: Version of the payload contracts above. Bumped when a payload changes shape, so a client can
#: tell which contract it is talking to instead of guessing.
API_VERSION = "1"

PLAN_PAYLOAD_TYPE = "RoutePlan"
PLAN_SUMMARY_TYPE = "RoutePlanSummary"
SETTINGS_PAYLOAD_TYPE = "AppSetting"

#: U14 payload types: the advisory recommendation, the committed route of the current selection, one
#: immutable run row, and the driver's selection change.
RECOMMENDATION_PAYLOAD_TYPE = "FirstStopRecommendation"
ROUTE_PAYLOAD_TYPE = "CommittedRoute"
RUN_PAYLOAD_TYPE = "OptimizationRun"
SELECTION_PAYLOAD_TYPE = "FirstStopSelection"

#: The sentence every recommendation payload carries. It is the product principle of D4/D32 stated
#: as data, so no client can present a recommendation as an applied decision by accident.
RECOMMENDATION_ADVISORY_NOTE = (
    "This is a recommendation, not an applied decision: it was recomputed live for this request, it "
    "is NOT plan state, and the plan's first stop stays awaiting the driver's choice until the "
    "driver accepts it or chooses another stop (D4/D11/D32, I5)."
)

#: The documented error codes of this API: ``code -> (HTTP status, meaning)``.
#:
#: The HTTP status is repeated here (rather than only in ``api/http_server.py``) because a code and
#: its status are one contract; the transport table in ``api/http_server.py`` maps *exceptions* to
#: these codes and is the authoritative mapping for a raised error.
ERROR_CODES: dict[str, tuple[int, str]] = {
    "invalid_body": (400, "the request body is not a valid JSON object (unparsable, empty or not an object)"),
    "unknown_path": (404, "the requested path is not part of this API"),
    "unknown_plan": (404, "no stored plan has that id"),
    "unknown_stop": (404, "the plan has no stop with that id"),
    "unknown_run": (404, "no stored optimization run has that id"),
    "unknown_setting": (404, "no value is stored for that settings key"),
    "method_not_allowed": (405, "the path exists but does not accept this HTTP method"),
    "no_first_stop_selected": (
        409,
        "a committed route was requested while the plan is awaiting the driver's first-stop choice "
        "(D9/I4); the honest answer is no route, never an invented one",
    ),
    "plan_busy": (
        409,
        "the plan's single-flight computation lock did not become free inside the documented bound; "
        "this API answers synchronously and has no background job queue, so the request is refused "
        "instead of returning a partial or fabricated result",
    ),
    "illegal_state": (409, "the requested change is not legal in the plan's current state"),
    "invalid_input": (422, "the request was understood but violates a domain rule"),
    "unsupported_capability": (
        501,
        "the capability is declared in the domain but not implemented; it is never faked",
    ),
    "timezone_data_unavailable": (
        503,
        "no IANA time zone database is reachable, so local wall-clock times cannot be resolved",
    ),
    "storage_error": (500, "stored state is unreadable or the database refused the operation"),
    "internal_error": (500, "the request failed for a reason this API does not classify"),
}


def error_code(code: str) -> str:
    """Return ``code`` after checking it is one of the documented :data:`ERROR_CODES`."""
    if code not in ERROR_CODES:
        raise KeyError(f"undocumented API error code {code!r}; add it to ERROR_CODES first")
    return code


def error_document(code: str, error_type: str, message: str) -> dict[str, Any]:
    """The single error envelope of this API: ``{"error": {"code", "type", "message"}}``.

    ``code`` is the machine-readable documented code, ``type`` the domain/exception class name so
    a developer can locate the rule that refused the request, and ``message`` the human-readable
    explanation (RoutePilot error messages are written to be shown as-is, D26).
    """
    return {
        "error": {
            "code": error_code(code),
            "type": error_type,
            "message": message,
        }
    }


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def _instant(value: Instant) -> str:
    """UTC ISO-8601 text with a trailing ``Z`` and second precision (D2, storage convention)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant_text(value: Instant) -> str:
    """One domain instant in the API's canonical text form (UTC ISO-8601 with a trailing ``Z``).

    Public because the transport needs the same text for an instant it carries **outside** a payload
    (the recommendation envelope's ``computed_at``), and a second formatting rule in the transport
    is exactly how two representations of one instant start to disagree.
    """
    return _instant(value)


def _utc_iso(value: str) -> str:
    """A stored ``...Z`` timestamp, re-emitted in the canonical contract form."""
    return value[:-1] + "Z" if value.endswith("Z") else value


def _duration_seconds(value: int | None) -> int | None:
    """Durations are integer SECONDS in this API, never floats and never formatted text."""
    return None if value is None else int(value)


def _enum(value: Any) -> Any:
    """Enums serialise as their string value; everything else is passed through."""
    return value.value if hasattr(value, "value") else value


def _place(place: PlaceRef) -> dict[str, Any]:
    return {
        "label": place.label,
        "latitude": float(place.point.latitude),
        "longitude": float(place.point.longitude),
    }


def _window(window: ServiceWindow, plan_default: WindowEndPolicy) -> dict[str, Any]:
    """One service window, in the plan's local wall clock.

    ``window_end_policy`` is the stop's **own** override and is ``null`` when the stop inherits the
    plan default (D29); ``effective_window_end_policy`` states which policy actually applies to
    this stop, so a client never has to re-implement the inheritance rule. Both are ``null`` for a
    non-fixed window, which has no end to interpret.
    """
    fixed = window.is_fixed
    return {
        "window_kind": window.window_kind.value,
        "start_local": window.start_local.strftime("%H:%M:%S") if fixed else None,
        "end_local": window.end_local.strftime("%H:%M:%S") if fixed else None,
        "window_end_policy": (
            window.window_end_policy.value
            if fixed and window.window_end_policy is not None
            else None
        ),
        "effective_window_end_policy": (
            window.effective_end_policy(plan_default).value if fixed else None
        ),
        "description": window.describe(),
    }


def stop_payload(stop: RouteStop, plan_default: WindowEndPolicy) -> dict[str, Any]:
    """One stop: spec section 25 fields plus the per-stop window policy and status flags."""
    return {
        "id": stop.id,
        "input_position": stop.input_position,
        "raw_address": stop.raw_address,
        "normalized_address": stop.normalized_address,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
        "geocode_status": stop.geocode_status.value,
        "service_status": stop.service_status.value,
        "enabled": bool(stop.enabled),
        "priority": stop.priority,
        "service_duration_sec": _duration_seconds(stop.service_duration),
        "service_window": _window(stop.service_window, plan_default),
        "notes": stop.notes,
    }


def cost_policy_payload(policy: RouteCostPolicy) -> dict[str, Any]:
    """The objective that produced a plan's figures, with the capability table (D16).

    Weights are reported as declared; the component statuses come from the domain's own
    declarations, so this payload cannot claim a component is scored when it is not.
    """
    return {
        "name": policy.name,
        "provisional": bool(policy.provisional),
        "weights": {
            component.value: float(weight)
            for component, weight in sorted(policy.weights.items(), key=lambda item: item[0].value)
        },
        "components": [
            {
                "component": declaration.component.value,
                "status": declaration.status.value,
                "requires": declaration.requires,
                "note": declaration.note,
            }
            for declaration in policy.declarations.values()
        ],
        "unimplemented_components": [
            component.value for component in policy.unimplemented_components()
        ],
        "notes": policy.notes,
    }


def first_stop_payload(plan: RoutePlan) -> dict[str, Any]:
    """The plan's first-stop **state**: the driver's decision, never the engine's recommendation.

    ``mode``/``selection_source``/``pinned``/``selected_stop_id`` come from
    :class:`~core.model.first_stop.FirstStopIntent`; ``state`` is the plan-level derived state
    (``awaiting_first_stop_choice`` before the driver decides, which is normal, not an error - I4).
    There is deliberately no recommendation field: a recommendation is derived and recomputable and
    is never plan state (D4/D11/D32/I5).
    """
    intent: FirstStopIntent = plan.first_service_stop
    state = plan.first_stop_state
    return {
        "mode": intent.mode.value,
        "selected_stop_id": intent.selected_stop_id,
        "selection_source": (
            intent.selection_source.value if intent.selection_source is not None else None
        ),
        "pinned": bool(intent.pinned),
        "state": state.value,
        "description": intent.describe(),
    }


def order_overrides_payload(plan: RoutePlan) -> dict[str, Any]:
    """The user's order constraints: implemented kinds only, with the kind named (D21)."""
    return {
        "constraints": [
            {
                "kind": constraint.kind.value,
                "stop_id": constraint.stop_id,
                "position": constraint.position,
            }
            for constraint in plan.order_overrides.constraints
        ]
    }


def plan_payload(plan: RoutePlan, data_provenance: DataProvenance | str) -> dict[str, Any]:
    """A whole plan, including its stops, its first-stop state and its provenance.

    ``data_provenance`` is the provenance the plan was **stored** with (the domain does not carry
    it), so it is passed in by the caller that read the plan: ``DEMO_SYNTHETIC`` must never be
    presented as real routing data (spec section 33, D15/D23).
    """
    return {
        "id": plan.id,
        "name": None,  # the domain models no plan name, so none is invented (U10 deliverable 4)
        "timezone": plan.timezone,
        "route_mode": plan.route_mode.value,
        "window_end_policy": plan.window_end_policy.value,
        "departure": _place(plan.departure),
        "finish": _place(plan.finish),
        "departure_time": _instant(plan.departure_time),
        "default_service_duration_sec": _duration_seconds(plan.default_service_duration),
        "data_provenance": _enum(data_provenance),
        "inputs_fingerprint": plan.inputs_fingerprint(),
        "cost_policy": cost_policy_payload(plan.cost_policy),
        "first_stop": first_stop_payload(plan),
        "order_overrides": order_overrides_payload(plan),
        "stops": [stop_payload(stop, plan.window_end_policy) for stop in plan.stops],
        "counts": {
            "stops": len(plan.stops),
            "enabled_stops": len(plan.active_stops()),
            "disabled_stops": len(plan.disabled_stops()),
        },
    }


def plan_document(plan: RoutePlan, data_provenance: DataProvenance | str) -> dict[str, Any]:
    """The API envelope for one plan payload."""
    return {
        "type": PLAN_PAYLOAD_TYPE,
        "api_version": API_VERSION,
        "data": plan_payload(plan, data_provenance),
    }


def plan_summary_payload(plan: RoutePlan, data_provenance: DataProvenance | str) -> dict[str, Any]:
    """A list entry: enough to render a plan list without shipping every stop."""
    return {
        "id": plan.id,
        "timezone": plan.timezone,
        "route_mode": plan.route_mode.value,
        "departure_time": _instant(plan.departure_time),
        "departure_label": plan.departure.label,
        "finish_label": plan.finish.label,
        "data_provenance": _enum(data_provenance),
        "first_stop": first_stop_payload(plan),
        "counts": {
            "stops": len(plan.stops),
            "enabled_stops": len(plan.active_stops()),
            "disabled_stops": len(plan.disabled_stops()),
        },
    }


def plan_summary_document(
    plan: RoutePlan, data_provenance: DataProvenance | str
) -> dict[str, Any]:
    """The API envelope for one plan-summary payload."""
    return {
        "type": PLAN_SUMMARY_TYPE,
        "api_version": API_VERSION,
        "data": plan_summary_payload(plan, data_provenance),
    }


def settings_payload(key: str, value: Any, *, configured: bool) -> dict[str, Any]:
    """One settings entry.

    ``configured`` is ``False`` exactly when the key holds no stored value. The value is reported
    as stored (whole JSON values are the settings store's contract) and no default is invented
    here, because a default would be a product decision taken in a transport.
    """
    return {
        "key": key,
        "value": value,
        "configured": bool(configured),
    }


def settings_document(key: str, value: Any, *, configured: bool) -> dict[str, Any]:
    """The API envelope for one settings payload."""
    return {
        "type": SETTINGS_PAYLOAD_TYPE,
        "api_version": API_VERSION,
        "data": settings_payload(key, value, configured=configured),
    }


def plan_provenance(repository: RoutePlanRepository) -> DataProvenance:
    """The provenance a plan repository writes and accepts.

    The domain :class:`~core.model.route_plan.RoutePlan` does not carry provenance (the schema
    column is ``NOT NULL`` and the repository is constructed with it), so the API asks the
    repository instead of assuming a value. An implementation that does not expose it is an error
    rather than a licence to guess: guessing would let synthetic data be reported as real routing.
    """
    provenance = getattr(repository, "data_provenance", None)
    if provenance is None:
        raise InvalidRoutePlanError(
            "the configured plan repository does not expose data_provenance, so this API cannot "
            "state which data the plan came from; refusing to guess (D15/D23)"
        )
    return provenance if isinstance(provenance, DataProvenance) else DataProvenance(provenance)


def is_json_safe(value: Any) -> bool:
    """Whether ``value`` consists only of types the JSON contract allows.

    Used by the test suite to pin the contract in one place: no enum, no ``datetime``, no tuple,
    no ``set`` may appear in a payload, so the transport can never leak a domain representation.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and is_json_safe(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return all(is_json_safe(item) for item in value)
    return False


# --------------------------------------------------------------------------- #
# U14: the engine-facing payloads (recommendation, route, run)
# --------------------------------------------------------------------------- #
def route_metrics_payload(metrics: RouteMetrics) -> dict[str, Any]:
    """One complete route's measured metrics (v2 sections 12/15).

    Every duration is integer **seconds**; ``finish_arrival`` is a UTC instant with a trailing
    ``Z``. ``duration_sec`` is the complete elapsed route duration - driving + waiting + service,
    the FINISH leg included - and a missed hard window is never folded into it: it is an explicit
    violation and ``feasible`` is ``False`` (D13 amendment). ``baseline_kind`` names which baseline
    a set of numbers describes (``user_supplied`` / ``algorithm_greedy``) and is ``null`` for the
    committed route, exactly as the domain carries it - an internal algorithm reference is never
    presented as the user's BEFORE route (D22).
    """
    return {
        "distance_m": float(metrics.distance_m),
        "duration_sec": int(metrics.duration_sec),
        "travel_sec": int(metrics.travel_sec),
        "waiting_sec": int(metrics.waiting_sec),
        "service_sec": int(metrics.service_sec),
        "finish_arrival": _instant(metrics.finish_arrival),
        "feasible": bool(metrics.feasible),
        "baseline_kind": metrics.baseline_kind.value if metrics.baseline_kind is not None else None,
    }


def candidate_metrics_payload(metrics: CandidateMetrics) -> dict[str, Any]:
    """The measured objective components of one candidate's complete route (v2 section 16)."""
    return {
        "travel_sec": int(metrics.travel_sec),
        "waiting_sec": int(metrics.waiting_sec),
        "distance_m": float(metrics.distance_m),
    }


def candidate_payload(
    candidate: FirstStopCandidate, *, rank: int | None = None
) -> dict[str, Any]:
    """One first-stop candidate: its first-leg metrics and its complete-route metrics (v2 section 12).

    The two groups are kept apart on purpose. ``first_leg`` is what the driver sees for the
    candidate itself (travel, ETA, waiting, service start, that stop's own lateness) and is
    **reported**, never the ranking criterion; ``complete_route`` is the whole route with the
    FINISH leg included, and it is what decides feasibility and rank (v2 sections 12/14/15, D32).
    ``rank`` is the 1-based position among fully feasible candidates and is ``null`` for a rejected
    candidate, so a rejected candidate can never look ranked (v2 section 14).
    """
    return {
        "stop_id": candidate.stop_id,
        "rank": rank,
        "feasible": bool(candidate.feasible),
        "first_leg": {
            "travel_sec": int(candidate.travel_time),
            "estimated_arrival": _instant(candidate.estimated_arrival),
            "service_window_start": (
                None
                if candidate.service_window_start is None
                else _instant(candidate.service_window_start)
            ),
            "waiting_sec": int(candidate.waiting_time),
            "estimated_service_start": (
                None
                if candidate.estimated_service_start is None
                else _instant(candidate.estimated_service_start)
            ),
            "lateness_sec": int(candidate.lateness),
        },
        "complete_route": {
            "duration_sec": int(candidate.estimated_complete_route_duration),
            "travel_sec": int(candidate.complete_travel_time),
            "waiting_sec": int(candidate.complete_waiting_time),
            "service_sec": int(candidate.total_service_time),
            "finish_arrival": (
                None if candidate.estimated_finish is None else _instant(candidate.estimated_finish)
            ),
            "max_lateness_sec": int(candidate.max_lateness),
            "violating_stop_ids": [str(stop_id) for stop_id in candidate.violating_stop_ids],
        },
        "objective": {
            "score": None if candidate.score is None else float(candidate.score),
            "breakdown": [[str(key), float(value)] for key, value in candidate.explanation],
            "metrics": (
                None
                if candidate.metrics is None
                else candidate_metrics_payload(candidate.metrics)
            ),
        },
    }


def candidate_diagnostic_payload(diagnostic: CandidateDiagnostic) -> dict[str, Any]:
    """One rejection reason: which **violating** stop, and which **candidate** it was rejected for.

    The two stop ids are different questions (D9): ``stop_id`` is the stop whose hard window the
    complete route cannot meet, and ``candidate_stop_id`` is the candidate first stop whose complete
    route could not serve it. ``code`` is the machine-readable
    :class:`~core.model.solution.ViolationKind` value.
    """
    return {
        "stop_id": diagnostic.stop_id,
        "candidate_stop_id": diagnostic.candidate_stop_id,
        "code": diagnostic.code,
        "violation_kind": (
            diagnostic.violation_kind.value
            if diagnostic.violation_kind is not None
            else None
        ),
        "message": diagnostic.message,
        "reason": diagnostic.reason,
    }


def selection_payload(plan: RoutePlan) -> dict[str, Any]:
    """The driver's first-stop **decision** after a selection change (never a recommendation).

    ``changed`` states whether this request changed the stored decision, and ``note`` says what the
    decision now is. The block carries the decision only - there is no recommendation field here,
    because a recommendation is derived and recomputable and is never plan state (D4/D11/D32/I5).
    """
    intent: FirstStopIntent = plan.first_service_stop
    selection = first_stop_payload(plan)
    if intent.has_selection:
        note = (
            f"the driver's first stop is {intent.selected_stop_id} "
            f"({intent.selection_source.value}, pinned) (D5/D6)"
        )
    else:
        note = (
            "nothing is selected: the plan is awaiting_first_stop_choice with a null selected stop "
            "and a null source (D8/D9)"
        )
    return {
        "plan_id": plan.id,
        "first_stop": selection,
        "state": selection["state"],
        "mode": selection["mode"],
        "selected_stop_id": selection["selected_stop_id"],
        "selection_source": selection["selection_source"],
        "pinned": selection["pinned"],
        "note": note,
    }


def recommendation_payload(
    report: Any,
    *,
    computation_seconds: int | None = None,
    ranked_limit: int | None = None,
) -> dict[str, Any]:
    """The exhaustive first-stop recommendation of one plan (v2 sections 12-14, 20; D32).

    ``report`` is the engine's own :class:`~core.engine.first_stop.evaluation.
    FirstStopEvaluationReport`: every ranked candidate is a complete route, and the rejected
    candidates are kept with the stop ids that violate and the reason (v2 section 14). A
    ``no_fully_feasible_route`` outcome is a valid answer with its diagnostics and carries no
    recommended stop - never a fabricated winner.

    ``ranked_limit`` truncates **only** the ranked list in this payload (a top-K view for the UI,
    v2 section 13); ``counts.ranked`` always states how many candidates were ranked, so a truncated
    list can never be mistaken for the whole ranking, and ``counts.candidates_evaluated`` always
    equals ``ranked + rejected`` (v2 section 20 forbids an unreported prefilter). ``rejected`` is
    never truncated.
    """
    ranked = tuple(report.ranked)
    shown = ranked if ranked_limit is None else ranked[:ranked_limit]
    counts = {
        "candidates_evaluated": int(report.candidates_evaluated),
        "ranked": len(ranked),
        "rejected": len(report.rejected),
        "optimizer_runs": int(report.optimizer_runs),
        "ranked_returned": len(shown),
    }
    return {
        "plan_id": report.plan_id,
        "status": report.status.value,
        "recommended_stop_id": report.recommended_stop_id,
        "advisory": True,
        "applied_decision": False,
        "as_plan_state": False,
        "note": RECOMMENDATION_ADVISORY_NOTE,
        "policy": {
            "name": report.policy_name,
            "provisional": bool(report.policy_is_provisional),
            "window_end_policy": report.window_end_policy.value,
        },
        "fingerprints": {
            "inputs_fingerprint": report.inputs_fingerprint,
        },
        "counts": counts,
        "computation_seconds": (
            None if computation_seconds is None else int(computation_seconds)
        ),
        "disabled_stop_ids": [str(stop_id) for stop_id in report.disabled_stop_ids],
        "ranked": [
            candidate_payload(candidate, rank=position)
            for position, candidate in enumerate(shown, start=1)
        ],
        "rejected": [
            candidate_payload(candidate, rank=None) for candidate in report.rejected
        ],
        "diagnostics": [
            candidate_diagnostic_payload(diagnostic) for diagnostic in report.diagnostics
        ],
    }


def recommendation_document(
    report: Any,
    *,
    computed_at: str | None = None,
    computation_seconds: int | None = None,
    ranked_limit: int | None = None,
) -> dict[str, Any]:
    """The API envelope for one recommendation payload.

    ``computed_at`` is the plan's own deterministic ``resolved_at`` (the demo never reads a wall
    clock); it is passed in as already-serialised text by the caller that owns the domain object,
    so this module stays free of transport decisions.
    """
    return {
        "type": RECOMMENDATION_PAYLOAD_TYPE,
        "api_version": API_VERSION,
        "live_recompute": True,
        "computed_at": computed_at,
        "data": recommendation_payload(
            report, computation_seconds=computation_seconds, ranked_limit=ranked_limit
        ),
    }


def timeline_row_payload(timeline: StopTimeline) -> dict[str, Any]:
    """One stop's timeline row (spec section 7, D29).

    All four instants are UTC with a trailing ``Z``. ``service_window_start``/``_end`` are the
    resolved window instants (``null`` when the stop has no fixed window) and
    ``window_end_policy`` states which policy the reported ``lateness_sec`` was measured under, so
    "late" always means the same thing (D29). ``flags`` are the non-fatal facts of
    :class:`~core.model.solution.TimelineFlag` - an unknown window is shown, never silently treated
    as open.
    """
    return {
        "stop_id": timeline.stop_id,
        "departure_from_previous": _instant(timeline.departure_from_previous),
        "travel_sec": int(timeline.travel_time),
        "estimated_arrival": _instant(timeline.estimated_arrival),
        "window_kind": timeline.window_kind.value,
        "service_window_start": (
            None if timeline.service_window_start is None else _instant(timeline.service_window_start)
        ),
        "service_window_end": (
            None if timeline.service_window_end is None else _instant(timeline.service_window_end)
        ),
        "window_end_policy": (
            timeline.window_end_policy.value if timeline.window_end_policy is not None else None
        ),
        "waiting_sec": int(timeline.waiting_time),
        "service_start": _instant(timeline.service_start),
        "service_duration_sec": int(timeline.service_duration),
        "estimated_departure": _instant(timeline.estimated_departure),
        "lateness_sec": int(timeline.lateness),
        "finish_overtime_sec": int(timeline.finish_overtime),
        "feasibility": timeline.feasibility.value,
        "flags": [flag.value for flag in timeline.flags],
    }


def violation_payload(violation: Any) -> dict[str, Any]:
    """One explicit infeasibility of a committed route (D13 amendment), never a hidden penalty."""
    return {
        "stop_id": violation.stop_id,
        "kind": violation.kind.value,
        "message": violation.message,
        "service_start": (
            None if violation.service_start is None else _instant(violation.service_start)
        ),
        "service_window_end": (
            None if violation.service_window_end is None else _instant(violation.service_window_end)
        ),
    }


def route_payload(solution: Any, *, route_fingerprint: str) -> dict[str, Any]:
    """The committed complete route of the plan's current selection (v2 sections 12/15, D22).

    ``solution`` is the engine's own :class:`~core.model.solution.RouteSolution`: the order, one
    timeline row per stop (arrival/ETA, waiting, service start, service duration, departure, the
    local window and the lateness), the metrics of the committed route **and** both baselines
    (``user_supplied`` = the driver's BEFORE order, ``algorithm_greedy`` = the labelled internal
    reference that is never shown as BEFORE), and the explicit violations. Every number is the
    engine's; the transport computes none of them.

    ``selection`` restates which driver decision produced this route, so a route can never be read
    as if the engine had chosen the first stop itself (I3/I4, D32).

    **No matrix fingerprint is exposed.** ``fingerprints`` carries exactly the two fingerprints the
    engine produces: ``inputs_fingerprint`` (the recommendation fingerprint) and
    ``route_fingerprint`` (the committed route's own digest). ``core`` offers no identity helper for
    the configured travel matrix - ``route_fingerprint`` and ``RoutePlan.inputs_fingerprint``
    *accept* a caller-supplied ``matrix_fingerprint`` but compute none - and inventing a digest here
    would be a business formula in the transport, so the field is absent rather than permanently
    ``null``.
    """
    intent: FirstStopIntent = solution.first_service_stop
    metrics = {
        "after": route_metrics_payload(solution.metrics),
        "user_baseline": (
            None
            if solution.user_baseline is None
            else route_metrics_payload(solution.user_baseline)
        ),
        "algorithm_baseline": (
            None
            if solution.algorithm_baseline is None
            else route_metrics_payload(solution.algorithm_baseline)
        ),
        "saved_distance_m": (
            None if solution.saved_distance_m is None else float(solution.saved_distance_m)
        ),
        "saved_duration_sec": (
            None if solution.saved_duration_sec is None else int(solution.saved_duration_sec)
        ),
    }
    return {
        # ``plan_id`` is supplied by :func:`route_document`, which is given the plan it came from:
        # a ``RouteSolution`` does not carry a plan id, and inventing one here would be a guess.
        "order": [str(stop_id) for stop_id in solution.order],
        "selection": {
            "mode": intent.mode.value,
            "selected_stop_id": intent.selected_stop_id,
            "selection_source": (
                intent.selection_source.value if intent.selection_source is not None else None
            ),
            "pinned": bool(intent.pinned),
        },
        "status": solution.status.value,
        "timeline": [timeline_row_payload(row) for row in solution.timelines],
        "metrics": metrics,
        "violations": [violation_payload(violation) for violation in solution.violations],
        "fingerprints": {
            "inputs_fingerprint": solution.inputs_fingerprint,
            "route_fingerprint": route_fingerprint,
        },
        "tzdata_version": solution.tzdata_version,
        "provenance": solution.provenance.value,
    }


def route_document(
    solution: Any,
    *,
    plan_id: str,
    route_fingerprint: str,
    computation_seconds: int | None = None,
    live_recompute: bool = True,
) -> dict[str, Any]:
    """The API envelope for one committed-route payload."""
    data = route_payload(solution, route_fingerprint=route_fingerprint)
    data["plan_id"] = plan_id
    data["computation_seconds"] = (
        None if computation_seconds is None else int(computation_seconds)
    )
    return {
        "type": ROUTE_PAYLOAD_TYPE,
        "api_version": API_VERSION,
        "live_recompute": bool(live_recompute),
        "data": data,
    }


def recorded_recommendation_payload(
    recommendation: OptimizationRunRecommendation,
) -> dict[str, Any]:
    """What a run recorded as the recommendation it showed - **history**, never plan state.

    The ranked candidate **ids** are the run's own audit record; the candidates' numbers live in the
    run's ``top_k``. ``ranked_stop_ids`` is empty exactly when nothing was available.
    """
    return {
        "status": recommendation.status.value,
        "recommended_stop_id": recommendation.recommended_stop_id,
        "ranked_stop_ids": [str(stop_id) for stop_id in recommendation.ranked_stop_ids],
        "resolved_at": (
            None if recommendation.resolved_at is None else _instant(recommendation.resolved_at)
        ),
        "inputs_fingerprint": recommendation.inputs_fingerprint,
        "diagnostics": [
            candidate_diagnostic_payload(diagnostic) for diagnostic in recommendation.diagnostics
        ],
        "as_plan_state": False,
    }


def run_payload(run: OptimizationRun) -> dict[str, Any]:
    """One immutable optimization-run row: the audit of a single execution (U11/D38).

    It carries the identity and kind of the run, the algorithm and its version, **both**
    fingerprints (``inputs_fingerprint`` and ``route_fingerprint`` - and no matrix fingerprint, since
    the API exposes none; see :func:`route_payload`), the tzdata version actually in use, the cost
    policy actually used, the stored status, ``created_at_utc``, the metrics with both baselines, the
    order, the explicit violations and the recorded recommendation/top-K/violations payloads.
    ``top_k`` is the head of the recorded ranking and is ``null`` when the run recorded no candidate
    detail - "nothing stored" and "no candidate existed" are different facts (D9).
    """
    run_metrics = run.metrics
    metrics = {
        "after": route_metrics_payload(run_metrics.after),
        "user_baseline": route_metrics_payload(run_metrics.user_baseline),
        "algorithm_baseline": route_metrics_payload(run_metrics.algorithm_baseline),
        "saved_distance_m": float(run_metrics.saved_distance_m),
        "saved_duration_sec": int(run_metrics.saved_duration_sec),
    }
    top_k = run.top_k
    return {
        "id": run.id,
        "plan_id": run.plan_id,
        "run_kind": run.run_kind.value,
        "status": run.status.value,
        "algorithm": run.algorithm,
        "algorithm_version": run.algorithm_version,
        "fingerprints": {
            "inputs_fingerprint": run.inputs_fingerprint,
            "route_fingerprint": run.route_fingerprint,
        },
        "tzdata_version": run.tzdata_version,
        "cost_policy": cost_policy_payload(run.cost_policy),
        "data_provenance": run.data_provenance.value,
        "created_at": _instant(run.created_at_utc),
        "order": [str(stop_id) for stop_id in run.order],
        "has_committed_route": bool(run.has_committed_route),
        "metrics": metrics,
        "violations": [violation_payload(violation) for violation in run.violations],
        "recommendation": recorded_recommendation_payload(run.recommendation),
        "top_k": (
            None
            if top_k is None
            else [
                candidate_payload(candidate, rank=position)
                for position, candidate in enumerate(top_k, start=1)
            ]
        ),
    }


def run_document(run: OptimizationRun, *, computation: dict[str, Any] | None = None) -> dict[str, Any]:
    """The API envelope for one run payload.

    ``computation`` carries the measured latency evidence of the recalculation that produced this
    row (``computation_seconds`` and the two engine phase durations). It is *about* the request, not
    about the stored run: it is deliberately **not** part of ``data``, so the run payload stays the
    faithful representation of the immutable row and nothing measured here is ever mistaken for
    stored history.
    """
    return {
        "type": RUN_PAYLOAD_TYPE,
        "api_version": API_VERSION,
        "computation": computation,
        "data": run_payload(run),
    }


def run_list_document(
    plan_id: str, runs: tuple[OptimizationRun, ...]
) -> dict[str, Any]:
    """The API envelope for a plan's run history, oldest first (the repository's own order)."""
    return {
        "type": "OptimizationRunList",
        "api_version": API_VERSION,
        "plan_id": plan_id,
        "count": len(runs),
        "read_only": True,
        "note": (
            "append-only history: only POST /api/plans/{id}/optimize appends a run, and a GET never "
            "does (owner decision 5)"
        ),
        "data": [run_payload(run) for run in runs],
    }


def selection_document(plan: RoutePlan) -> dict[str, Any]:
    """The API envelope for one selection-change payload."""
    return {
        "type": SELECTION_PAYLOAD_TYPE,
        "api_version": API_VERSION,
        "data": selection_payload(plan),
    }

