"""
Tests for the "live query enabled by default" product decision's backfill
migration in data.models.init_db().

Before this change, live_query_enabled defaulted to 0 for every source
(data/models.py's column DEFAULT) and nothing ever flipped it to 1 except a
manual, undocumented raw SQLite UPDATE. The product decision reverses the
default going forward (see data.datasource_service.create_data_source,
tested in tests/test_connect_time_lightweight_bootstrap_fixture.py's
test_live_query_enabled_by_default_no_manual_flip) and additionally requires
existing already-connected SQL sources to be upgraded, since re-connecting
them is not something a user should have to do just to pick up this policy
change.

init_db() runs on every app startup, so the backfill here is written as an
idempotent migration (matching the existing pattern used for every other
schema/data migration in this file): re-running it is always safe, it only
ever raises rows from 0 to 1, never lowers them, and it only ever touches
rows whose registered capabilities include 'sql_query' — a source with no
SQL capability (hypothetical today; all three registered connectors are
relational) is left untouched.

Run from the project root:
    venv/Scripts/pytest tests/test_live_query_default_migration.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-live-query-migration-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-live-query-migration-salt-long-enough-456")

import data.models as models

_USER = "user-1"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "live_query_migration.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", path)
    models.init_db()
    return path


def _insert_source(path: str, capabilities: list[str], live_query_enabled: int = 0) -> int:
    conn = _db_conn(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO data_source_connections
                (user_id, display_name, source_type, source_category,
                 encrypted_config_json, capabilities_json, live_query_enabled,
                 created_at, updated_at)
            VALUES (?, 'Pre-existing Source', 'mssql', 'relational_db', '{}', ?, ?,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (_USER, json.dumps(capabilities), live_query_enabled),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _live_query_enabled(path: str, source_id: int) -> int:
    conn = _db_conn(path)
    try:
        return conn.execute(
            "SELECT live_query_enabled FROM data_source_connections WHERE id = ?",
            (source_id,),
        ).fetchone()["live_query_enabled"]
    finally:
        conn.close()


def test_preexisting_sql_source_upgraded_on_next_init(db_path):
    source_id = _insert_source(
        db_path,
        capabilities=["connection_test", "schema_discovery", "sql_query"],
        live_query_enabled=0,
    )

    models.init_db()  # simulates the next app startup, e.g. after this deploy

    assert _live_query_enabled(db_path, source_id) == 1


def test_source_without_sql_query_capability_is_left_untouched(db_path):
    source_id = _insert_source(
        db_path,
        capabilities=["connection_test", "schema_discovery"],  # no sql_query
        live_query_enabled=0,
    )

    models.init_db()

    assert _live_query_enabled(db_path, source_id) == 0


def test_already_enabled_source_is_untouched(db_path):
    source_id = _insert_source(
        db_path,
        capabilities=["connection_test", "schema_discovery", "sql_query"],
        live_query_enabled=1,
    )

    models.init_db()

    assert _live_query_enabled(db_path, source_id) == 1


def test_migration_is_idempotent_across_repeated_startups(db_path):
    source_id = _insert_source(
        db_path,
        capabilities=["connection_test", "schema_discovery", "sql_query"],
        live_query_enabled=0,
    )

    for _ in range(3):
        models.init_db()

    assert _live_query_enabled(db_path, source_id) == 1
