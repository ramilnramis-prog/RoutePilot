"""Static guard: ``core/`` must stay pure (decision D1, spec sections 14, 22, 26).

The dependency rule is a property of the codebase, so it is checked automatically instead of
being remembered. Two things are rejected inside ``core/``:

* imports of HTTP, UI, storage, network or vendor modules;
* imports of RoutePilot's own outer layers (``api``, ``web``, ``storage``, ``demo``, ``tools``,
  ``tests``) - the dependency direction only ever points inwards.

Limitation: this is a static check over ``import`` statements. A dynamic
``importlib.import_module("sqlite3")`` would slip through; there is no reason for such code to
exist in ``core``, and code review covers the rest.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FORBIDDEN_LAYERS",
    "FORBIDDEN_MODULES",
    "ImportViolation",
    "scan_core",
    "scan_directory",
]

#: Standard-library and third-party modules that would break core purity.
FORBIDDEN_MODULES: dict[str, str] = {
    # network / transport
    "socket": "network access",
    "socketserver": "network access",
    "ssl": "network access",
    "http": "HTTP access",
    "urllib": "network access",
    "ftplib": "network access",
    "smtplib": "network access",
    "telnetlib": "network access",
    "xmlrpc": "network access",
    "webbrowser": "UI/network access",
    "requests": "HTTP client",
    "httpx": "HTTP client",
    "aiohttp": "HTTP client",
    "fastapi": "web framework",
    "flask": "web framework",
    "django": "web framework",
    "starlette": "web framework",
    "uvicorn": "ASGI server",
    # storage
    "sqlite3": "storage",
    "shelve": "storage",
    "dbm": "storage",
    "pickle": "uncontrolled serialization",
    # UI
    "tkinter": "UI toolkit",
    "PyQt5": "UI toolkit",
    "PyQt6": "UI toolkit",
    "PySide6": "UI toolkit",
    # process / environment
    "subprocess": "process control",
    "multiprocessing": "process control",
    "asyncio": "IO event loop (core must stay deterministic and IO-free)",
    # time zone handling that bypasses zoneinfo
    "pytz": "use zoneinfo + tzdata instead (D2)",
    "dateutil": "use zoneinfo + tzdata instead (D2)",
}

#: RoutePilot's own outer layers: core must never import them.
FORBIDDEN_LAYERS: frozenset[str] = frozenset(
    {"api", "web", "storage", "demo", "tools", "tests"}
)


@dataclass(frozen=True)
class ImportViolation:
    """One forbidden import inside ``core/``."""

    path: str
    line: int
    module: str
    reason: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: imports {self.module!r} ({self.reason})"


def _root_module(module: str | None) -> str | None:
    if not module:
        return None
    return module.split(".", 1)[0]


def scan_directory(package_root: Path, *, display_root: Path | None = None) -> tuple[ImportViolation, ...]:
    """Scan a package directory for forbidden imports."""
    package_root = Path(package_root)
    display_root = Path(display_root) if display_root else package_root
    violations: list[ImportViolation] = []
    if not package_root.is_dir():
        return ()

    for path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):  # a syntax error is a different problem, not a violation
            continue
        try:
            display_path = str(path.relative_to(display_root))
        except ValueError:
            display_path = str(path)

        for node in ast.walk(tree):
            module: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = _root_module(alias.name)
                    if root:
                        _record(violations, display_path, node.lineno, root)
                continue
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative import inside core is fine
                    continue
                module = node.module
            if module:
                root = _root_module(module)
                if root:
                    _record(violations, display_path, node.lineno, root)

    return tuple(violations)


def _record(
    violations: list[ImportViolation], display_path: str, line: int, root: str
) -> None:
    if root in FORBIDDEN_LAYERS:
        violations.append(
            ImportViolation(
                display_path,
                line,
                root,
                "core must not depend on an outer layer (dependency direction points inwards)",
            )
        )
    elif root in FORBIDDEN_MODULES:
        violations.append(ImportViolation(display_path, line, root, FORBIDDEN_MODULES[root]))


def scan_core(repo_root: Path | str) -> tuple[ImportViolation, ...]:
    """Scan ``<repo_root>/core`` for forbidden imports."""
    repo_root = Path(repo_root)
    return scan_directory(repo_root / "core", display_root=repo_root)
