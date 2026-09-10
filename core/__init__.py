"""RoutePilot — a practical route planner for drivers, couriers and dispatchers.

``core`` is deliberately pure: it must not import HTTP, UI, storage or network modules
(D1). It is the only package that contains product logic.
"""

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
