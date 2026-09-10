"""Identifier types.

``NewType`` keeps identifiers from being mixed up with each other and with free text while
staying plain strings at runtime (no runtime cost, no custom class to keep in sync).
"""

from __future__ import annotations

from typing import NewType

__all__ = ["PlanId", "RunId", "StopId"]

#: Identifier of a service stop.
StopId = NewType("StopId", str)

#: Identifier of a route plan.
PlanId = NewType("PlanId", str)

#: Identifier of a route optimization run.
RunId = NewType("RunId", str)
