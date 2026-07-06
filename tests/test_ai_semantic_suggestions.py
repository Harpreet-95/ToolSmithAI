"""
Tests for Phase 3D — AI Suggestion Review Queue.

Covers:
  1.  Suggestions inserted on generate (AI enabled, weak column)
  2.  Dedupe: second generate does NOT insert a duplicate PENDING row
  3.  List pending suggestions for a source
  4.  Accept applies business_label / meaning to dictionary, is_approved stays 0
  5.  Accept sets generation_method = 'ai_suggested'
  6.  Reject does NOT update dictionary row
  7.  Accept blocked for human-approved row (returns blocked dict, not None)
  8.  Reject marks status REJECTED
  9.  Accept marks status ACCEPTED
  10. Accept on non-existent suggestion returns None
  11. Reject on already-reviewed suggestion returns None
  12. list_ai_suggestions returns only matching status rows
  13. Ownership check: wrong user cannot list/accept/reject

Run from the project root:
    python -m pytest tests/test_ai_semantic_suggestions.py -v
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-ai-sug-long-enough-secret!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-ai-sug")

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from data.dictionary_service import (
    accept_ai_suggestion,
    generate_and_save_dictionary,
    list_ai_suggestions,
    reject_ai_suggestion,
)


# ---------------------------------------------------------------------------
# Minimal in-memory schema
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT    NOT NULL,
        display_name          TEXT    NOT NULL DEFAULT 'Test Source',
        source_type           TEXT    NOT NULL DEFAULT 'mssql',
        source_category       TEXT    NOT NULL DEFAULT 'relational_db',
        encrypted_config_json TEXT    NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json     TEXT    NOT NULL DEFAULT '[]',
        metadata_json         TEXT    NOT NULL DEFAULT '{}',
        source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
        is_active             INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE schema_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        source_type      TEXT    NOT NULL DEFAULT 'mssql',
        table_count      INTEGER NOT NULL DEFAULT 0,
        view_count       INTEGER NOT NULL DEFAULT 0,
        column_count     INTEGER NOT NULL DEFAULT 0,
        snapshot_json    TEXT    NOT NULL DEFAULT '{}',
        discovered_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_snapshots (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id               INTEGER NOT NULL,
        schema_snapshot_id      INTEGER NOT NULL DEFAULT 1,
        snapshot_version        INTEGER NOT NULL DEFAULT 1,
        mode                    TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        sample_rate             REAL    NOT NULL DEFAULT 1.0,
        profiling_rules_version TEXT    NOT NULL DEFAULT '4.0.0',
        status                  TEXT    NOT NULL DEFAULT 'COMPLETE',
        tables_total            INTEGER NOT NULL DEFAULT 0,
        tables_profiled         INTEGER NOT NULL DEFAULT 0,
        tables_skipped          INTEGER NOT NULL DEFAULT 0,
        tables_failed           INTEGER NOT NULL DEFAULT 0,
        tables_timed_out        INTEGER NOT NULL DEFAULT 0,
        columns_total           INTEGER NOT NULL DEFAULT 0,
        columns_profiled        INTEGER NOT NULL DEFAULT 0,
        columns_skipped         INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled     INTEGER NOT NULL DEFAULT 0,
        pii_columns_found       INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at              TEXT,
        completed_at            TEXT,
        duration_seconds        INTEGER,
        resumable_state_json    TEXT,
        batch_size              INTEGER NOT NULL DEFAULT 50,
        next_table_index        INTEGER NOT NULL DEFAULT 0,
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_column_profiles (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id   INTEGER NOT NULL,
        source_id               INTEGER NOT NULL,
        table_fqn               TEXT    NOT NULL,
        column_name             TEXT    NOT NULL,
        data_type               TEXT    NOT NULL DEFAULT 'TEXT',
        raw_type                TEXT,
        is_nullable             INTEGER NOT NULL DEFAULT 1,
        is_primary_key          INTEGER NOT NULL DEFAULT 0,
        is_identity             INTEGER NOT NULL DEFAULT 0,
        ordinal_position        INTEGER NOT NULL DEFAULT 0,
        null_percentage         REAL,
        blank_percentage        REAL,
        uniqueness_score        REAL,
        cardinality_tier        TEXT,
        semantic_type           TEXT,
        semantic_confidence     REAL,
        pii_name_heuristic      INTEGER NOT NULL DEFAULT 0,
        pii_confirmed           INTEGER NOT NULL DEFAULT 0,
        quality_score           REAL,
        quality_grade           TEXT,
        dominant_pattern        TEXT,
        pattern_coverage        REAL,
        profiling_depth         TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_status        TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE data_dictionary_tables (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL,
        table_fqn         TEXT    NOT NULL,
        table_name        TEXT    NOT NULL,
        schema_name       TEXT    NOT NULL,
        table_type        TEXT    NOT NULL DEFAULT 'TABLE',
        business_name     TEXT,
        description       TEXT,
        domain            TEXT,
        grain             TEXT,
        is_approved       INTEGER NOT NULL DEFAULT 0,
        approved_by       TEXT,
        approved_at       TEXT,
        generation_method TEXT    NOT NULL DEFAULT 'rule_based',
        created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source_id, table_fqn)
    );

    CREATE TABLE data_dictionary_columns (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL,
        table_fqn         TEXT    NOT NULL,
        column_name       TEXT    NOT NULL,
        business_label    TEXT,
        meaning           TEXT,
        semantic_type     TEXT,
        is_metric         INTEGER NOT NULL DEFAULT 0,
        is_dimension      INTEGER NOT NULL DEFAULT 0,
        is_date           INTEGER NOT NULL DEFAULT 0,
        is_id             INTEGER NOT NULL DEFAULT 0,
        pii_risk          INTEGER NOT NULL DEFAULT 0,
        is_approved       INTEGER NOT NULL DEFAULT 0,
        approved_by       TEXT,
        approved_at       TEXT,
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
        provider                TEXT,
        model                   TEXT,
        prompt_version          TEXT,
        created_by              TEXT,
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reviewed_by             TEXT,
        reviewed_at             TEXT
    );
"""


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------

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
    conn.commit()
    return conn


@pytest.fixture()
def db():
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.dictionary_service.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _col(name: str, data_type: str = "TEXT", *, pk: bool = False) -> ColumnInfo:
    return ColumnInfo(
        column_name=name,
        ordinal_position=1,
        data_type=data_type,
        raw_type="nvarchar",
        is_nullable=True,
        is_primary_key=pk,
        is_identity=False,
    )


def _table(schema: str, name: str, cols: list[ColumnInfo]) -> TableInfo:
    return TableInfo(
        table_name=name,
        schema_name=schema,
        table_fqn=f"{schema}.{name}",
        table_type="TABLE",
        columns=cols,
    )


def _snapshot(source_id: int, tables: list[TableInfo]) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=source_id,
        source_type="mssql",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )


def _seed_source(db: sqlite3.Connection, user_id: str = "user-1") -> int:
    cur = db.execute(
        "INSERT INTO data_source_connections (user_id) VALUES (?)", (user_id,)
    )
    db.commit()
    return cur.lastrowid


def _seed_schema_snapshot(db: sqlite3.Connection, source_id: int, snap: SchemaSnapshot) -> int:
    cur = db.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, "
        "column_count, snapshot_json, discovered_at, created_at) "
        "VALUES (?, 1, 'mssql', 0, 0, 0, ?, ?, CURRENT_TIMESTAMP)",
        (source_id, json.dumps(dataclasses.asdict(snap)), snap.discovered_at),
    )
    db.commit()
    return cur.lastrowid


def _seed_profiling_snapshot(db: sqlite3.Connection, source_id: int) -> int:
    cur = db.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, snapshot_version) "
        "VALUES (?, 1, 1)",
        (source_id,),
    )
    db.commit()
    return cur.lastrowid


def _seed_col_profile(
    db: sqlite3.Connection,
    profiling_snapshot_id: int,
    source_id: int,
    table_fqn: str,
    column_name: str,
    *,
    semantic_type: str | None = None,
    semantic_confidence: float | None = None,
    quality_score: float | None = None,
) -> None:
    db.execute(
        """INSERT INTO profiling_column_profiles
           (profiling_snapshot_id, source_id, table_fqn, column_name,
            semantic_type, semantic_confidence, quality_score)
           VALUES (?,?,?,?,?,?,?)""",
        (profiling_snapshot_id, source_id, table_fqn, column_name,
         semantic_type, semantic_confidence, quality_score),
    )
    db.commit()


def _make_ai_result(
    business_name: str = "AI Business Name",
    description: str = "AI-inferred description.",
    domain: str = "General",
    entity: str = "Unknown",
    confidence: float = 0.75,
    reasoning: tuple = ("semantic_type=unknown", "low_confidence"),
    review_required: bool = True,
):
    from core.ai.models import AISemanticResult
    return AISemanticResult(
        business_name=business_name,
        description=description,
        domain=domain,
        entity=entity,
        confidence=confidence,
        reasoning=reasoning,
        review_required=review_required,
    )


def _get_suggestions(db: sqlite3.Connection, source_id: int, status: str = "PENDING") -> list[dict]:
    rows = db.execute(
        "SELECT * FROM ai_semantic_suggestions WHERE source_id = ? AND status = ?",
        (source_id, status),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_col_dict(db: sqlite3.Connection, source_id: int, table_fqn: str, col: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM data_dictionary_columns WHERE source_id = ? AND table_fqn = ? AND column_name = ?",
        (source_id, table_fqn, col),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Test 1: Suggestion inserted when AI enabled on weak column
# ---------------------------------------------------------------------------

def test_suggestion_inserted_on_generate(db, monkeypatch):
    """generate_and_save_dictionary must persist an AI suggestion to
    ai_semantic_suggestions when AI is enabled and column is weak."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "signals", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.signals", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result(
        business_name="Raw Signal Data",
        description="Unclassified signal data requiring manual review.",
        confidence=0.65,
        reasoning=("semantic_type=unknown", "low_profiling_confidence"),
    )

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        result = generate_and_save_dictionary(source_id, "user-1")

    assert result["ai_suggestions_count"] == 1
    assert result["ai_suggestions_queued"] == 1

    rows = _get_suggestions(db, source_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["table_fqn"] == "dbo.signals"
    assert row["column_name"] == "raw_data"
    assert row["suggested_business_name"] == "Raw Signal Data"
    assert row["status"] == "PENDING"
    assert row["review_required"] == 1
    assert row["created_by"] == "user-1"

    reasoning = json.loads(row["ai_reasoning_json"])
    assert "semantic_type=unknown" in reasoning


# ---------------------------------------------------------------------------
# Test 2: Dedupe — second generate does NOT create a duplicate PENDING row
# ---------------------------------------------------------------------------

def test_dedupe_no_duplicate_pending(db, monkeypatch):
    """Running Generate Dictionary twice must not create a second PENDING row
    for the same column when one already exists."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "signals", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.signals", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result()

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")
        # Run again — should NOT create a second row
        result2 = generate_and_save_dictionary(source_id, "user-1")

    assert result2["ai_suggestions_queued"] == 0, (
        "Second run must not insert when a PENDING row already exists"
    )

    rows = _get_suggestions(db, source_id)
    assert len(rows) == 1, "Only one PENDING row must exist for the column"


# ---------------------------------------------------------------------------
# Test 3: List pending suggestions for a source
# ---------------------------------------------------------------------------

def test_list_pending_suggestions(db, monkeypatch):
    """list_ai_suggestions must return all PENDING rows for the source."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "events", [_col("raw_data", "NVARCHAR"), _col("notes", "TEXT")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    for col in ("raw_data", "notes"):
        _seed_col_profile(db, prof_id, source_id, "dbo.events", col,
                          semantic_type="UNKNOWN", semantic_confidence=0.15)

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    suggestions = list_ai_suggestions(source_id, "user-1")
    assert suggestions is not None
    assert len(suggestions) == 2

    cols_returned = {s["column_name"] for s in suggestions}
    assert cols_returned == {"raw_data", "notes"}
    for s in suggestions:
        assert s["status"] == "PENDING"


# ---------------------------------------------------------------------------
# Test 4: Accept applies business_label and meaning; is_approved stays 0
# ---------------------------------------------------------------------------

def test_accept_applies_dictionary_but_does_not_approve(db, monkeypatch):
    """Accepting a suggestion must write business_label and meaning to the
    dictionary column row but must NOT set is_approved = 1."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "signals", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.signals", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result(
        business_name="Raw Signal Payload",
        description="Binary payload data from sensor arrays.",
        confidence=0.78,
    )
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    sug = _get_suggestions(db, source_id)[0]
    result = accept_ai_suggestion(source_id, "user-1", sug["id"])

    assert result is not None
    assert result.get("accepted") is True

    col = _get_col_dict(db, source_id, "dbo.signals", "raw_data")
    assert col is not None
    assert col["business_label"] == "Raw Signal Payload"
    assert col["meaning"] == "Binary payload data from sensor arrays."
    assert col["is_approved"] == 0, "Accept must never auto-approve the dictionary row"


# ---------------------------------------------------------------------------
# Test 5: Accept sets generation_method = 'ai_suggested'
# ---------------------------------------------------------------------------

def test_accept_sets_generation_method_ai_suggested(db, monkeypatch):
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.misc", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    sug = _get_suggestions(db, source_id)[0]
    accept_ai_suggestion(source_id, "user-1", sug["id"])

    col = _get_col_dict(db, source_id, "dbo.misc", "raw_data")
    assert col["generation_method"] == "ai_suggested"


# ---------------------------------------------------------------------------
# Test 6: Reject does NOT update dictionary row
# ---------------------------------------------------------------------------

def test_reject_does_not_modify_dictionary(db, monkeypatch):
    """Rejecting a suggestion must not change business_label or meaning in the
    data_dictionary_columns row."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "records", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.records", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result(business_name="Should Not Appear", description="Should not appear.")
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    col_before = _get_col_dict(db, source_id, "dbo.records", "raw_data")
    original_label   = col_before["business_label"]
    original_meaning = col_before["meaning"]

    sug = _get_suggestions(db, source_id)[0]
    result = reject_ai_suggestion(source_id, "user-1", sug["id"])

    assert result is not None
    assert result.get("rejected") is True

    col_after = _get_col_dict(db, source_id, "dbo.records", "raw_data")
    assert col_after["business_label"] == original_label, "Reject must not change business_label"
    assert col_after["meaning"] == original_meaning,      "Reject must not change meaning"


# ---------------------------------------------------------------------------
# Test 7: Accept blocked for human-approved row
# ---------------------------------------------------------------------------

def test_accept_blocked_for_human_approved_row(db, monkeypatch):
    """Accepting a suggestion whose target column has is_approved=1 must return
    a dict with blocked=True and must NOT modify the dictionary row."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "customers", [_col("notes", "TEXT")])]
    snap = _snapshot(source_id, tables)
    snap_id = _seed_schema_snapshot(db, source_id, snap)

    # Pre-seed a human-approved dictionary row
    db.execute(
        """INSERT INTO data_dictionary_columns
           (source_id, snapshot_id, table_fqn, column_name,
            business_label, meaning, semantic_type,
            is_metric, is_dimension, is_date, is_id, pii_risk,
            is_approved, approved_by, generation_method,
            created_at, updated_at)
           VALUES (?, ?, 'dbo.customers', 'notes',
                   'Customer Notes', 'Freeform notes by account team.',
                   'dimension', 0, 1, 0, 0, 0, 1, 'admin',
                   'human', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (source_id, snap_id),
    )
    db.commit()

    # Insert a suggestion manually (as if generated by AI)
    db.execute(
        """INSERT INTO ai_semantic_suggestions
           (source_id, object_type, table_fqn, column_name,
            suggested_business_name, suggested_description,
            ai_confidence, ai_reasoning_json, review_required,
            status, created_by, created_at)
           VALUES (?, 'dict.column', 'dbo.customers', 'notes',
                   'AI Override Name', 'AI-generated description.',
                   0.80, '[]', 1, 'PENDING', 'user-1', CURRENT_TIMESTAMP)""",
        (source_id,),
    )
    db.commit()

    sug_id = db.execute(
        "SELECT id FROM ai_semantic_suggestions WHERE source_id = ?", (source_id,)
    ).fetchone()["id"]

    result = accept_ai_suggestion(source_id, "user-1", sug_id)

    # Must return blocked, not None
    assert result is not None, "accept_ai_suggestion must return a dict, not None"
    assert result.get("blocked") is True, "Result must signal blocked=True"

    # Dictionary row must be untouched
    col = _get_col_dict(db, source_id, "dbo.customers", "notes")
    assert col["is_approved"] == 1,           "Human approval must not be cleared"
    assert col["business_label"] == "Customer Notes", "Human label must not be overwritten"
    assert col["generation_method"] == "human",       "generation_method must stay 'human'"


# ---------------------------------------------------------------------------
# Test 8: Reject marks status REJECTED
# ---------------------------------------------------------------------------

def test_reject_marks_status_rejected(db, monkeypatch):
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.misc", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    sug = _get_suggestions(db, source_id)[0]
    reject_ai_suggestion(source_id, "user-1", sug["id"])

    row = db.execute(
        "SELECT status, reviewed_by FROM ai_semantic_suggestions WHERE id = ?",
        (sug["id"],),
    ).fetchone()
    assert row["status"] == "REJECTED"
    assert row["reviewed_by"] == "user-1"


# ---------------------------------------------------------------------------
# Test 9: Accept marks status ACCEPTED
# ---------------------------------------------------------------------------

def test_accept_marks_status_accepted(db, monkeypatch):
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.misc", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    sug = _get_suggestions(db, source_id)[0]
    accept_ai_suggestion(source_id, "user-1", sug["id"])

    row = db.execute(
        "SELECT status, reviewed_by FROM ai_semantic_suggestions WHERE id = ?",
        (sug["id"],),
    ).fetchone()
    assert row["status"] == "ACCEPTED"
    assert row["reviewed_by"] == "user-1"


# ---------------------------------------------------------------------------
# Test 10: Accept on non-existent suggestion returns None
# ---------------------------------------------------------------------------

def test_accept_nonexistent_suggestion_returns_none(db):
    source_id = _seed_source(db)
    result = accept_ai_suggestion(source_id, "user-1", 999_999)
    assert result is None


# ---------------------------------------------------------------------------
# Test 11: Reject on already-reviewed suggestion returns None
# ---------------------------------------------------------------------------

def test_reject_already_reviewed_returns_none(db, monkeypatch):
    """Rejecting an already-REJECTED (or ACCEPTED) suggestion must return None."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(db, prof_id, source_id, "dbo.misc", "raw_data",
                      semantic_type="UNKNOWN", semantic_confidence=0.20)

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    sug = _get_suggestions(db, source_id)[0]
    reject_ai_suggestion(source_id, "user-1", sug["id"])
    # Second reject on same suggestion
    result2 = reject_ai_suggestion(source_id, "user-1", sug["id"])
    assert result2 is None, "Re-rejecting an already-reviewed suggestion must return None"


# ---------------------------------------------------------------------------
# Test 12: list_ai_suggestions returns only matching status rows
# ---------------------------------------------------------------------------

def test_list_filters_by_status(db, monkeypatch):
    """list_ai_suggestions(status='ACCEPTED') must not return PENDING rows."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "events", [_col("raw_data", "NVARCHAR"), _col("notes", "TEXT")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    for col in ("raw_data", "notes"):
        _seed_col_profile(db, prof_id, source_id, "dbo.events", col,
                          semantic_type="UNKNOWN", semantic_confidence=0.15)

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        generate_and_save_dictionary(source_id, "user-1")

    all_sug = _get_suggestions(db, source_id)
    assert len(all_sug) == 2

    # Accept first, reject second
    accept_ai_suggestion(source_id, "user-1", all_sug[0]["id"])
    reject_ai_suggestion(source_id, "user-1", all_sug[1]["id"])

    pending  = list_ai_suggestions(source_id, "user-1", "PENDING")
    accepted = list_ai_suggestions(source_id, "user-1", "ACCEPTED")
    rejected = list_ai_suggestions(source_id, "user-1", "REJECTED")

    assert len(pending)  == 0
    assert len(accepted) == 1
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# Test 13: Ownership check — wrong user is blocked
# ---------------------------------------------------------------------------

def test_ownership_check_blocks_wrong_user(db):
    """list_ai_suggestions / accept / reject must return None for a user_id
    that does not own the data source."""
    source_id = _seed_source(db, user_id="owner-user")

    # Manually insert a suggestion
    db.execute(
        """INSERT INTO ai_semantic_suggestions
           (source_id, object_type, table_fqn, column_name,
            ai_reasoning_json, status, created_by, created_at)
           VALUES (?, 'dict.column', 'dbo.t', 'col', '[]', 'PENDING', 'owner-user', CURRENT_TIMESTAMP)""",
        (source_id,),
    )
    db.commit()
    sug_id = db.execute(
        "SELECT id FROM ai_semantic_suggestions WHERE source_id = ?", (source_id,)
    ).fetchone()["id"]

    wrong_user = "attacker-user"
    assert list_ai_suggestions(source_id, wrong_user) is None
    assert accept_ai_suggestion(source_id, wrong_user, sug_id) is None
    assert reject_ai_suggestion(source_id, wrong_user, sug_id) is None
