"""
Tests for Business Knowledge Graph Phase 2.

Covers get_table_business_context, get_column_business_context,
and get_business_summary. All tests use a temporary SQLite file
and monkeypatch data.db.get_connection so no live DB is needed.

Run from the project root:
    venv/Scripts/pytest tests/test_phase3_business_knowledge.py -v
"""
import json
import os
import sqlite3
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-bkg-phase3-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase3-long-enough-value-1234")


# ---------------------------------------------------------------------------
# Test DB helpers
# ---------------------------------------------------------------------------

def _create_bkg_db(path: str) -> None:
    """Seed a temp SQLite file with every table the service reads from."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE data_source_connections (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               TEXT    NOT NULL,
            display_name          TEXT    NOT NULL DEFAULT 'Test Source',
            source_type           TEXT    NOT NULL DEFAULT 'mssql',
            source_category       TEXT    NOT NULL DEFAULT 'RELATIONAL',
            encrypted_config_json TEXT    NOT NULL DEFAULT '{}',
            config_schema_version INTEGER NOT NULL DEFAULT 1,
            capabilities_json     TEXT    NOT NULL DEFAULT '[]',
            metadata_json         TEXT    NOT NULL DEFAULT '{}',
            source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
            is_active             INTEGER NOT NULL DEFAULT 1,
            last_discovered_at    TEXT,
            last_snapshot_id      INTEGER,
            created_at            TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at            TEXT    NOT NULL DEFAULT '2026-01-01'
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
            discovered_at    TEXT    NOT NULL DEFAULT '2026-01-01',
            created_at       TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE profiling_snapshots (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id                INTEGER NOT NULL,
            schema_snapshot_id       INTEGER NOT NULL DEFAULT 1,
            snapshot_version         INTEGER NOT NULL DEFAULT 1,
            mode                     TEXT    NOT NULL DEFAULT 'full',
            sample_rate              REAL    NOT NULL DEFAULT 1.0,
            profiling_rules_version  TEXT    NOT NULL DEFAULT '1.0.0',
            status                   TEXT    NOT NULL DEFAULT 'COMPLETE',
            tables_total             INTEGER NOT NULL DEFAULT 0,
            tables_profiled          INTEGER NOT NULL DEFAULT 0,
            tables_skipped           INTEGER NOT NULL DEFAULT 0,
            tables_failed            INTEGER NOT NULL DEFAULT 0,
            tables_timed_out         INTEGER NOT NULL DEFAULT 0,
            columns_total            INTEGER NOT NULL DEFAULT 0,
            columns_profiled         INTEGER NOT NULL DEFAULT 0,
            columns_skipped          INTEGER NOT NULL DEFAULT 0,
            total_rows_profiled      INTEGER NOT NULL DEFAULT 0,
            pii_columns_found        INTEGER NOT NULL DEFAULT 0,
            classifications_complete INTEGER NOT NULL DEFAULT 0,
            started_at               TEXT,
            completed_at             TEXT,
            duration_seconds         INTEGER,
            resumable_state_json     TEXT,
            created_at               TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE profiling_table_profiles (
            id                           INTEGER PRIMARY KEY AUTOINCREMENT,
            profiling_snapshot_id        INTEGER NOT NULL,
            source_id                    INTEGER NOT NULL,
            table_fqn                    TEXT    NOT NULL,
            table_name                   TEXT    NOT NULL,
            schema_name                  TEXT    NOT NULL,
            table_type                   TEXT    NOT NULL DEFAULT 'TABLE',
            exact_row_count              INTEGER,
            estimated_row_count          INTEGER,
            row_count_tier               TEXT,
            has_date_column              INTEGER NOT NULL DEFAULT 0,
            date_column_name             TEXT,
            earliest_record              TEXT,
            latest_record                TEXT,
            data_span_days               INTEGER,
            data_currency                TEXT,
            column_count                 INTEGER NOT NULL DEFAULT 0,
            pk_column_count              INTEGER NOT NULL DEFAULT 0,
            fk_count                     INTEGER NOT NULL DEFAULT 0,
            referenced_by_count          INTEGER NOT NULL DEFAULT 0,
            is_junction_table            INTEGER NOT NULL DEFAULT 0,
            is_root_table                INTEGER NOT NULL DEFAULT 0,
            is_leaf_table                INTEGER NOT NULL DEFAULT 0,
            has_identity_column          INTEGER NOT NULL DEFAULT 0,
            avg_null_percentage          REAL,
            completeness_score           REAL,
            table_class                  TEXT,
            classification_confidence    REAL,
            classification_evidence_json TEXT,
            competing_classes_json       TEXT,
            classification_rule_version  TEXT,
            pii_column_count             INTEGER NOT NULL DEFAULT 0,
            confirmed_pii_count          INTEGER NOT NULL DEFAULT 0,
            profiling_depth              TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
            profiling_duration_ms        INTEGER,
            profiling_status             TEXT    NOT NULL DEFAULT 'COMPLETE',
            skip_reason                  TEXT,
            profiled_at                  TEXT,
            created_at                   TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at                   TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE profiling_column_profiles (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            profiling_snapshot_id INTEGER NOT NULL,
            source_id             INTEGER NOT NULL,
            table_fqn             TEXT    NOT NULL,
            column_name           TEXT    NOT NULL,
            data_type             TEXT    NOT NULL DEFAULT 'INTEGER',
            raw_type              TEXT,
            is_nullable           INTEGER NOT NULL DEFAULT 1,
            is_primary_key        INTEGER NOT NULL DEFAULT 0,
            is_identity           INTEGER NOT NULL DEFAULT 0,
            ordinal_position      INTEGER NOT NULL DEFAULT 1,
            null_count            INTEGER,
            null_percentage       REAL,
            populated_count       INTEGER,
            populated_percentage  REAL,
            empty_string_count    INTEGER,
            zero_count            INTEGER,
            distinct_count        INTEGER,
            distinct_percentage   REAL,
            uniqueness_score      REAL,
            cardinality_tier      TEXT,
            min_value             TEXT,
            max_value             TEXT,
            min_length            INTEGER,
            max_length_observed   INTEGER,
            avg_length            REAL,
            mean_value            REAL,
            std_deviation         REAL,
            p5_value              TEXT,
            p95_value             TEXT,
            dominant_pattern      TEXT,
            pattern_coverage      REAL,
            email_match_rate      REAL,
            phone_match_rate      REAL,
            guid_match_rate       REAL,
            date_string_rate      REAL,
            numeric_string_rate   REAL,
            masked_value_rate     REAL,
            semantic_type         TEXT,
            semantic_confidence   REAL,
            semantic_evidence_json TEXT,
            semantic_rule_version TEXT,
            pii_name_heuristic    INTEGER NOT NULL DEFAULT 0,
            pii_confirmed         INTEGER NOT NULL DEFAULT 0,
            pii_signals_json      TEXT,
            pii_classification            TEXT,
            pii_classification_confidence REAL,
            pii_classification_evidence   TEXT,
            pii_classification_source     TEXT,
            pii_classified_at             TEXT,
            pii_manual_override   INTEGER NOT NULL DEFAULT 0,
            top_values_coverage   REAL,
            profiling_depth       TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
            profiling_duration_ms INTEGER,
            profiling_status      TEXT    NOT NULL DEFAULT 'COMPLETE',
            created_at            TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at            TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE data_dictionary_tables (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL,
            snapshot_id       INTEGER NOT NULL DEFAULT 1,
            table_fqn         TEXT    NOT NULL,
            table_name        TEXT    NOT NULL,
            schema_name       TEXT    NOT NULL DEFAULT 'dbo',
            table_type        TEXT    NOT NULL DEFAULT 'TABLE',
            business_name     TEXT,
            description       TEXT,
            domain            TEXT,
            grain             TEXT,
            is_approved       INTEGER NOT NULL DEFAULT 0,
            approved_by       TEXT,
            approved_at       TEXT,
            generation_method TEXT    NOT NULL DEFAULT 'rule_based',
            created_at        TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at        TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE data_dictionary_columns (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL,
            snapshot_id       INTEGER NOT NULL DEFAULT 1,
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
            created_at        TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at        TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE domain_assignments (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id             INTEGER NOT NULL,
            profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
            table_fqn             TEXT    NOT NULL,
            domain                TEXT    NOT NULL,
            confidence            REAL    NOT NULL DEFAULT 0.0,
            evidence_json         TEXT    NOT NULL DEFAULT '[]',
            competing_domains_json TEXT   NOT NULL DEFAULT '[]',
            created_at            TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at            TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE entity_assignments (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id               INTEGER NOT NULL,
            profiling_snapshot_id   INTEGER NOT NULL DEFAULT 1,
            table_fqn               TEXT    NOT NULL,
            entity                  TEXT    NOT NULL,
            confidence              REAL    NOT NULL DEFAULT 0.0,
            evidence_json           TEXT    NOT NULL DEFAULT '[]',
            competing_entities_json TEXT    NOT NULL DEFAULT '[]',
            created_at              TEXT    NOT NULL DEFAULT '2026-01-01',
            updated_at              TEXT    NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE table_relationships (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id         INTEGER NOT NULL,
            snapshot_id       INTEGER NOT NULL,
            from_schema       TEXT    NOT NULL,
            from_table        TEXT    NOT NULL,
            from_table_fqn    TEXT    NOT NULL,
            from_column       TEXT    NOT NULL,
            to_schema         TEXT    NOT NULL,
            to_table          TEXT    NOT NULL,
            to_table_fqn      TEXT    NOT NULL,
            to_column         TEXT    NOT NULL,
            relationship_name TEXT,
            relationship_type TEXT    NOT NULL DEFAULT 'FOREIGN_KEY',
            confidence        REAL    NOT NULL DEFAULT 1.0,
            evidence_json     TEXT,
            created_at        TEXT    NOT NULL DEFAULT '2026-01-01',
            relationship_status TEXT  NOT NULL DEFAULT 'AUTO'
        );
        CREATE UNIQUE INDEX idx_tr_snapshot_uniq
            ON table_relationships (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
    """)
    conn.commit()
    conn.close()


def _seed_full_scenario(path: str) -> dict:
    """
    Seed one source, one table (dbo.orders), two columns, with all metadata layers.
    Returns a dict of IDs for assertions.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    conn.execute("INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) VALUES (1, 'user-X', 'Prod DB', 'mssql', 'RELATIONAL')")
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count, view_count, column_count) VALUES (1, 1, 2, 0, 4)")
    conn.execute("INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, tables_profiled, columns_profiled, pii_columns_found) VALUES (1, 1, 1, 2, 4, 1)")

    # Profiling table profile
    conn.execute("""INSERT INTO profiling_table_profiles
        (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_type,
         estimated_row_count, row_count_tier, fk_count, referenced_by_count,
         is_junction_table, is_root_table, is_leaf_table, has_identity_column,
         completeness_score, table_class, classification_confidence,
         pii_column_count, confirmed_pii_count, profiling_depth, profiling_status)
        VALUES (1, 1, 'dbo.orders', 'orders', 'dbo', 'TABLE',
                5000, 'MEDIUM', 2, 0,
                0, 0, 1, 0,
                0.98, 'TRANSACTION', 0.9,
                1, 0, 'STRUCTURAL_ONLY', 'COMPLETE')""")

    # Profiling column profiles
    conn.execute("""INSERT INTO profiling_column_profiles
        (profiling_snapshot_id, source_id, table_fqn, column_name,
         data_type, raw_type, is_nullable, is_primary_key, ordinal_position,
         distinct_count, null_percentage, cardinality_tier,
         semantic_type, semantic_confidence, pii_name_heuristic, pii_confirmed)
        VALUES (1, 1, 'dbo.orders', 'id',
                'INTEGER', 'int', 0, 1, 1,
                5000, 0.0, 'HIGH',
                'ID', 0.95, 0, 0)""")
    conn.execute("""INSERT INTO profiling_column_profiles
        (profiling_snapshot_id, source_id, table_fqn, column_name,
         data_type, raw_type, is_nullable, is_primary_key, ordinal_position,
         distinct_count, null_percentage, cardinality_tier,
         semantic_type, semantic_confidence, pii_name_heuristic, pii_confirmed)
        VALUES (1, 1, 'dbo.orders', 'customer_id',
                'INTEGER', 'int', 0, 0, 2,
                200, 0.0, 'MEDIUM',
                'ID', 0.85, 1, 0)""")

    # Dictionary
    conn.execute("""INSERT INTO data_dictionary_tables
        (source_id, snapshot_id, table_fqn, table_name, schema_name, table_type,
         business_name, description, grain, is_approved, generation_method)
        VALUES (1, 1, 'dbo.orders', 'orders', 'dbo', 'TABLE',
                'Customer Orders', 'Stores all customer orders', 'One row per order',
                0, 'rule_based')""")
    conn.execute("""INSERT INTO data_dictionary_columns
        (source_id, snapshot_id, table_fqn, column_name,
         business_label, meaning, semantic_type, is_id, pii_risk, is_approved, generation_method)
        VALUES (1, 1, 'dbo.orders', 'id',
                'Order ID', 'Unique order identifier', 'ID', 1, 0, 1, 'rule_based')""")
    conn.execute("""INSERT INTO data_dictionary_columns
        (source_id, snapshot_id, table_fqn, column_name,
         business_label, meaning, semantic_type, is_id, pii_risk, is_approved, generation_method)
        VALUES (1, 1, 'dbo.orders', 'customer_id',
                'Customer ID', 'FK to customers table', 'ID', 1, 0, 0, 'rule_based')""")

    # Domain
    conn.execute("""INSERT INTO domain_assignments
        (source_id, profiling_snapshot_id, table_fqn, domain, confidence, evidence_json)
        VALUES (1, 1, 'dbo.orders', 'Sales', 0.88, '["table name matches sales pattern"]')""")

    # Entity
    conn.execute("""INSERT INTO entity_assignments
        (source_id, profiling_snapshot_id, table_fqn, entity, confidence, evidence_json)
        VALUES (1, 1, 'dbo.orders', 'Order', 0.92, '["column patterns match order entity"]')""")

    # Relationships
    conn.execute("""INSERT INTO table_relationships
        (source_id, snapshot_id, from_schema, from_table, from_table_fqn,
         from_column, to_schema, to_table, to_table_fqn, to_column,
         relationship_name, relationship_type, confidence)
        VALUES (1, 1, 'dbo', 'orders', 'dbo.orders',
                'customer_id', 'dbo', 'customers', 'dbo.customers', 'id',
                'FK_orders_customers', 'FOREIGN_KEY', 1.0)""")

    conn.commit()
    conn.close()
    return {"source_id": 1, "schema_snap_id": 1, "prof_snap_id": 1}


def _db_conn(path: str):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# Import service under test
# ---------------------------------------------------------------------------

from data.business_knowledge_service import (
    get_table_business_context,
    get_column_business_context,
    get_business_summary,
)
from data.profiling_snapshot_resolver import (
    get_latest_profiling_snapshot as _real_get_latest_profiling_snapshot,
    get_latest_profiling_snapshot_detail as _real_get_latest_profiling_snapshot_detail,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# T1 — table business context composes source, dictionary, domain, entity, profiling
def test_table_business_context_basic_composition(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    assert ctx is not None
    # Source section
    assert ctx["source"]["source_type"] == "mssql"
    assert ctx["source"]["display_name"] == "Prod DB"
    # Table identity
    assert ctx["table"]["table_fqn"] == "dbo.orders"
    assert ctx["table"]["table_name"] == "orders"
    assert ctx["table"]["schema_name"] == "dbo"


# T2 — dictionary inclusion: business name, description, approval state
def test_table_business_context_includes_dictionary(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_dict.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    assert ctx["dictionary"] is not None
    assert ctx["dictionary"]["business_name"] == "Customer Orders"
    assert ctx["dictionary"]["description"] == "Stores all customer orders"
    assert ctx["dictionary"]["is_approved"] is False
    assert ctx["dictionary"]["generation_method"] == "rule_based"


# T3 — domain and entity inclusion
def test_table_business_context_includes_domain_and_entity(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_de.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    assert ctx["domain"] is not None
    assert ctx["domain"]["domain"] == "Sales"
    assert ctx["domain"]["confidence"] == 0.88

    assert ctx["entity"] is not None
    assert ctx["entity"]["entity"] == "Order"
    assert ctx["entity"]["confidence"] == 0.92


# T4 — profiling inclusion: table class, row count, PII, classification
def test_table_business_context_includes_profiling(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_prof.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    prof = ctx["profiling"]
    assert prof is not None
    assert prof["table_class"] == "TRANSACTION"
    assert prof["classification_confidence"] == 0.9
    assert prof["estimated_row_count"] == 5000
    assert prof["fk_count"] == 2
    assert prof["is_leaf_table"] is True
    assert prof["profiling_status"] == "COMPLETE"


# T5 — relationship inclusion: outbound FK to dbo.customers
def test_table_business_context_includes_relationships(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_rel.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    rels = ctx["relationships"]
    assert rels is not None
    assert len(rels["outbound"]) == 1
    assert rels["outbound"][0]["to_table_fqn"] == "dbo.customers"
    assert rels["outbound"][0]["from_column"] == "customer_id"
    assert rels["inbound"] == []


# T6 — column business context composition
def test_column_business_context(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_col.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_column_business_context(
        source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
    )

    assert ctx is not None
    assert ctx["column_name"] == "id"
    assert ctx["table_fqn"] == "dbo.orders"
    # Schema section
    assert ctx["schema"]["data_type"] == "INTEGER"
    assert ctx["schema"]["is_primary_key"] is True
    # Dictionary section
    assert ctx["dictionary"]["business_label"] == "Order ID"
    assert ctx["dictionary"]["is_approved"] is True
    # Profiling section
    assert ctx["profiling"]["semantic_type"] == "ID"
    assert ctx["profiling"]["semantic_confidence"] == 0.95
    # Parent table context
    assert ctx["table_context"]["domain"] == "Sales"
    assert ctx["table_context"]["entity"] == "Order"
    # Evidence built from existing data
    assert any("Order ID" in e for e in ctx["evidence"])


# T7 — business summary aggregates all metadata layers
def test_business_summary(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_sum.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-X")

    assert summary is not None
    assert summary["source"]["source_type"] == "mssql"
    # Schema counts from snapshot
    assert summary["schema"]["table_count"] == 2
    # Dictionary coverage
    assert summary["dictionary"]["tables_with_definitions"] == 1
    assert summary["dictionary"]["columns_with_labels"] == 2
    assert summary["dictionary"]["columns_approved"] == 1
    # Domain
    assert summary["domains"]["tables_assigned"] == 1
    assert "Sales" in summary["domains"]["distribution"]
    # Entity
    assert summary["entities"]["tables_assigned"] == 1
    assert "Order" in summary["entities"]["distribution"]
    # Relationships
    assert summary["relationships"]["total_relationships"] == 1
    assert summary["relationships"]["tables_with_fks"] == 1
    # PII: customer_id has pii_name_heuristic=1, pii_confirmed=0
    assert summary["pii"]["columns_flagged"] == 1
    assert summary["pii"]["columns_pending_review"] == 1
    # Coverage scores are between 0 and 1
    assert 0 <= summary["coverage"]["readiness_score"] <= 1


# T8 — graceful handling when metadata layers are absent (no profiling, no dict)
def test_graceful_missing_metadata(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_missing.db")
    _create_bkg_db(db)

    # Only seed source and schema snapshot — no profiling, dictionary, domain, entity, relationships
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO data_source_connections (id, user_id, source_type, source_category) VALUES (2, 'user-Y', 'mssql', 'RELATIONAL')")
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (2, 2, 3)")
    conn.commit()
    conn.close()

    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=2, user_id="user-Y", table_fqn="dbo.orders")
    assert ctx is not None
    assert ctx["dictionary"] is None
    assert ctx["domain"] is None
    assert ctx["entity"] is None
    assert ctx["profiling"] is None
    assert ctx["relationships"]["outbound"] == []
    assert ctx["relationships"]["inbound"] == []
    assert ctx["columns"] == []
    assert ctx["metadata_completeness"]["completeness_score"] == 0.0

    # Column context with no profiling or dictionary
    col_ctx = get_column_business_context(source_id=2, user_id="user-Y", table_fqn="dbo.orders", column_name="id")
    assert col_ctx is not None
    assert col_ctx["schema"] is None
    assert col_ctx["dictionary"] is None
    assert col_ctx["profiling"] is None
    assert col_ctx["confidence"] is None
    assert col_ctx["evidence"] == []

    # Summary with only schema data
    summary = get_business_summary(source_id=2, user_id="user-Y")
    assert summary is not None
    assert summary["schema"]["table_count"] == 3
    assert summary["dictionary"]["tables_with_definitions"] == 0
    assert summary["domains"]["tables_assigned"] == 0
    assert summary["relationships"]["total_relationships"] == 0
    assert summary["pii"]["columns_flagged"] == 0

    # Unknown source returns None (not found / not owned)
    ctx_not_found = get_table_business_context(source_id=99, user_id="user-Y", table_fqn="dbo.orders")
    assert ctx_not_found is None


# ---------------------------------------------------------------------------
# Phase 2A.1 — resolver-migration regression guards
#
# _latest_profiling_snap_id now delegates its lookup to
# data.profiling_snapshot_resolver.get_latest_profiling_snapshot. That
# resolver's policy is status-independent (highest snapshot_version wins,
# regardless of status) — these tests prove the migration preserved that
# exact pre-existing behavior.
# ---------------------------------------------------------------------------

def _seed_minimal_source(db: str, source_id: int = 1, user_id: str = "user-bkg") -> None:
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (?, ?, 'mssql', 'RELATIONAL')",
        (source_id, user_id),
    )
    conn.commit()
    conn.close()


def _insert_profiling_snapshot(
    db: str, source_id: int, snapshot_version: int, status: str = "COMPLETE",
) -> int:
    conn = _db_conn(db)
    cur = conn.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (?, 1, ?, ?)",
        (source_id, snapshot_version, status),
    )
    conn.commit()
    snap_id = cur.lastrowid
    conn.close()
    return snap_id


# ---------------------------------------------------------------------------
# Phase 2A.2 — resolver-migration regression guards (get_business_summary)
#
# get_business_summary now delegates its profiling-snapshot lookup to
# data.profiling_snapshot_resolver.get_latest_profiling_snapshot_detail. That
# resolver's policy is status-independent (highest snapshot_version wins,
# regardless of status) — these tests prove the migration preserved that
# exact pre-existing behavior and field mapping.
# ---------------------------------------------------------------------------

def _insert_profiling_snapshot_detail(
    db: str,
    source_id: int,
    snapshot_version: int,
    status: str = "COMPLETE",
    mode: str = "full",
    completed_at: str | None = None,
    tables_profiled: int = 0,
    columns_profiled: int = 0,
    pii_columns_found: int = 0,
) -> int:
    conn = _db_conn(db)
    cur = conn.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, status, mode, "
        "completed_at, tables_profiled, columns_profiled, pii_columns_found) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, snapshot_version, status, mode, completed_at,
         tables_profiled, columns_profiled, pii_columns_found),
    )
    conn.commit()
    snap_id = cur.lastrowid
    conn.close()
    return snap_id


def _insert_schema_snapshot(db: str, source_id: int, table_count: int = 0) -> int:
    conn = _db_conn(db)
    cur = conn.execute(
        "INSERT INTO schema_snapshots (source_id, table_count) VALUES (?, ?)",
        (source_id, table_count),
    )
    conn.commit()
    snap_id = cur.lastrowid
    conn.close()
    return snap_id


def test_business_summary_delegates_to_detail_resolver(tmp_path, monkeypatch):
    """Proves the migration actually happened at runtime, not just in source."""
    db = str(tmp_path / "bkg_p2a2_a.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.business_knowledge_service.get_latest_profiling_snapshot_detail",
        wraps=_real_get_latest_profiling_snapshot_detail,
    ) as spy:
        summary = get_business_summary(source_id=1, user_id="user-bkg")

    spy.assert_called_once_with(1)
    assert summary is not None


def test_business_summary_counters_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_b.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_schema_snapshot(db, source_id=1, table_count=10)
    _insert_profiling_snapshot_detail(
        db, source_id=1, snapshot_version=1,
        tables_profiled=7, columns_profiled=42, pii_columns_found=3,
    )
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")

    assert summary["profiling"]["tables_profiled"] == 7
    assert summary["profiling"]["columns_profiled"] == 42
    assert summary["profiling"]["pii_columns_found"] == 3
    assert summary["coverage"]["profile_coverage"] == 0.7


def test_business_summary_status_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_c.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1, status="RUNNING")
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")
    assert summary["profiling"]["status"] == "RUNNING"


def test_business_summary_mode_not_surfaced_but_resolved_safely(tmp_path, monkeypatch):
    """mode is part of ProfilingSnapshotDetail and read by the resolver, but
    get_business_summary's response contract has never surfaced it directly —
    this guards that the migration didn't start leaking it or break on it."""
    db = str(tmp_path / "bkg_p2a2_d.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1, mode="STRUCTURAL_ONLY")
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")
    assert summary is not None
    assert "mode" not in summary["profiling"]


def test_business_summary_completed_at_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_e.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_profiling_snapshot_detail(
        db, source_id=1, snapshot_version=1, completed_at="2026-03-01T12:00:00",
    )
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")
    assert summary["profiling"]["completed_at"] == "2026-03-01T12:00:00"


def test_business_summary_latest_snapshot_version_wins(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_f.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1, tables_profiled=1)
    newest_id = _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=2, tables_profiled=9)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")
    assert summary["profiling"]["snapshot_id"] == newest_id
    assert summary["profiling"]["tables_profiled"] == 9


def test_business_summary_older_complete_beats_newer_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_g.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    complete_id = _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=2, status="CANCELLED")
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")
    # The resolver must prefer the older COMPLETE snapshot over the newer
    # CANCELLED one — a newer non-COMPLETE snapshot must never shadow older,
    # usable COMPLETE profiling evidence.
    assert summary["profiling"]["snapshot_id"] == complete_id
    assert summary["profiling"]["status"] == "COMPLETE"


def test_business_summary_older_complete_beats_newer_running(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_h.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    complete_id = _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=2, status="RUNNING")
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")
    # The resolver must prefer the older COMPLETE snapshot over the newer
    # RUNNING one.
    assert summary["profiling"]["snapshot_id"] == complete_id
    assert summary["profiling"]["status"] == "COMPLETE"


def test_business_summary_no_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_i.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    summary = get_business_summary(source_id=1, user_id="user-bkg")

    assert summary is not None
    assert summary["profiling"] == {
        "snapshot_id": None,
        "tables_profiled": 0,
        "columns_profiled": 0,
        "pii_columns_found": 0,
        "status": None,
        "completed_at": None,
    }
    assert summary["coverage"]["profile_coverage"] == 0.0


def test_business_summary_no_writes(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2a2_j.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    _insert_schema_snapshot(db, source_id=1, table_count=5)
    _insert_profiling_snapshot_detail(db, source_id=1, snapshot_version=1, tables_profiled=3)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _snapshot_tables(conn):
        return {
            t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
            for t in ("data_source_connections", "schema_snapshots", "profiling_snapshots")
        }

    conn = _db_conn(db)
    before = _snapshot_tables(conn)
    conn.close()

    get_business_summary(source_id=1, user_id="user-bkg")
    get_business_summary(source_id=999, user_id="nobody")  # no-source path too

    conn = _db_conn(db)
    after = _snapshot_tables(conn)
    conn.close()

    assert before == after


# ---------------------------------------------------------------------------
# Phase 2C.1 — get_table_business_context() call-site cleanup
#
# The wrapper _latest_profiling_snap_id() already delegated to
# get_latest_profiling_snapshot() (Phase 2A.1) — this phase removes the
# unnecessary indirection from get_table_business_context()'s call site
# only. get_column_business_context() still calls the wrapper (untouched).
# No behavior or connection-count change is expected: the wrapper was
# already opening its own resolver connection via get_latest_profiling_snapshot();
# calling that function directly is a pure rename at this call site.
# ---------------------------------------------------------------------------

# 1 — get_latest_profiling_snapshot() called exactly once
def test_table_business_context_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c1_a.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.business_knowledge_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    assert ctx is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_table_business_context_resolver_called_with_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c1_b.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.business_knowledge_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by get_table_business_context()
#
# Phase 2C.3 deleted _latest_profiling_snap_id()'s definition outright — it
# had zero remaining production call sites after Phases 2C.1/2C.2 moved both
# consumers onto the shared resolver directly. The prior version of this
# test proved the wrapper wasn't called by monkeypatching it to raise; that
# mechanism no longer works once the attribute doesn't exist (monkeypatch on
# a missing attribute raises AttributeError by default). See
# test_business_knowledge_service_has_no_legacy_snapshot_helper and
# test_table_business_context_still_uses_shared_resolver below for the
# structural replacement coverage.


# 4 — returned business context unchanged
def test_table_business_context_returned_context_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c1_d.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    assert ctx is not None
    assert ctx["source"]["source_type"] == "mssql"
    assert ctx["source"]["display_name"] == "Prod DB"
    assert ctx["table"]["table_fqn"] == "dbo.orders"
    assert ctx["dictionary"]["business_name"] == "Customer Orders"
    assert ctx["domain"]["domain"] == "Sales"
    assert ctx["entity"]["entity"] == "Order"
    prof = ctx["profiling"]
    assert prof is not None
    assert prof["table_class"] == "TRANSACTION"
    assert prof["classification_confidence"] == 0.9
    assert prof["estimated_row_count"] == 5000
    assert prof["fk_count"] == 2
    assert prof["pii_column_count"] == 1
    assert ctx["metadata_completeness"]["has_profiling"] is True
    assert len(ctx["columns"]) == 2


# 5 — no snapshot behavior unchanged
def test_table_business_context_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c1_e.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-bkg", table_fqn="dbo.orders")

    assert ctx is not None
    assert ctx["profiling"] is None
    assert ctx["metadata_completeness"]["has_profiling"] is False


# 6 — snapshot policy: older COMPLETE snapshot wins over a newer non-COMPLETE one
def test_table_business_context_snapshot_policy_complete_precedence(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c1_f.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (1, 1, 'dbo.orders', 'orders', 'dbo', 'Old')"
    )
    conn.commit()
    conn.close()
    _insert_profiling_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    cancelled_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class) "
        "VALUES (?, 1, 'dbo.orders', 'orders', 'dbo', 'New')",
        (cancelled_id,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_table_business_context(source_id=1, user_id="user-bkg", table_fqn="dbo.orders")

    # Status-aware selection: the older COMPLETE snapshot (table_class="Old")
    # wins over the newer CANCELLED one (table_class="New") — a newer
    # non-COMPLETE snapshot must never shadow older, usable COMPLETE evidence.
    assert ctx["profiling"]["table_class"] == "Old"


# 7 — read-only behavior preserved
def test_table_business_context_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c1_g.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _snapshot_tables(conn):
        return {
            t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
            for t in (
                "data_source_connections", "schema_snapshots", "profiling_snapshots",
                "profiling_table_profiles", "profiling_column_profiles",
            )
        }

    conn = _db_conn(db)
    before = _snapshot_tables(conn)
    conn.close()

    get_table_business_context(source_id=1, user_id="user-X", table_fqn="dbo.orders")

    conn = _db_conn(db)
    after = _snapshot_tables(conn)
    conn.close()

    assert before == after


# ---------------------------------------------------------------------------
# Phase 2C.2 — get_column_business_context() call-site cleanup
#
# The last production caller of the wrapper _latest_profiling_snap_id().
# Same reasoning as Phase 2C.1: the wrapper already delegated to
# get_latest_profiling_snapshot() (Phase 2A.1), so this is a pure call-site
# rename — no behavior or connection-count change, since the wrapper was
# already opening its own resolver connection.
# ---------------------------------------------------------------------------

# 1 — resolver called exactly once per invocation
def test_column_business_context_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_a.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.business_knowledge_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        ctx = get_column_business_context(
            source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
        )

    assert ctx is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_column_business_context_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_b.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    with patch(
        "data.business_knowledge_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        get_column_business_context(
            source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
        )

    spy.assert_called_once_with(1)


# 3 — wrapper no longer exists to be called by get_column_business_context()
#
# See the equivalent note above test 3 in the Phase 2C.1 section: this was
# the last production caller of the now-deleted wrapper. Structural
# replacement coverage lives in
# test_business_knowledge_service_has_no_legacy_snapshot_helper and
# test_column_business_context_still_uses_shared_resolver below.


# 4 — return shape unchanged
def test_column_business_context_return_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_d.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_column_business_context(
        source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
    )

    assert ctx is not None
    assert set(ctx.keys()) == {
        "source_id", "table_fqn", "column_name",
        "schema", "dictionary", "profiling", "table_context", "evidence", "confidence",
    }


# 5 — profile values unchanged
def test_column_business_context_profile_values_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_e.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_column_business_context(
        source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
    )

    assert ctx["column_name"] == "id"
    assert ctx["table_fqn"] == "dbo.orders"
    assert ctx["schema"]["data_type"] == "INTEGER"
    assert ctx["schema"]["is_primary_key"] is True
    assert ctx["dictionary"]["business_label"] == "Order ID"
    assert ctx["dictionary"]["is_approved"] is True
    assert ctx["profiling"]["semantic_type"] == "ID"
    assert ctx["profiling"]["semantic_confidence"] == 0.95
    assert ctx["table_context"]["domain"] == "Sales"
    assert ctx["table_context"]["entity"] == "Order"
    assert any("Order ID" in e for e in ctx["evidence"])


# 6 — no-snapshot behavior unchanged
def test_column_business_context_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_f.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_column_business_context(
        source_id=1, user_id="user-bkg", table_fqn="dbo.orders", column_name="id"
    )

    assert ctx is not None
    assert ctx["schema"] is None
    assert ctx["profiling"] is None


# 7 — snapshot policy: older COMPLETE snapshot wins over a newer non-COMPLETE one
def test_column_business_context_snapshot_policy_complete_precedence(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_g.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, semantic_type) "
        "VALUES (1, 1, 'dbo.orders', 'id', 'Old')"
    )
    conn.commit()
    conn.close()
    _insert_profiling_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    cancelled_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, semantic_type) "
        "VALUES (?, 1, 'dbo.orders', 'id', 'CancelledNew')",
        (cancelled_id,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_column_business_context(
        source_id=1, user_id="user-bkg", table_fqn="dbo.orders", column_name="id"
    )

    # Status-aware selection: the older COMPLETE snapshot ("Old") wins over
    # the newer CANCELLED one ("CancelledNew") — a newer non-COMPLETE
    # snapshot must never shadow older, usable COMPLETE evidence.
    assert ctx["profiling"]["semantic_type"] == "Old"


def test_column_business_context_snapshot_policy_running_precedence(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_h.db")
    _create_bkg_db(db)
    _seed_minimal_source(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, semantic_type) "
        "VALUES (1, 1, 'dbo.orders', 'id', 'Old')"
    )
    conn.commit()
    conn.close()
    _insert_profiling_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    running_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="RUNNING")
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, semantic_type) "
        "VALUES (?, 1, 'dbo.orders', 'id', 'RunningNew')",
        (running_id,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    ctx = get_column_business_context(
        source_id=1, user_id="user-bkg", table_fqn="dbo.orders", column_name="id"
    )

    # Status-aware selection: the older COMPLETE snapshot ("Old") wins over
    # the newer RUNNING one ("RunningNew").
    assert ctx["profiling"]["semantic_type"] == "Old"


# 8 — read-only behavior preserved
def test_column_business_context_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_i.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.business_knowledge_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

    def _snapshot_tables(conn):
        return {
            t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
            for t in (
                "data_source_connections", "schema_snapshots", "profiling_snapshots",
                "profiling_table_profiles", "profiling_column_profiles",
            )
        }

    conn = _db_conn(db)
    before = _snapshot_tables(conn)
    conn.close()

    get_column_business_context(
        source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
    )

    conn = _db_conn(db)
    after = _snapshot_tables(conn)
    conn.close()

    assert before == after


# 9 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_column_business_context_resolver_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "bkg_p2c2_j.db")
    _create_bkg_db(db)
    _seed_full_scenario(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.business_knowledge_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    ctx = get_column_business_context(
        source_id=1, user_id="user-X", table_fqn="dbo.orders", column_name="id"
    )

    assert ctx is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2C.3 — Business Knowledge wrapper retirement
#
# _latest_profiling_snap_id() had zero remaining production call sites after
# Phases 2C.1/2C.2 moved both get_table_business_context() and
# get_column_business_context() onto get_latest_profiling_snapshot()
# directly. This phase deletes the dead wrapper's definition outright.
# Mirrors the Phase 2B.8 precedent in tests/test_phase6_semantic.py for
# data.semantic_layer_service's identically-named (but separately scoped)
# wrapper.
# ---------------------------------------------------------------------------

# 1 — business_knowledge_service_has_no_legacy_snapshot_helper
def test_business_knowledge_service_has_no_legacy_snapshot_helper():
    import data.business_knowledge_service as bks_module
    assert not hasattr(bks_module, "_latest_profiling_snap_id")


# 2 — table_business_context_still_uses_shared_resolver
def test_table_business_context_still_uses_shared_resolver():
    import inspect
    import data.business_knowledge_service as bks_module

    source = inspect.getsource(bks_module.get_table_business_context)
    assert "get_latest_profiling_snapshot(" in source
    assert "_latest_profiling_snap_id(" not in source


# 3 — column_business_context_still_uses_shared_resolver
def test_column_business_context_still_uses_shared_resolver():
    import inspect
    import data.business_knowledge_service as bks_module

    source = inspect.getsource(bks_module.get_column_business_context)
    assert "get_latest_profiling_snapshot(" in source
    assert "_latest_profiling_snap_id(" not in source


# 4 — business_knowledge_source_has_no_legacy_helper_reference
def test_business_knowledge_source_has_no_legacy_helper_reference():
    import inspect
    import data.business_knowledge_service as bks_module

    source = inspect.getsource(bks_module)
    assert "_latest_profiling_snap_id" not in source
