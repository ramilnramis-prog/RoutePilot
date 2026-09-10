"""RoutePilot test suite.

Deterministic and fully offline: no network, no browser, no LLM, no paid map API (spec section 27).

On a machine without the ``tzdata`` package (for example an offline Windows box) the suite
explicitly activates a discovered system TZif tree through ``zoneinfo``'s standard search path
and says so. Production code never does this silently - the real fix stays
``python -m pip install tzdata`` (D12).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.time import tzdata  # noqa: E402  (import after the path bootstrap)

#: Set when the suite had to fall back to a system TZif tree; ``None`` when tzdata is available.
TZDATA_FALLBACK_PATH: Path | None = None


def _activate_offline_fallback() -> None:
    global TZDATA_FALLBACK_PATH
    report = tzdata.probe_tzdata()
    if report.is_available:
        return
    activated = tzdata.activate_system_tzif_fallback()
    if activated is not None:
        TZDATA_FALLBACK_PATH = activated
        print(
            "[tests] WARNING: no tzdata package available; using the system TZif tree at "
            f"{activated}. Install the real dependency with: python -m pip install tzdata",
            file=sys.stderr,
        )


_activate_offline_fallback()
