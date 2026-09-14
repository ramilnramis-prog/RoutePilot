"""RoutePilot API package: the HTTP transport and its framework-agnostic application layer.

Layering (``docs/ARCHITECTURE.md`` section 1, extended for Stage 4):

    web/      HTML/CSS/JS + map (later, U15)        -- never imported by core
    api/      transport + application layer          -- depends on core, storage and demo
    demo/     deterministic demo dataset             -- depends on core
    storage/  SQLite repositories behind core Protocols
    core/     domain, time and engine                -- imports nothing but the stdlib

The dependency direction is one-way, and it is enforced rather than remembered:
``core/`` never imports ``api/`` (``tools/isolation_check.py``,
``tests/test_core_isolation.py``, ``python tools/doctor.py``).

What lives here
===============

``api.services``
    the **framework-agnostic** application layer: plain functions/classes over the repository
    Protocols and a database identifier, returning Python data and domain objects. No HTTP type,
    no status code and no JSON appears there, so a future FastAPI transport can replace
    ``api.http_server`` without touching this module or ``core/``.
``api.serialization``
    domain -> JSON-ready payloads, with the serialisation contracts documented in one place.
``api.http_server``
    the stdlib ``ThreadingHTTPServer`` transport: the route table, request parsing, response
    writing, static asset serving, the pure static-path resolver and the error mapping table.
``api.serve``
    the CLI (``python -m api.serve``), default host ``127.0.0.1`` and default database
    ``var/routepilot.db`` (a gitignored directory).

Scope of this unit (U13): the **read/config surface only** - health, plans list/get/create, the
approved ``enabled``/``priority`` stop controls and the settings store. The recommendation, the
driver's selection, the computed route and the run history are declared but **not implemented**
(they are U14) and answer ``501`` with the capability named, never a fabricated result. There is
no web UI yet (U15), and there is no background job queue by owner decision.

Every number the API reports comes from ``core``/``storage``; no business formula is implemented
in this package. All shipped data is DEMO/SYNTHETIC and is labelled as such (D15/D16/D23).
"""

from __future__ import annotations

__all__ = ["API_PACKAGE", "DESCRIPTION"]

#: Module path of this package, for diagnostics and tests.
API_PACKAGE = __name__

DESCRIPTION = "RoutePilot HTTP transport and framework-agnostic application layer (Stage 4 U13)"
