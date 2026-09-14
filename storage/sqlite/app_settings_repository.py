"""SQLite ``AppSettingsRepository``: the ``app_settings`` key/value store (U11; D38).

Implements the pure port :class:`core.repositories.AppSettingsRepository` against the APPROVED
schema ``docs/STORAGE_SCHEMA.md`` section 6, using stdlib ``sqlite3`` only - no ORM and no
third-party dependency. Storage depends on ``core``; ``core`` never imports this module.

What this repository is, and what it deliberately is not
-------------------------------------------------------

It is a **store**, not a policy engine:

* :meth:`SqliteAppSettingsRepository.get` returns the stored value of a key, or ``None`` when the
  key is absent - a documented answer, not an error, so a caller can tell "never set" from a value
  it stored itself;
* :meth:`SqliteAppSettingsRepository.set` upserts the whole JSON value of one key and manages
  ``updated_at_utc`` itself. It preserves nothing else: the row is exactly
  ``(key, value_json, updated_at_utc)``, and a re-set replaces the value and refreshes the stamp;
* **no setting's meaning lives here.** This module does not know which keys exist in a given
  deployment, what their values mean, which are required, or what a missing one should fall back to.
  Those are decisions of the code that owns each setting, and inventing defaults here would be a
  product decision taken in an adapter.

Intended keys (schema section 6), documented but deliberately not policed
------------------------------------------------------------------------

``default_timezone``
    IANA zone name a new plan starts from (never a numeric offset - D2).
``doctor_mode``
    how much environment checking the doctor performs.
``default_data_provenance``
    the provenance a new plan or run is created with (``DEMO_SYNTHETIC`` / ``REAL_ROUTING``).
``tile_url``
    base URL of the map tile service.
``tile_attribution``
    the attribution text the tile vendor requires.
``tile_max_zoom``
    the deepest zoom the configured tile service serves.

Tile configuration lives in settings precisely so a map vendor stays configuration-isolated (D15)
and never leaks into ``core/``. This repository neither requires any of these keys to be present nor
rejects a key outside the list: it stores what the owning code asks it to store.

Failure behaviour
-----------------

Loud, never lossy:

* an empty or blank key is refused before any SQL runs;
* a value that is not JSON-serialisable at all (an arbitrary object, a set, a ``bytes`` payload) is
  refused by serialising it first, so nothing partial is written;
* a value carrying a non-finite float (``nan`` / ``inf``) is refused for the same reason: RFC 8259
  JSON has no literal for it, so storing it would write text a conforming parser cannot read;
* a stored ``value_json`` that is not valid JSON raises :class:`storage.StoredSettingsError` when it
  is read, instead of being softened into ``None`` (which would make a corrupt row look "unset").
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from storage import StorageError, StoredSettingsError
from storage.sqlite.database import utc_now_iso

__all__ = ["INTENDED_SETTING_KEYS", "SETTINGS_COLUMNS", "SqliteAppSettingsRepository"]

#: The intended keys of schema section 6, in the schema's order. Documented, never enforced: this
#: repository stores and returns JSON values by key and owns none of their semantics.
INTENDED_SETTING_KEYS = (
    "default_timezone",
    "doctor_mode",
    "default_data_provenance",
    "tile_url",
    "tile_attribution",
    "tile_max_zoom",
)

#: The approved column set of ``app_settings`` (schema section 6), in schema order.
SETTINGS_COLUMNS = ("key", "value_json", "updated_at_utc")

_UPSERT = (
    "INSERT INTO app_settings (key, value_json, updated_at_utc) VALUES (?, ?, ?) "
    "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json, "
    "updated_at_utc = excluded.updated_at_utc"
)


class SqliteAppSettingsRepository:
    """Store and read whole JSON settings values by key (schema section 6).

    ``connection`` must be an open connection to a migrated database - use
    :func:`storage.sqlite.database.connect` followed by :func:`storage.sqlite.database.migrate`.
    The constructor refuses a connection whose rows are not ``sqlite3.Row`` and one that lacks the
    approved table, so a misconfiguration fails at construction instead of at the first read.

    ``clock`` is a testability seam returning a timezone-aware UTC ``datetime``; it exists so
    deterministic tests can pin ``updated_at_utc``, and it defaults to the real clock.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._ensure_row_factory(connection)
        self._ensure_schema(connection)

    # ------------------------------------------------------------------ #
    # reading and writing
    # ------------------------------------------------------------------ #
    def get(self, key: str) -> Any:
        """The stored value of ``key``, or ``None`` when no such key is stored (documented).

        Absence is a normal answer, not an error: the caller asked for the value of a setting and
        there is none. A stored value that is not valid JSON is a different fact and is raised as
        :class:`~storage.StoredSettingsError` - a corrupt row must never masquerade as "unset".
        """
        self._require_key(key)
        row = self._connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        stored = row["value_json"]
        if not isinstance(stored, str):
            raise StoredSettingsError(
                f"setting {key!r}: value_json must be TEXT holding JSON, got "
                f"{type(stored).__name__}"
            )
        try:
            return json.loads(stored)
        except json.JSONDecodeError as error:
            raise StoredSettingsError(
                f"setting {key!r}: value_json is not valid JSON ({error}); the stored text is "
                f"{stored!r}. A corrupt setting is reported instead of being read as 'unset'."
            ) from None

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``, replacing any previous value (upsert).

        The value is serialised **before** any SQL runs, so a value that cannot be represented as
        JSON is refused without touching the stored row. ``updated_at_utc`` is written by this
        repository (UTC ISO-8601 with a trailing ``Z``, schema section 1) and nothing else about the
        row is preserved.

        Raises:
            StoredSettingsError: ``key`` is empty or blank, or ``value`` is not JSON-serialisable or
                carries a non-finite float (``nan`` / ``inf``), which RFC 8259 JSON has no literal
                for and which would make the stored text unreadable by a conforming parser.
            storage.StorageError: the repository clock does not return a timezone-aware datetime.
        """
        self._require_key(key)
        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise StoredSettingsError(
                f"setting {key!r} cannot be stored: value of type {type(value).__name__} is not "
                f"storable as RFC 8259 JSON ({error}). Settings hold JSON values, so a value that "
                "has no JSON representation - including a non-finite float such as nan or inf - is "
                "refused instead of being stored as non-standard text."
            ) from None
        now = self._now_iso()
        with self._connection:
            self._connection.execute(_UPSERT, (key, payload, now))

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_key(key: object) -> None:
        if not isinstance(key, str) or not key.strip():
            raise StoredSettingsError(
                f"a setting key must be a non-empty string, got {key!r}; the key is the primary "
                "key of app_settings, so a blank one could not identify a setting"
            )

    def _now_iso(self) -> str:
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise StorageError(
                "the repository clock must return a timezone-aware datetime (storage stores UTC "
                f"only), got {moment!r}"
            )
        return utc_now_iso(moment)

    @staticmethod
    def _ensure_row_factory(connection: sqlite3.Connection) -> None:
        if connection.row_factory is not sqlite3.Row:
            raise StorageError(
                "this repository reads rows by column name, so the connection needs "
                "row_factory = sqlite3.Row; open it with storage.sqlite.database.connect()"
            )

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", ("app_settings",)
            ).fetchone()
            is None
        ):
            raise StorageError(
                "the database is missing app_settings: apply the approved schema first with "
                "storage.sqlite.database.migrate(connection)"
            )
