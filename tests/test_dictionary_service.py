"""
Tests for dictionary generation with deep profiling signals (Phase 2A).

Covers:
  1. No profiling snapshot → old rule-based behaviour preserved
  2. High-confidence profiling semantic_type overrides name-only classifier
  3. Low-confidence profiling semantic_type does NOT override
  4. PII profiling signal produces manual-review wording
  5. Low quality score adds review caveat
  6. High uniqueness ID column gets specific identifier wording
  7. Generated dictionary entries remain unapproved (is_approved = 0)

All tests use an in-memory SQLite DB patched over data.dictionary_service and
data.profiling_service.get_connection — no on-disk state is touched.

Run from the project root:
    python -m pytest tests/test_dictionary_service.py -v
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
os.environ.setdefault("JWT_SECRET",     "test-jwt-dict-service-long-enough-secret!!")
os.environ.setdefault("USER_ID_SALT",   "test-salt-dict-service")

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from data.dictionary_service import generate_and_save_dictionary


# ---------------------------------------------------------------------------
# Minimal schema — covers every table touched by generate_and_save_dictionary
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
    """Wraps sqlite3.Connection, suppresses close() so the in-memory DB
    survives across multiple service calls within a single test."""

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
    """Fresh in-memory DB.  Patches get_connection in both service modules."""
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.dictionary_service.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Schema-building helpers
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
    pii_name_heuristic: int = 0,
    pii_confirmed: int = 0,
    null_percentage: float | None = None,
    blank_percentage: float | None = None,
    uniqueness_score: float | None = None,
    cardinality_tier: str | None = None,
    quality_score: float | None = None,
    quality_grade: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO profiling_column_profiles
           (profiling_snapshot_id, source_id, table_fqn, column_name,
            semantic_type, semantic_confidence,
            pii_name_heuristic, pii_confirmed,
            null_percentage, blank_percentage,
            uniqueness_score, cardinality_tier,
            quality_score, quality_grade)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            profiling_snapshot_id, source_id, table_fqn, column_name,
            semantic_type, semantic_confidence,
            pii_name_heuristic, pii_confirmed,
            null_percentage, blank_percentage,
            uniqueness_score, cardinality_tier,
            quality_score, quality_grade,
        ),
    )
    db.commit()


def _get_col_entry(db: sqlite3.Connection, source_id: int, table_fqn: str, col_name: str) -> dict:
    row = db.execute(
        "SELECT * FROM data_dictionary_columns "
        "WHERE source_id = ? AND table_fqn = ? AND column_name = ?",
        (source_id, table_fqn, col_name),
    ).fetchone()
    assert row is not None, f"No dictionary entry found for {table_fqn}.{col_name}"
    return dict(row)


# ---------------------------------------------------------------------------
# Test 1: No profiling snapshot → behaviour identical to pure rule-based
# ---------------------------------------------------------------------------

def test_no_profiling_snapshot_uses_rule_based_behaviour(db):
    """When no profiling_snapshots row exists, generate_and_save_dictionary must
    produce the same output as if profiling_context were None."""
    source_id = _seed_source(db)
    tables = [_table("dbo", "orders", [_col("order_id", pk=True), _col("status")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    # No profiling snapshot seeded.

    result = generate_and_save_dictionary(source_id, "user-1")

    assert result is not None
    assert result["generation_method"] == "rule_based"

    entry = _get_col_entry(db, source_id, "dbo.orders", "order_id")
    # Rule-based: primary-key column → semantic_type 'id'
    assert entry["semantic_type"] == "id"
    assert "identifier" in entry["meaning"].lower()
    assert entry["generation_method"] == "rule_based"


# ---------------------------------------------------------------------------
# Test 2: High-confidence profiling semantic_type overrides name-only classifier
# ---------------------------------------------------------------------------

def test_high_confidence_profiling_overrides_semantic_type(db):
    """When profiling assigns semantic_type='AMOUNT' with confidence >= 0.70,
    the dictionary entry must use 'metric' (the mapped type) instead of the
    rule classifier's type derived from the column name."""
    source_id = _seed_source(db)
    # Column name 'val' would normally be classified as 'other' by rules,
    # but profiling says AMOUNT with high confidence.
    tables = [_table("dbo", "payments", [_col("val", "INTEGER")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.payments", "val",
        semantic_type="AMOUNT",
        semantic_confidence=0.85,
    )

    result = generate_and_save_dictionary(source_id, "user-1")

    assert result is not None
    entry = _get_col_entry(db, source_id, "dbo.payments", "val")
    assert entry["semantic_type"] == "metric", (
        f"Expected 'metric' from profiling AMOUNT override, got '{entry['semantic_type']}'"
    )
    assert entry["generation_method"] == "rule_based+profiling"
    assert result["generation_method"] == "rule_based+profiling"


# ---------------------------------------------------------------------------
# Test 3: Low-confidence profiling semantic_type does NOT override
# ---------------------------------------------------------------------------

def test_low_confidence_profiling_does_not_override_semantic_type(db):
    """When profiling semantic_confidence < 0.70, the rule-based semantic_type
    must be preserved."""
    source_id = _seed_source(db)
    # 'status' would be classified as 'dimension' by name rules (TEXT type)
    tables = [_table("dbo", "records", [_col("status")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.records", "status",
        semantic_type="FLAG",
        semantic_confidence=0.55,  # below threshold
    )

    generate_and_save_dictionary(source_id, "user-1")

    entry = _get_col_entry(db, source_id, "dbo.records", "status")
    # Rule-based dimension (TEXT, not an id/date/flag/metric)
    assert entry["semantic_type"] == "dimension", (
        f"Low-confidence profiling should not override; expected 'dimension', "
        f"got '{entry['semantic_type']}'"
    )


# ---------------------------------------------------------------------------
# Test 4: PII profiling signal produces manual-review wording
# ---------------------------------------------------------------------------

def test_pii_profiling_signal_produces_manual_review_meaning(db):
    """A column with pii_confirmed=1 or pii_name_heuristic=1 in the profiling
    snapshot must receive the '[PII — manual review required]' meaning,
    even if the rule-based classifier did not flag it as PII."""
    source_id = _seed_source(db)
    # 'score' would not normally be detected as PII by name rules.
    tables = [_table("dbo", "assessments", [_col("score", "INTEGER")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.assessments", "score",
        pii_confirmed=1,  # profiling pattern analysis confirmed PII
    )

    generate_and_save_dictionary(source_id, "user-1")

    entry = _get_col_entry(db, source_id, "dbo.assessments", "score")
    assert entry["pii_risk"] == 1
    assert entry["meaning"] == "[PII — manual review required]", (
        f"Expected PII placeholder, got: {entry['meaning']!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Low quality score adds review caveat to meaning
# ---------------------------------------------------------------------------

def test_low_quality_score_adds_review_caveat(db):
    """A column with quality_score < 60 must have 'requires review due to data quality'
    appended to its meaning."""
    source_id = _seed_source(db)
    tables = [_table("dbo", "events", [_col("category")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.events", "category",
        quality_score=42.0,  # below 60
    )

    generate_and_save_dictionary(source_id, "user-1")

    entry = _get_col_entry(db, source_id, "dbo.events", "category")
    assert "requires review due to data quality" in entry["meaning"], (
        f"Expected quality caveat in meaning, got: {entry['meaning']!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: High uniqueness ID column gets specific identifier wording
# ---------------------------------------------------------------------------

def test_high_uniqueness_id_column_gets_specific_identifier_wording(db):
    """A column with semantic_type='ID' (or resolved to 'id') and uniqueness_score >= 0.95
    must use the 'Unique identifier for each … record.' phrasing."""
    source_id = _seed_source(db)
    # 'record_id' is a PK, so rule classifier → 'id'. Profiling confirms high uniqueness.
    tables = [_table("dbo", "logs", [_col("record_id", pk=True)])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.logs", "record_id",
        semantic_type="ID",
        semantic_confidence=0.95,
        uniqueness_score=0.999,
    )

    generate_and_save_dictionary(source_id, "user-1")

    entry = _get_col_entry(db, source_id, "dbo.logs", "record_id")
    assert "each" in entry["meaning"].lower() and "record" in entry["meaning"].lower(), (
        f"Expected 'each … record' wording for high-uniqueness ID, got: {entry['meaning']!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: Generated dictionary entries remain unapproved (is_approved = 0)
# ---------------------------------------------------------------------------

def test_generated_entries_are_unapproved(db):
    """Both table and column dictionary entries generated by generate_and_save_dictionary
    must have is_approved = 0 regardless of whether profiling was used."""
    source_id = _seed_source(db)
    tables = [
        _table("dbo", "customers", [
            _col("customer_id", pk=True),
            _col("email"),
        ]),
    ]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    # Seed profiling so profiling path is exercised
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.customers", "customer_id",
        semantic_type="ID",
        semantic_confidence=0.90,
        uniqueness_score=1.0,
    )

    generate_and_save_dictionary(source_id, "user-1")

    table_rows = db.execute(
        "SELECT is_approved FROM data_dictionary_tables WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    col_rows = db.execute(
        "SELECT is_approved FROM data_dictionary_columns WHERE source_id = ?",
        (source_id,),
    ).fetchall()

    assert table_rows, "No table dictionary entries were generated"
    assert col_rows, "No column dictionary entries were generated"

    for r in table_rows:
        assert r["is_approved"] == 0, "Table entry must start unapproved"
    for r in col_rows:
        assert r["is_approved"] == 0, "Column entry must start unapproved"


# ===========================================================================
# Phase 3C — AI Semantic Intelligence: Dictionary Suggestions
# Tests 8–15
# ===========================================================================

# ---------------------------------------------------------------------------
# Shared helpers for Phase 3C tests
# ---------------------------------------------------------------------------

def _make_ai_result(
    business_name: str = "Misc Data",
    description: str = "AI-inferred description for this column.",
    domain: str = "General",
    entity: str = "Unknown",
    confidence: float = 0.72,
    reasoning: tuple = ("semantic_type=unknown",),
    review_required: bool = True,
):
    """Return an AISemanticResult without importing it at module level."""
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


# ---------------------------------------------------------------------------
# Test 8: AI disabled → ai_suggestions_count == 0, all existing behaviour intact
# ---------------------------------------------------------------------------

def test_ai_disabled_does_not_produce_suggestions(db, monkeypatch):
    """When ENABLE_AI_SEMANTIC_INTELLIGENCE is not set, ai_suggestions_count must
    be 0 and the existing rule-based behaviour must be identical."""
    monkeypatch.delenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", raising=False)
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    result = generate_and_save_dictionary(source_id, "user-1")

    assert result is not None
    assert result["ai_suggestions_count"] == 0
    assert "ai_suggestions" not in result
    # Rule-based entry still generated
    entry = _get_col_entry(db, source_id, "dbo.misc", "raw_data")
    assert entry["generation_method"] == "rule_based"


# ---------------------------------------------------------------------------
# Test 9: AI disabled — flag is false string
# ---------------------------------------------------------------------------

def test_ai_disabled_via_false_env_flag(db, monkeypatch):
    """Setting ENABLE_AI_SEMANTIC_INTELLIGENCE=false (explicit) also disables AI."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "false")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze") as mock_analyze:
        result = generate_and_save_dictionary(source_id, "user-1")

    mock_analyze.assert_not_called()
    assert result["ai_suggestions_count"] == 0


# ---------------------------------------------------------------------------
# Test 10: High-confidence, specific semantic type → AI NOT called
# ---------------------------------------------------------------------------

def test_high_confidence_entry_skips_ai(db, monkeypatch):
    """A column with high profiling confidence and a specific semantic type
    must not be sent to the AI provider."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # order_id → PK → rule classifier gives semantic_type='id' (specific, not 'other')
    tables = [_table("dbo", "orders", [_col("order_id", pk=True)])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.orders", "order_id",
        semantic_type="ID",
        semantic_confidence=0.95,  # well above default threshold (0.75)
        quality_score=95.0,
    )

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze") as mock_analyze:
        result = generate_and_save_dictionary(source_id, "user-1")

    mock_analyze.assert_not_called()
    assert result["ai_suggestions_count"] == 0


# ---------------------------------------------------------------------------
# Test 11: 'other' semantic type → AI IS called, suggestion returned
# ---------------------------------------------------------------------------

def test_other_semantic_type_triggers_ai_suggestion(db, monkeypatch):
    """A column whose rule-engine gives semantic_type='other' must be sent to
    the AI layer.  The returned suggestion must appear in the summary and must
    NOT be written to the database."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # 'NVARCHAR' type → no rule-based classification → semantic_type='other'
    tables = [_table("dbo", "signals", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.signals", "raw_data",
        semantic_type="UNKNOWN",
        semantic_confidence=0.25,
    )

    mock_ai = _make_ai_result(
        business_name="Raw Signal Data",
        description="Unclassified signal data requiring manual review.",
        confidence=0.60,
    )

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        result = generate_and_save_dictionary(source_id, "user-1")

    # Suggestion in summary
    assert result["ai_suggestions_count"] == 1
    suggestion = result["ai_suggestions"][0]
    assert suggestion["column_name"] == "raw_data"
    assert suggestion["suggested_business_name"] == "Raw Signal Data"
    assert suggestion["review_required"] is True

    # Database row unchanged — generation_method is rule-based, NOT ai_enriched
    entry = _get_col_entry(db, source_id, "dbo.signals", "raw_data")
    assert "ai" not in entry["generation_method"]
    assert entry["semantic_type"] == "other"   # rule-based result preserved


# ---------------------------------------------------------------------------
# Test 12: Low quality score → AI called, suggestion returned
# ---------------------------------------------------------------------------

def test_low_quality_score_triggers_ai(db, monkeypatch):
    """A column with quality_score < 60 must also trigger the AI layer
    even when the semantic type is a recognised category (not 'other')."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # 'category' TEXT → rule classifier gives semantic_type='dimension' (specific)
    # but quality_score=35 < 60 → AI should still be called
    tables = [_table("dbo", "events", [_col("category")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.events", "category",
        semantic_type="STATUS",
        semantic_confidence=0.80,   # high confidence
        quality_score=35.0,         # but very low quality — needs AI interpretation
        quality_grade="F",
    )

    mock_ai = _make_ai_result(
        business_name="Event Category",
        description="Classifies events by type; data quality requires review.",
        confidence=0.55,
    )

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai) as mock_analyze:
        result = generate_and_save_dictionary(source_id, "user-1")

    mock_analyze.assert_called_once()
    assert result["ai_suggestions_count"] == 1
    assert result["ai_suggestions"][0]["review_required"] is True


# ---------------------------------------------------------------------------
# Test 13: AI provider failure → dictionary generation still succeeds
# ---------------------------------------------------------------------------

def test_ai_failure_does_not_fail_dictionary_generation(db, monkeypatch):
    """If the AI provider raises during analysis, the exception must be caught
    and dictionary generation must complete successfully with ai_suggestions_count=0."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.misc", "raw_data",
        semantic_type="UNKNOWN",
        semantic_confidence=0.20,
    )

    with patch(
        "core.ai.semantic_intelligence.SemanticIntelligenceService.analyze",
        side_effect=RuntimeError("simulated API timeout"),
    ):
        result = generate_and_save_dictionary(source_id, "user-1")

    # Dictionary generation must have completed
    assert result is not None
    assert result["columns_generated"] == 1
    assert result["ai_suggestions_count"] == 0
    assert "ai_suggestions" not in result

    # Rule-based DB entry still intact
    entry = _get_col_entry(db, source_id, "dbo.misc", "raw_data")
    assert entry["semantic_type"] == "other"
    assert entry["is_approved"] == 0


# ---------------------------------------------------------------------------
# Test 14: Human-approved column row is not overwritten by regeneration
# ---------------------------------------------------------------------------

def test_human_approved_column_not_overwritten(db, monkeypatch):
    """A column row with generation_method='human' and is_approved=1 must
    survive an entire generate_and_save_dictionary call unchanged.
    This validates the ON CONFLICT ... WHERE generation_method != 'human' guard."""
    monkeypatch.delenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", raising=False)
    source_id = _seed_source(db)
    tables = [_table("dbo", "customers", [_col("notes", "TEXT")])]
    snap = _snapshot(source_id, tables)
    snap_id = _seed_schema_snapshot(db, source_id, snap)

    # Pre-seed a human-approved dictionary row with custom values
    db.execute(
        """INSERT INTO data_dictionary_columns
           (source_id, snapshot_id, table_fqn, column_name,
            business_label, meaning, semantic_type, is_metric, is_dimension,
            is_date, is_id, pii_risk, is_approved, approved_by,
            generation_method, created_at, updated_at)
           VALUES (?, ?, 'dbo.customers', 'notes',
                   'Customer Notes', 'Freeform notes entered by the account team.',
                   'dimension', 0, 1, 0, 0, 0, 1, 'admin',
                   'human', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (source_id, snap_id),
    )
    db.commit()

    generate_and_save_dictionary(source_id, "user-1")

    entry = _get_col_entry(db, source_id, "dbo.customers", "notes")
    assert entry["is_approved"] == 1,          "Human approval must not be cleared"
    assert entry["approved_by"] == "admin",    "Approver must not be overwritten"
    assert entry["generation_method"] == "human", "generation_method must stay 'human'"
    assert entry["business_label"] == "Customer Notes", "Business label must not change"
    assert "account team" in entry["meaning"], "Human meaning must be preserved"


# ---------------------------------------------------------------------------
# Test 15: AI suggestion is not auto-approved (is_approved stays 0 in DB)
# ---------------------------------------------------------------------------

def test_ai_suggestion_not_auto_approved(db, monkeypatch):
    """Even when the AI layer returns a high-confidence suggestion, the DB
    column entry must remain is_approved=0 and the generation_method must
    not be 'ai_enriched' — AI never writes to the DB."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.misc", "raw_data",
        semantic_type="UNKNOWN",
        semantic_confidence=0.20,
    )

    mock_ai = _make_ai_result(
        business_name="Raw Data Field",
        description="AI-inferred: unclassified data requiring review.",
        confidence=0.88,
        review_required=True,
    )

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", return_value=mock_ai):
        result = generate_and_save_dictionary(source_id, "user-1")

    # AI suggestion present in summary only
    assert result["ai_suggestions_count"] == 1
    assert result["ai_suggestions"][0]["review_required"] is True

    # DB row NOT modified by AI
    entry = _get_col_entry(db, source_id, "dbo.misc", "raw_data")
    assert entry["is_approved"] == 0,              "AI must never auto-approve"
    assert entry["generation_method"] != "ai_enriched", "AI must not write to DB"
    assert entry["meaning"] != mock_ai.description,     "AI description must not overwrite DB"


# ---------------------------------------------------------------------------
# Test 16: Provider called with rich context from profiling signals
# ---------------------------------------------------------------------------

def test_ai_context_includes_profiling_signals(db, monkeypatch):
    """The AISemanticContext passed to svc.analyze must include quality,
    cardinality, null rate, and uniqueness signals from the profiling snapshot."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    tables = [_table("dbo", "analysis", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.analysis", "raw_data",
        semantic_type="UNKNOWN",
        semantic_confidence=0.30,
        quality_score=45.0,
        quality_grade="D",
        null_percentage=12.5,
        uniqueness_score=0.85,
        cardinality_tier="HIGH",
    )

    captured: list = []

    def capture_context(ctx):
        captured.append(ctx)
        return None  # No suggestion — test is about context, not result

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze", side_effect=capture_context):
        generate_and_save_dictionary(source_id, "user-1")

    assert len(captured) == 1, "analyze() should have been called exactly once"
    ctx = captured[0]

    assert ctx.source_id == source_id
    assert ctx.table_fqn == "dbo.analysis"
    assert ctx.column_name == "raw_data"
    assert ctx.quality_score == 45.0
    assert ctx.quality_grade == "D"
    assert ctx.null_percentage == 12.5
    assert ctx.uniqueness_score == 0.85
    assert ctx.cardinality_tier == "HIGH"
    assert ctx.semantic_type == "other"  # mapped from 'other' (NVARCHAR → rule classifier)
    # Sample values are never included (PII safety)
    assert ctx.sample_values == []
    assert ctx.top_values == []


# ===========================================================================
# Phase 3D Performance — Safe limits on AI enrichment
# Tests 17–20
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 17: AI_SEMANTIC_MAX_SUGGESTIONS_PER_RUN is enforced
# ---------------------------------------------------------------------------

def test_max_suggestions_limit_enforced(db, monkeypatch):
    """When eligible columns exceed AI_SEMANTIC_MAX_SUGGESTIONS_PER_RUN, suggestions
    must be capped and ai_skipped_due_to_limit must reflect the remainder."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # 30 NVARCHAR columns with generic names → semantic_type='other' → all eligible
    cols = [_col(f"col_{i}", "NVARCHAR") for i in range(30)]
    tables = [_table("dbo", "signals", cols)]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    mock_ai = _make_ai_result()

    with patch("data.dictionary_service._AI_MAX_SUGGESTIONS_PER_RUN", 5), \
         patch("data.dictionary_service._AI_MAX_TABLES_PER_RUN", 99), \
         patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze",
               return_value=mock_ai):
        result = generate_and_save_dictionary(source_id, "user-1")

    assert result["ai_eligible_count"]       == 30, "All 30 NVARCHAR columns must be eligible"
    assert result["ai_suggestions_count"]    == 5,  "Suggestions must be capped at 5"
    assert result["ai_processed_count"]      == 5,  "Only 5 API calls should be made"
    assert result["ai_skipped_due_to_limit"] == 25, "Remaining 25 must be recorded as skipped"


# ---------------------------------------------------------------------------
# Test 18: AI_SEMANTIC_MAX_TABLES_PER_RUN is enforced
# ---------------------------------------------------------------------------

def test_max_tables_limit_enforced(db, monkeypatch):
    """Eligible columns spread across more tables than AI_SEMANTIC_MAX_TABLES_PER_RUN
    must only be processed for the first max_tables tables encountered."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # 10 tables × 2 eligible NVARCHAR columns = 20 eligible total
    tables = [
        _table("dbo", f"tbl_{i}", [_col("raw_a", "NVARCHAR"), _col("raw_b", "NVARCHAR")])
        for i in range(10)
    ]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    mock_ai = _make_ai_result()

    with patch("data.dictionary_service._AI_MAX_TABLES_PER_RUN", 3), \
         patch("data.dictionary_service._AI_MAX_SUGGESTIONS_PER_RUN", 100), \
         patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze",
               return_value=mock_ai):
        result = generate_and_save_dictionary(source_id, "user-1")

    # Only first 3 tables processed → 3×2=6 suggestions; 7×2=14 skipped
    assert result["ai_eligible_count"]       == 20, "All 20 columns must be counted as eligible"
    assert result["ai_suggestions_count"]    == 6,  "Only 3 tables × 2 columns = 6 suggestions"
    assert result["ai_skipped_due_to_limit"] == 14, "Remaining 14 from 7 skipped tables"


# ---------------------------------------------------------------------------
# Test 19: All four summary fields present and correct
# ---------------------------------------------------------------------------

def test_summary_ai_count_fields_present(db, monkeypatch):
    """generate_and_save_dictionary must always return all four AI count fields,
    with correct values for a mixed schema (eligible + ineligible columns)."""
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # order_id (PK → semantic_type='id' → NOT eligible)
    # raw_data (NVARCHAR → semantic_type='other' → eligible)
    tables = [_table("dbo", "misc", [_col("order_id", pk=True), _col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    mock_ai = _make_ai_result()
    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze",
               return_value=mock_ai):
        result = generate_and_save_dictionary(source_id, "user-1")

    for field in ("ai_eligible_count", "ai_processed_count",
                  "ai_suggestions_count", "ai_skipped_due_to_limit"):
        assert field in result, f"Summary field '{field}' is missing"

    assert result["ai_eligible_count"]       == 1, "Only raw_data is eligible"
    assert result["ai_processed_count"]      == 1, "One API call must be made"
    assert result["ai_suggestions_count"]    == 1, "One suggestion returned"
    assert result["ai_skipped_due_to_limit"] == 0, "No columns skipped — within limits"


# ---------------------------------------------------------------------------
# Test 20: All four summary fields present when AI is disabled (all zero)
# ---------------------------------------------------------------------------

def test_summary_ai_fields_zero_when_disabled(db, monkeypatch):
    """All four AI summary fields must be present and zero when
    ENABLE_AI_SEMANTIC_INTELLIGENCE is not set."""
    monkeypatch.delenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", raising=False)
    source_id = _seed_source(db)
    tables = [_table("dbo", "misc", [_col("raw_data", "NVARCHAR")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))

    result = generate_and_save_dictionary(source_id, "user-1")

    for field in ("ai_eligible_count", "ai_processed_count",
                  "ai_suggestions_count", "ai_skipped_due_to_limit"):
        assert field in result, f"'{field}' must be present even when AI is disabled"
        assert result[field] == 0, f"'{field}' must be 0 when AI is disabled"


# ===========================================================================
# Phase 3D Quality Fix — Stricter AI eligibility
# Tests 21–24
# ===========================================================================

from data.dictionary_service import _column_needs_ai
from core.dictionary.generator import ColumnDictEntry


def _make_col_entry(
    semantic_type: str = "other",
    meaning: str = "Stores raw data.",
    pii_risk: bool = False,
) -> ColumnDictEntry:
    """Minimal ColumnDictEntry for _column_needs_ai unit tests."""
    return ColumnDictEntry(
        source_id=1,
        snapshot_id=1,
        table_fqn="dbo.test",
        column_name="col",
        business_label="Col",
        meaning=meaning,
        semantic_type=semantic_type,
        is_metric=False,
        is_dimension=False,
        is_date=False,
        is_id=False,
        pii_risk=pii_risk,
    )


# ---------------------------------------------------------------------------
# Test 21: Moderate confidence + non-generic description → AI NOT called
# ---------------------------------------------------------------------------

def test_moderate_confidence_specific_description_does_not_call_ai(db, monkeypatch):
    """A column with moderate rule-engine confidence (0.42 < 0.75) but a specific,
    non-generic description and sufficient quality must NOT trigger AI.

    Before the fix, semantic_confidence < 0.75 alone was a trigger.
    After the fix, AI only runs when: semantic_type in {other,unknown}
    OR meaning starts with 'Stores ' OR quality_score < 60.
    """
    monkeypatch.setenv("ENABLE_AI_SEMANTIC_INTELLIGENCE", "true")
    source_id = _seed_source(db)
    # 'status' TEXT → rule classifier gives semantic_type='dimension' with
    # a specific "Categorizes ..." description (not "Stores ...").
    tables = [_table("dbo", "orders", [_col("status")])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables))
    prof_id = _seed_profiling_snapshot(db, source_id)
    _seed_col_profile(
        db, prof_id, source_id, "dbo.orders", "status",
        semantic_type="STATUS",
        semantic_confidence=0.42,   # below the old 0.75 threshold
        quality_score=80.0,         # above the 60 quality threshold
        quality_grade="B",
    )

    with patch("core.ai.semantic_intelligence.SemanticIntelligenceService.analyze") as mock_analyze:
        result = generate_and_save_dictionary(source_id, "user-1")

    mock_analyze.assert_not_called()
    assert result["ai_eligible_count"]    == 0
    assert result["ai_suggestions_count"] == 0


# ---------------------------------------------------------------------------
# Test 22: 'Stores ...' description triggers AI regardless of semantic_type
# ---------------------------------------------------------------------------

def test_stores_description_triggers_ai_eligibility():
    """_column_needs_ai must return True when meaning starts with 'Stores '
    even when semantic_type is a non-weak category like 'dimension'."""
    entry = _make_col_entry(semantic_type="dimension", meaning="Stores category data.")
    assert _column_needs_ai(entry, None) is True


# ---------------------------------------------------------------------------
# Test 23: other/unknown semantic type triggers AI eligibility
# ---------------------------------------------------------------------------

def test_other_semantic_type_triggers_ai_eligibility():
    """_column_needs_ai must return True when semantic_type is 'other'."""
    entry_other   = _make_col_entry(semantic_type="other",   meaning="Categorizes x.")
    entry_unknown = _make_col_entry(semantic_type="unknown", meaning="Categorizes x.")
    assert _column_needs_ai(entry_other,   None) is True
    assert _column_needs_ai(entry_unknown, None) is True


# ---------------------------------------------------------------------------
# Test 24: Low quality_score triggers AI; sufficient quality does not
# ---------------------------------------------------------------------------

def test_low_quality_score_triggers_ai_eligibility():
    """_column_needs_ai must return True when quality_score < 60,
    and False when quality_score >= 60 and no other weak signal is present."""
    entry = _make_col_entry(semantic_type="dimension", meaning="Categorizes status.")

    # Low quality → AI eligible
    assert _column_needs_ai(entry, {"quality_score": 35.0}) is True
    assert _column_needs_ai(entry, {"quality_score": 59.9}) is True

    # Sufficient quality and no other weak signal → NOT eligible
    assert _column_needs_ai(entry, {"quality_score": 60.0}) is False
    assert _column_needs_ai(entry, {"quality_score": 95.0}) is False

    # No profiling at all → NOT eligible (dimension + specific description)
    assert _column_needs_ai(entry, None) is False
