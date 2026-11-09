"""
Tests for the object_type guard added to data.dictionary_service.accept_ai_suggestion()
in Phase 11. The ai_semantic_suggestions review queue is shared across dict.column
(existing) and the new domain.assignment / entity.assignment / schema.drift / pii.new
object types created by the autonomous metadata lifecycle. accept_ai_suggestion()'s
"apply to dictionary row" logic only makes sense for dict.column — this guard
prevents it from silently mis-applying (or no-op'ing while claiming success) for
the new types. reject_ai_suggestion() needs no such guard: it is a pure status
flip that was already object-type-agnostic.

Run from the project root:
    python -m pytest tests/test_ai_suggestions_object_type_guard.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-ai-suggestion-guard-long-1!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-ai-suggestion-guard")

from data.dictionary_service import accept_ai_suggestion, reject_ai_suggestion

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL
    );

    CREATE TABLE data_dictionary_columns (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL DEFAULT 1,
        table_fqn         TEXT    NOT NULL,
        column_name       TEXT    NOT NULL,
        business_label    TEXT,
        meaning           TEXT,
        is_approved       INTEGER NOT NULL DEFAULT 0,
        generation_method TEXT    NOT NULL DEFAULT 'rule_based',
        created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source_id, table_fqn, column_name)
    );

    CREATE TABLE ai_semantic_suggestions (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id               INTEGER NOT NULL,
        object_type             TEXT    NOT NULL DEFAULT 'dict.column',
        table_fqn               TEXT    NOT NULL,
        column_name             TEXT    NOT NULL,
        suggested_business_name TEXT,
        suggested_description   TEXT,
        suggested_domain        TEXT,
        suggested_entity        TEXT,
        ai_confidence           REAL,
        ai_reasoning_json       TEXT    NOT NULL DEFAULT '[]',
        review_required         INTEGER NOT NULL DEFAULT 1,
        status                  TEXT    NOT NULL DEFAULT 'PENDING',
        created_by              TEXT,
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reviewed_by             TEXT,
        reviewed_at             TEXT
    );
"""


class _NoClose:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")
    conn.commit()
    return conn


def _insert_suggestion(conn, object_type: str, table_fqn="dbo.orders", column_name="") -> int:
    cursor = conn.execute(
        "INSERT INTO ai_semantic_suggestions "
        "(source_id, object_type, table_fqn, column_name, suggested_domain, status, created_at) "
        "VALUES (1, ?, ?, ?, 'Finance', 'PENDING', '2026-01-01T00:00:00+00:00')",
        (object_type, table_fqn, column_name),
    )
    conn.commit()
    return cursor.lastrowid


def _env():
    conn = _make_db()
    wrapper = _NoClose(conn)
    patcher = patch("data.dictionary_service.get_connection", return_value=wrapper)
    patcher.start()
    return conn, patcher


class TestAcceptBlocksNonDictColumnTypes:
    def test_domain_assignment_suggestion_is_blocked(self):
        conn, patcher = _env()
        try:
            sug_id = _insert_suggestion(conn, "domain.assignment")
            result = accept_ai_suggestion(1, "u1", sug_id)
            assert result["blocked"] is True
            row = conn.execute(
                "SELECT status FROM ai_semantic_suggestions WHERE id = ?", (sug_id,)
            ).fetchone()
            assert row["status"] == "PENDING"  # untouched
        finally:
            patcher.stop()

    def test_schema_drift_suggestion_is_blocked(self):
        conn, patcher = _env()
        try:
            sug_id = _insert_suggestion(conn, "schema.drift")
            result = accept_ai_suggestion(1, "u1", sug_id)
            assert result["blocked"] is True
        finally:
            patcher.stop()

    def test_dict_column_suggestion_still_accepted(self):
        conn, patcher = _env()
        try:
            conn.execute(
                "INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name) "
                "VALUES (1, 'dbo.orders', 'amount')"
            )
            conn.commit()
            sug_id = _insert_suggestion(conn, "dict.column", table_fqn="dbo.orders", column_name="amount")
            result = accept_ai_suggestion(1, "u1", sug_id)
            assert result == {"accepted": True, "suggestion_id": sug_id}
        finally:
            patcher.stop()


class TestRejectStillWorksForAllTypes:
    def test_reject_domain_assignment_suggestion(self):
        conn, patcher = _env()
        try:
            sug_id = _insert_suggestion(conn, "domain.assignment")
            result = reject_ai_suggestion(1, "u1", sug_id)
            assert result == {"rejected": True, "suggestion_id": sug_id}
            row = conn.execute(
                "SELECT status FROM ai_semantic_suggestions WHERE id = ?", (sug_id,)
            ).fetchone()
            assert row["status"] == "REJECTED"
        finally:
            patcher.stop()
