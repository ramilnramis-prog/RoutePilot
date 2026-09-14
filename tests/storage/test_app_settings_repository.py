"""Stage 3 U11 (D38): the ``app_settings`` key/value store.

Covers the approved schema's settings table (``docs/STORAGE_SCHEMA.md`` section 6) through
``storage.sqlite.app_settings_repository``:

* ``get``/``set`` round-trip of whole JSON values - strings, numbers, ``None``, booleans, lists and
  nested objects - with the stored text checked to be JSON;
* ``None`` for an absent key (documented: an unset setting is not an error) and *not* for a stored
  JSON ``null``, which is a value;
* overwrite semantics: a second ``set`` replaces the value, keeps exactly one row, and refreshes the
  repository-managed ``updated_at_utc`` (pinned through the injected clock);
* loud rejection: a non-JSON-serialisable value, an empty or blank key, and a malformed stored
  ``value_json`` each raise :class:`storage.StoredSettingsError`;
* the documented intended keys of schema section 6 are listed but not enforced: a key outside that
  list is stored and returned like any other, because this repository owns no setting's semantics;
* no database file is created: the database is ``:memory:``.

Deterministic and offline: no wall clock (the clock is injected), no files, no network.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from storage import StorageError, StoredSettingsError
from storage.sqlite.app_settings_repository import (
    INTENDED_SETTING_KEYS,
    SETTINGS_COLUMNS,
    SqliteAppSettingsRepository,
)
from storage.sqlite.database import connect, migrate

CLOCK_START = datetime(2026, 9, 11, 5, 0, 0, tzinfo=timezone.utc)


class SteppingClock:
    """A clock the tests control: each call returns the current instant, then advances it."""

    def __init__(self, start: datetime, *, step_seconds: int = 60) -> None:
        self.current = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        moment = self.current
        self.current = self.current + self._step
        return moment


class SettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        migrate(self.connection)
        self.clock = SteppingClock(CLOCK_START)
        self.repository = SqliteAppSettingsRepository(self.connection, clock=self.clock)

    def scalar(self, query: str, *parameters: Any) -> Any:
        return self.connection.execute(query, parameters).fetchone()[0]

    def row(self, key: str) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT * FROM app_settings WHERE key = ?", (key,)
        ).fetchone()


class RoundTripTests(SettingsTestCase):
    def test_values_round_trip_by_type(self) -> None:
        values: tuple[Any, ...] = (
            "Europe/Moscow",
            30,
            1.5,
            True,
            False,
            None,
            ["a", "b"],
            {"max_zoom": 19, "attribution": "© demo"},
            [],
            {},
        )
        for index, value in enumerate(values):
            with self.subTest(value=value):
                key = f"key-{index}"
                self.repository.set(key, value)
                self.assertEqual(self.repository.get(key), value)

    def test_intended_keys_round_trip_as_documented_types(self) -> None:
        documented = {
            "default_timezone": "Europe/Moscow",
            "doctor_mode": "strict",
            "default_data_provenance": "DEMO_SYNTHETIC",
            "tile_url": "https://tiles.invalid/{z}/{x}/{y}.png",
            "tile_attribution": "© demo tiles",
            "tile_max_zoom": 19,
        }
        for key in INTENDED_SETTING_KEYS:
            self.repository.set(key, documented[key])
        for key in INTENDED_SETTING_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.repository.get(key), documented[key])
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM app_settings"), len(INTENDED_SETTING_KEYS)
        )

    def test_a_key_outside_the_documented_list_is_stored_like_any_other(self) -> None:
        """The intended keys are documentation, not a whitelist: no setting's semantics live here."""
        self.repository.set("future_setting", {"enabled": True})
        self.assertEqual(self.repository.get("future_setting"), {"enabled": True})

    def test_the_stored_text_is_json(self) -> None:
        self.repository.set("tile_max_zoom", 19)
        stored = self.scalar("SELECT value_json FROM app_settings WHERE key = 'tile_max_zoom'")
        self.assertEqual(json.loads(stored), 19)
        self.assertEqual(self.row("tile_max_zoom").keys(), list(SETTINGS_COLUMNS))

    def test_an_absent_key_is_none_and_a_stored_null_is_a_value(self) -> None:
        self.assertIsNone(self.repository.get("never_set"))
        self.repository.set("explicitly_null", None)
        self.assertIsNone(self.repository.get("explicitly_null"))
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM app_settings WHERE key = 'explicitly_null'"), 1
        )

    def test_set_is_an_upsert_that_replaces_the_value_and_the_timestamp(self) -> None:
        self.repository.set("doctor_mode", "lenient")
        first = self.row("doctor_mode")
        self.repository.set("doctor_mode", "strict")
        second = self.row("doctor_mode")

        self.assertEqual(self.scalar("SELECT COUNT(*) FROM app_settings"), 1)
        self.assertEqual(self.repository.get("doctor_mode"), "strict")
        self.assertEqual(
            first["updated_at_utc"], "2026-09-11T05:00:00Z"
        )
        self.assertEqual(second["updated_at_utc"], "2026-09-11T05:01:00Z")
        self.assertNotEqual(first["updated_at_utc"], second["updated_at_utc"])

    def test_setting_the_same_value_twice_still_refreshes_the_timestamp(self) -> None:
        self.repository.set("tile_url", "https://tiles.invalid/{z}")
        self.repository.set("tile_url", "https://tiles.invalid/{z}")
        self.assertEqual(self.row("tile_url")["updated_at_utc"], "2026-09-11T05:01:00Z")


class RejectionTests(SettingsTestCase):
    def test_a_non_serialisable_value_is_refused_and_nothing_is_written(self) -> None:
        for value in (object(), {1, 2}, b"bytes", datetime(2026, 9, 11)):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(StoredSettingsError) as caught:
                    self.repository.set("bad", value)
                self.assertIn("has no JSON representation", str(caught.exception))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM app_settings"), 0)
        self.assertIsNone(self.repository.get("bad"))

    def test_a_non_finite_float_is_refused_and_nothing_is_written(self) -> None:
        """RFC 8259 JSON has no literal for nan or inf, so such a value is never stored as text."""
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(StoredSettingsError) as caught:
                    self.repository.set("non_finite", value)
                self.assertIn("RFC 8259", str(caught.exception))
        for value in ({"tile_max_zoom": float("inf")}, [1.0, float("nan")]):
            with self.subTest(value=value):
                with self.assertRaises(StoredSettingsError):
                    self.repository.set("non_finite", value)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM app_settings"), 0)
        self.assertIsNone(self.repository.get("non_finite"))

    def test_a_refused_value_does_not_clobber_a_stored_one(self) -> None:
        self.repository.set("tile_max_zoom", 18)
        with self.assertRaises(StoredSettingsError):
            self.repository.set("tile_max_zoom", object())
        self.assertEqual(self.repository.get("tile_max_zoom"), 18)

    def test_an_empty_or_blank_key_is_refused(self) -> None:
        for key in ("", "   ", "\t", None, 7, b"key"):
            with self.subTest(key=key):
                with self.assertRaises(StoredSettingsError):
                    self.repository.set(key, 1)
                with self.assertRaises(StoredSettingsError):
                    self.repository.get(key)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM app_settings"), 0)

    def test_malformed_stored_json_is_refused_not_read_as_unset(self) -> None:
        self.repository.set("tile_url", "https://tiles.invalid/{z}")
        with self.connection:
            self.connection.execute(
                "UPDATE app_settings SET value_json = ? WHERE key = 'tile_url'",
                ("{not json",),
            )
        with self.assertRaises(StoredSettingsError) as caught:
            self.repository.get("tile_url")
        self.assertIn("not valid JSON", str(caught.exception))

    def test_a_stored_value_json_that_is_not_text_is_refused(self) -> None:
        self.repository.set("tile_url", "https://tiles.invalid/{z}")
        with self.connection:
            self.connection.execute(
                "UPDATE app_settings SET value_json = X'00' WHERE key = 'tile_url'"
            )
        with self.assertRaises(StoredSettingsError):
            self.repository.get("tile_url")

    def test_a_clock_that_is_not_timezone_aware_is_refused(self) -> None:
        repository = SqliteAppSettingsRepository(
            self.connection, clock=lambda: datetime(2026, 9, 11, 5, 0, 0)
        )
        with self.assertRaises(StorageError):
            repository.set("tile_url", "https://tiles.invalid/{z}")


class ConstructionTests(SettingsTestCase):
    def test_a_connection_without_row_factory_is_refused(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(StorageError):
            SqliteAppSettingsRepository(connection)

    def test_a_database_without_the_settings_table_is_refused(self) -> None:
        connection = connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(StorageError) as caught:
            SqliteAppSettingsRepository(connection)
        self.assertIn("app_settings", str(caught.exception))

    def test_no_database_file_is_created(self) -> None:
        self.repository.set("tile_max_zoom", 19)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                "app_settings",
            ),
            1,
        )
        self.assertEqual(self.connection.execute("PRAGMA database_list").fetchone()[2], "")
