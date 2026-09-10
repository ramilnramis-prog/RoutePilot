"""RoutePilot time handling: IANA database discovery, strict DST resolution, timeline arithmetic.

Import submodules explicitly (``from core.time import tz``) - this package intentionally
re-exports nothing, so that :mod:`core.time.tzdata` can be imported by the domain model without
creating an import cycle through :mod:`core.time.timeline`.
"""

__all__: list[str] = []
