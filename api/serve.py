"""Command-line entry point for the RoutePilot API (Stage 4 U13).

    python -m api.serve                       # http://127.0.0.1:8000, database var/routepilot.db
    python -m api.serve --port 8080
    python -m api.serve --db var/other.db
    python -m api.serve --host 0.0.0.0        # explicit, not the default: no authentication exists
    python -m api.serve --port 0              # let the OS choose; the chosen port is printed

Standard library only. The server binds to **loopback** by default, runs the (idempotent)
migration runner once at start-up and opens **one SQLite connection per request** from the
configured database identifier.

The default database lives at ``var/routepilot.db``. ``var/`` is gitignored, so no database
artifact can be committed; the file is created on first start (its parent directory included).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from api.http_server import STATIC_ROOT, create_server
from api.services import DEFAULT_DB_PATH, ApiServices

__all__ = ["build_parser", "main", "serve"]


def build_parser() -> argparse.ArgumentParser:
    """The documented command-line surface of ``python -m api.serve``."""
    parser = argparse.ArgumentParser(
        prog="python -m api.serve",
        description=(
            "Serve the RoutePilot API (stdlib http.server). The data is DEMO/SYNTHETIC and the "
            "transport is read/config only: no recommendation, selection or route endpoints yet."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default: %(default)s - loopback only, on purpose)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to bind; 0 lets the operating system choose (default: %(default)s)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        metavar="PATH",
        help=(
            "SQLite database path (default: %(default)s, which lives in the gitignored var/ "
            "directory) or an explicit SQLite identifier such as "
            "file:routepilot?mode=memory&cache=shared"
        ),
    )
    parser.add_argument(
        "--static-root",
        default=str(STATIC_ROOT),
        metavar="DIR",
        help=(
            "directory static assets are served from (default: %(default)s). The workspace that "
            "lives there is opened at the server root: GET / serves index.html."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the per-request access log",
    )
    return parser


def _prepare_database(identifier: str) -> None:
    """Create the parent directory of a file-backed database so the first start succeeds.

    An in-memory identifier (``:memory:`` or ``file:...?mode=memory...``) has no directory and is
    left exactly as given; nothing here invents a path.
    """
    if ":memory:" in identifier or "mode=memory" in identifier:
        return
    parent = Path(identifier).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def serve(arguments: argparse.Namespace) -> int:
    """Run the server described by ``arguments`` until interrupted. Returns an exit code."""
    _prepare_database(arguments.db)
    services = ApiServices(arguments.db)
    try:
        server = create_server(
            services,
            host=arguments.host,
            port=arguments.port,
            static_root=arguments.static_root,
            quiet=arguments.quiet,
        )
    except OSError as error:
        services.close()
        print(
            f"cannot bind {arguments.host}:{arguments.port}: {error}. Pick another --port "
            "(only one process can serve a port).",
            file=sys.stderr,
        )
        return 1
    host, port = server.server_address[0], server.server_address[1]
    print(f"RoutePilot API serving on http://{host}:{port}", flush=True)
    print(
        f"  database: {services.state.display_identifier} "
        f"(schema version {services.state.schema_version})",
        flush=True,
    )
    print(f"  static root: {server.static_root}", flush=True)
    print("  stop with Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("RoutePilot API stopped", flush=True)
    finally:
        server.server_close()
        services.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and serve. Returns the process exit code."""
    arguments = build_parser().parse_args(argv)
    if not 0 <= arguments.port <= 65535:
        print(f"--port must be between 0 and 65535, got {arguments.port}", file=sys.stderr)
        return 2
    return serve(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
