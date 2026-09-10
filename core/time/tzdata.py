"""IANA time zone database discovery and reporting (decisions D2 and D12).

RoutePilot uses ``zoneinfo`` + the ``tzdata`` package (spec section 20) and never builds its own
time zone or DST tables. This module answers one question: *is an IANA database reachable, where
did it come from, and which version is it?* The version matters because it participates in
``inputs_fingerprint`` (D2) - a rule change between tzdata releases must be able to invalidate a
cached recommendation.

Nothing here activates a fallback silently. :func:`activate_system_tzif_fallback` exists for
explicit development/test use (an offline machine with a compiled TZif tree, e.g. the zoneinfo
shipped with Git for Windows) and is never called from production code paths.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, reset_tzpath

import zoneinfo

from core.validation.errors import (
    TZDATA_INSTALL_COMMAND,
    InvalidTimezoneNameError,
    TimezoneDataMissingError,
    UnknownTimezoneError,
)

__all__ = [
    "TZDataReport",
    "TZDataStatus",
    "activate_system_tzif_fallback",
    "detect_timezone_source",
    "load_timezone",
    "probe_tzdata",
    "system_tzif_candidates",
    "tzdata_status",
    "tzdata_version",
    "validate_timezone_name",
]

#: A DST-observing zone that every complete database contains; used as a liveness probe.
PROBE_ZONE = "Europe/Berlin"

#: Permissive IANA identifier shape: ``Area/Location``, ``Area/Region/Location`` or ``UTC``.
_IANA_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*$")

TZDataStatus = Literal["package", "system", "missing"]


@dataclass(frozen=True)
class TZDataReport:
    """What the environment currently offers for time zone resolution."""

    status: TZDataStatus
    iana_version: str | None
    search_path: tuple[str, ...]
    fallback_candidate: str | None
    detail: str
    install_command: str = TZDATA_INSTALL_COMMAND

    @property
    def is_available(self) -> bool:
        return self.status != "missing"


# --------------------------------------------------------------------------- #
# low level probes
# --------------------------------------------------------------------------- #
def _package_version() -> str | None:
    try:
        import tzdata  # noqa: PLC0415 - optional dependency, probed on demand
    except ImportError:
        return None
    return getattr(tzdata, "IANA_VERSION", None)


def _package_available() -> bool:
    return importlib.util.find_spec("tzdata") is not None


def _zone_loads(zone_name: str) -> bool:
    try:
        ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _is_tzif_file(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"TZif"
    except OSError:
        return False


def system_tzif_candidates() -> tuple[Path, ...]:
    """Directories that contain a usable compiled (TZif) IANA database.

    Only trees that actually contain ``Europe/Berlin`` in TZif format qualify - a text-format
    database (for example Tcl's ``tzdata`` directory shipped with Python) is silently useless to
    ``zoneinfo`` and must not be reported as usable.
    """
    roots: list[Path] = []
    if sys.platform == "win32":
        roots.extend(
            [
                Path(r"C:\Program Files\Git\mingw64\share\zoneinfo"),
                Path(r"C:\Program Files\Git\usr\share\zoneinfo"),
                Path(r"C:\Program Files (x86)\Git\mingw64\share\zoneinfo"),
                Path(r"C:\Program Files (x86)\Git\usr\share\zoneinfo"),
            ]
        )
    roots.extend(
        [
            Path("/usr/share/zoneinfo"),
            Path("/usr/lib/zoneinfo"),
            Path("/usr/share/lib/zoneinfo"),
            Path("/etc/zoneinfo"),
        ]
    )
    roots.extend(Path(entry) for entry in zoneinfo.TZPATH if entry)

    valid: list[Path] = []
    for root in roots:
        if root in valid:
            continue
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        if _is_tzif_file(root / "Europe" / "Berlin"):
            valid.append(root)
    return tuple(valid)


def _version_from_tzdata_zi(root: Path) -> str | None:
    try:
        with open(root / "tzdata.zi", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    match = re.match(r"#\s*version\s+(\S+)", first_line.strip())
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# public probes
# --------------------------------------------------------------------------- #
def tzdata_status() -> TZDataStatus:
    """Where the IANA database comes from: ``package``, ``system`` or ``missing``."""
    if _package_available() and _zone_loads(PROBE_ZONE):
        return "package"
    if _zone_loads(PROBE_ZONE):
        return "system"
    return "missing"


def detect_timezone_source() -> str:
    """Human-readable one-liner about the current source (used by ``doctor``)."""
    return probe_tzdata().detail


def tzdata_version() -> str | None:
    """IANA version of the database in use, when it can be determined."""
    status = tzdata_status()
    if status == "package":
        return _package_version()
    if status == "system":
        for root in system_tzif_candidates():
            version = _version_from_tzdata_zi(root)
            if version:
                return version
        return _package_version()
    return None


def probe_tzdata() -> TZDataReport:
    """Full report: source, version, search path and (if any) a usable local fallback."""
    status = tzdata_status()
    candidates = system_tzif_candidates()
    fallback = str(candidates[0]) if candidates else None
    version = tzdata_version()

    if status == "package":
        detail = (
            f"tzdata package provides the IANA database"
            f" (version {version or 'unknown'})"
        )
    elif status == "system":
        paths = ", ".join(zoneinfo.TZPATH) or "unknown search path"
        detail = (
            f"IANA database found through the zoneinfo search path"
            f" (version {version or 'unknown'}): {paths}"
        )
    else:
        detail = "no IANA time zone database is reachable"
        if fallback:
            detail += (
                f"; a compiled TZif tree exists at {fallback} and can be used by setting "
                f"PYTHONTZPATH (or ZoneInfo.reset_tzpath)"
            )
    return TZDataReport(
        status=status,
        iana_version=version,
        search_path=tuple(zoneinfo.TZPATH),
        fallback_candidate=fallback,
        detail=detail,
    )


def activate_system_tzif_fallback() -> Path | None:
    """Explicitly switch ``zoneinfo`` to a discovered system TZif tree.

    Returns the path that was activated, or ``None`` when a database is already available or no
    usable tree was found. Intended for offline development and tests only; production code must
    not call this, so that a missing ``tzdata`` package cannot be papered over silently.
    """
    if tzdata_status() != "missing":
        return None
    candidates = system_tzif_candidates()
    if not candidates:
        return None
    chosen = candidates[0]
    reset_tzpath((str(chosen),))
    ZoneInfo.clear_cache()
    return chosen


# --------------------------------------------------------------------------- #
# validation / loading
# --------------------------------------------------------------------------- #
def validate_timezone_name(name: str, *, require_database: bool = False) -> None:
    """Validate an IANA identifier.

    Syntax is always validated. Existence is validated only when a database is actually
    reachable (or when the caller insists), so the domain model stays usable on a machine
    without tzdata while real local-time resolution still fails loudly.
    """
    if not isinstance(name, str) or not name:
        raise InvalidTimezoneNameError(name, "expected a non-empty string")
    if name != name.strip() or any(character.isspace() for character in name):
        raise InvalidTimezoneNameError(name, "must not contain whitespace")
    if name.startswith("/") or ".." in name:
        raise InvalidTimezoneNameError(name, "absolute paths and '..' are not IANA identifiers")
    if not _IANA_NAME_RE.match(name):
        raise InvalidTimezoneNameError(name, "expected something like 'Area/Location' or 'UTC'")

    if tzdata_status() == "missing":
        if require_database:
            raise TimezoneDataMissingError(
                f"Cannot verify IANA time zone {name!r}.",
                fallback_hint=_fallback_hint(),
            )
        return
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise UnknownTimezoneError(name) from None
    except ValueError as exc:
        raise InvalidTimezoneNameError(name, str(exc)) from None


def load_timezone(name: str) -> ZoneInfo:
    """Return the :class:`ZoneInfo` for an IANA identifier, with actionable errors."""
    validate_timezone_name(name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if tzdata_status() == "missing":
            raise TimezoneDataMissingError(
                f"Zone {name!r} could not be loaded.",
                fallback_hint=_fallback_hint(),
            ) from None
        raise UnknownTimezoneError(name) from None
    except ValueError as exc:
        raise InvalidTimezoneNameError(name, str(exc)) from None


def _fallback_hint() -> str | None:
    candidates = system_tzif_candidates()
    if not candidates:
        return None
    return (
        f"A compiled TZif tree is available at {candidates[0]}; set PYTHONTZPATH to that "
        "directory to use it (development fallback, not a substitute for the tzdata package)."
    )
