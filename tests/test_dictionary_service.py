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
from unittest.mock import patch

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
