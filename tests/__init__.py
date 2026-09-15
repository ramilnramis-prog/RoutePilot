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

#: The application's **runtime data directory**: the gitignored ``var/`` tree that
#: ``python -m api.serve`` writes (``api.services.DEFAULT_DB_PATH`` = ``var/routepilot.db``, plus the
#: append-only run history the product documents as immutable audit). It belongs to the running
#: application, **not** to the test suite: the suite must never own it, delete it or clean anything
#: inside it that it did not create itself (D40).
RUNTIME_DATA_DIR = REPO_ROOT / "var"

#: The one directory the test suite owns: the ``tests`` marker directory directly under the runtime
#: data directory. It is the **only** location where the suite may create scratch databases and
#: scratch trees, and the **only** location the suite may remove again (D40). Defining it here once
#: means every suite-owned artifact lives under a single root, so one invariant covers them all.
SUITE_SCRATCH_ROOT = RUNTIME_DATA_DIR / "tests"

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
