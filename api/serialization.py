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
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from core.model.cost_policy import RouteCostPolicy
from core.model.first_stop import FirstStopIntent
from core.model.route_plan import RoutePlan
from core.model.route_stop import RouteStop
from core.model.service_window import ServiceWindow, WindowEndPolicy
from core.model.value_objects import DataProvenance, Instant, PlaceRef
from core.repositories import RoutePlanRepository
from core.validation.errors import InvalidRoutePlanError

__all__ = [
    "API_VERSION",
    "ERROR_CODES",
    "PLAN_PAYLOAD_TYPE",
    "PLAN_SUMMARY_TYPE",
    "SETTINGS_PAYLOAD_TYPE",
    "error_code",
    "error_document",
    "is_json_safe",
    "plan_document",
    "plan_provenance",
    "plan_summary_document",
    "plan_summary_payload",
    "plan_payload",
    "settings_payload",
    "stop_payload",
]

#: Version of the payload contracts above. Bumped when a payload changes shape, so a client can
#: tell which contract it is talking to instead of guessing.
API_VERSION = "1"

PLAN_PAYLOAD_TYPE = "RoutePlan"
PLAN_SUMMARY_TYPE = "RoutePlanSummary"
SETTINGS_PAYLOAD_TYPE = "AppSetting"

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
    "unknown_setting": (404, "no value is stored for that settings key"),
    "method_not_allowed": (405, "the path exists but does not accept this HTTP method"),
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
