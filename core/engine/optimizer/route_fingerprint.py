"""The committed route's own fingerprint (Stage 2 unit U2; v2 section 7, change set item 3).

`RoutePlan.inputs_fingerprint()` answers "is the *recommendation* still fresh?" and deliberately
excludes the driver's decision: accepting or rejecting a recommendation must not make the
recommendation look stale (v2 section 7, D4). The committed route needs the opposite property, so
it gets its own digest.

:func:`route_fingerprint` covers, deterministically:

* the plan's recommendation inputs - by including ``plan.inputs_fingerprint()`` itself, so a route
  fingerprint can never disagree with the recommendation fingerprint about departure time,
  service windows, the travel matrix identity or the timezone-data version;
* the **driver's decision**: ``selected_stop_id``, its ``selection_source`` and ``pinned``
  (v2 sections 4 and 5 - a selected first stop is committed and pinned);
* the **route order itself**, position by position: "start at S07 and then visit 03, 01, 02" and
  "start at S07 and then visit 01, 02, 03" are two different committed routes.

The result is a hex SHA-256 digest of a canonical JSON document, so it is stable across runs and
processes and independent of dict or set iteration order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from core.model.ids import StopId
from core.model.route_plan import RoutePlan
from core.validation.errors import InvalidRoutePlanError

__all__ = ["ROUTE_FINGERPRINT_VERSION", "route_fingerprint", "route_fingerprint_payload"]

#: Bumped only if the digest's *meaning* changes, so a stored fingerprint stays interpretable.
ROUTE_FINGERPRINT_VERSION = 1


def route_fingerprint(
    plan: RoutePlan,
    order: Sequence[StopId],
    *,
    matrix_fingerprint: str | None = None,
) -> str:
    """Deterministic digest of the committed route's inputs, selection and order.

    Args:
        plan: the plan whose recommendation inputs and driver decision are part of the route.
        order: the committed service order, ``START -> ... -> FINISH``. It must be exactly the
            enabled stops, each exactly once (validated here, so a fingerprint is never taken of
            an order no route could have).
        matrix_fingerprint: identity/version of the travel matrix actually used, when the caller
            knows it. ``None`` means "not supplied" and is recorded as ``null``, never invented.

    Raises:
        InvalidOrderError: ``order`` is not exactly the enabled stops, each exactly once.
        InvalidRoutePlanError: ``plan`` is not a plan, or ``order`` is empty (there is no
            committed route to fingerprint).
    """
    canonical = route_fingerprint_payload(
        plan, order, matrix_fingerprint=matrix_fingerprint
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def route_fingerprint_payload(
    plan: RoutePlan,
    order: Sequence[StopId],
    *,
    matrix_fingerprint: str | None = None,
) -> str:
    """The canonical JSON document the digest is taken over.

    Exposed so a reviewer can read exactly what a route fingerprint does and does not cover,
    instead of inferring it from a hex string.
    """
    if not isinstance(plan, RoutePlan):
        raise InvalidRoutePlanError("a route fingerprint needs a RoutePlan")
    sequence = tuple(order)
    if not sequence:
        raise InvalidRoutePlanError(
            "an empty order is not a committed route: there is nothing to fingerprint (v2 "
            "section 2)"
        )
    plan.validate_order(sequence)

    first_stop = plan.first_service_stop
    payload = {
        "version": ROUTE_FINGERPRINT_VERSION,
        # The recommendation inputs, reused rather than re-listed: the route fingerprint must
        # change whenever the recommendation fingerprint does (v2 section 7).
        "recommendation": plan.inputs_fingerprint(matrix_fingerprint=matrix_fingerprint),
        "matrix_fingerprint": matrix_fingerprint,
        "selected_first_stop": first_stop.selected_stop_id,
        "selection_source": (
            first_stop.selection_source.value if first_stop.selection_source is not None else None
        ),
        "pinned": first_stop.pinned,
        "first_stop_mode": first_stop.mode.value,
        # Order-sensitive on purpose: the committed route is a sequence, not a set.
        "order": list(sequence),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
