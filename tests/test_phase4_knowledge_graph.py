"""
Tests for Knowledge Graph Reasoning Engine — Phase 4.

Scenario:
    dbo.customers  ← dbo.orders  ← dbo.invoices  ← dbo.payments
    (customers is referenced by orders, orders by invoices, invoices by payments)

    dbo.products  — isolated, no FK relationships

Domain:  customers + orders → Sales    |  invoices + payments → Finance
Entity:  one unique entity per table

Run from the project root:
    venv/Scripts/pytest tests/test_phase4_knowledge_graph.py -v
"""
import json
import os
import sqlite3
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-kg-phase4-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase4-long-enough-value-1234")

from data.knowledge_graph_service import (
    get_related_tables,
    find_business_assets,
    explain_table,
    trace_business_path,
    knowledge_graph_summary,
)
from data.profiling_snapshot_resolver import (
    get_latest_profiling_snapshot as _real_get_latest_profiling_snapshot,
)


# ---------------------------------------------------------------------------
# Test DB schema
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT    NOT NULL,
        display_name          TEXT    NOT NULL DEFAULT 'Test',
        source_type           TEXT    NOT NULL DEFAULT 'mssql',
        source_category       TEXT    NOT NULL DEFAULT 'RELATIONAL',
        encrypted_config_json TEXT    NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json     TEXT    NOT NULL DEFAULT '[]',
        metadata_json         TEXT    NOT NULL DEFAULT '{}',
        source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
        is_active             INTEGER NOT NULL DEFAULT 1,
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
        columns_total            INTEGER NOT NULL DEFAULT 0,
        columns_profiled         INTEGER NOT NULL DEFAULT 0,
        pii_columns_found        INTEGER NOT NULL DEFAULT 0,
        tables_skipped           INTEGER NOT NULL DEFAULT 0,
        tables_failed            INTEGER NOT NULL DEFAULT 0,
        tables_timed_out         INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled      INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at               TEXT,
        completed_at             TEXT,
        duration_seconds         INTEGER,
        resumable_state_json     TEXT,
        created_at               TEXT    NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_table_profiles (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id     INTEGER NOT NULL,
        source_id                 INTEGER NOT NULL,
        table_fqn                 TEXT    NOT NULL,
        table_name                TEXT    NOT NULL,
        schema_name               TEXT    NOT NULL DEFAULT 'dbo',
        table_type                TEXT    NOT NULL DEFAULT 'TABLE',
        exact_row_count           INTEGER,
        estimated_row_count       INTEGER,
        row_count_tier            TEXT,
        has_date_column           INTEGER NOT NULL DEFAULT 0,
        date_column_name          TEXT,
        earliest_record           TEXT,
        latest_record             TEXT,
        data_span_days            INTEGER,
        data_currency             TEXT,
        column_count              INTEGER NOT NULL DEFAULT 0,
        pk_column_count           INTEGER NOT NULL DEFAULT 0,
        fk_count                  INTEGER NOT NULL DEFAULT 0,
        referenced_by_count       INTEGER NOT NULL DEFAULT 0,
        is_junction_table         INTEGER NOT NULL DEFAULT 0,
        is_root_table             INTEGER NOT NULL DEFAULT 0,
        is_leaf_table             INTEGER NOT NULL DEFAULT 0,
        has_identity_column       INTEGER NOT NULL DEFAULT 0,
        avg_null_percentage       REAL,
        completeness_score        REAL,
        table_class               TEXT,
        classification_confidence REAL,
        classification_evidence_json TEXT,
        competing_classes_json    TEXT,
        classification_rule_version TEXT,
        pii_column_count          INTEGER NOT NULL DEFAULT 0,
        confirmed_pii_count       INTEGER NOT NULL DEFAULT 0,
        profiling_depth           TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms     INTEGER,
        profiling_status          TEXT    NOT NULL DEFAULT 'COMPLETE',
        skip_reason               TEXT,
        profiled_at               TEXT,
        created_at                TEXT    NOT NULL DEFAULT '2026-01-01',
        updated_at                TEXT    NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_column_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id             INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        column_name           TEXT    NOT NULL,
        data_type             TEXT    NOT NULL DEFAULT 'TEXT',
        raw_type              TEXT,
        is_nullable           INTEGER NOT NULL DEFAULT 1,
        is_primary_key        INTEGER NOT NULL DEFAULT 0,
        is_identity           INTEGER NOT NULL DEFAULT 0,
        ordinal_position      INTEGER NOT NULL DEFAULT 1,
        null_percentage       REAL,
        distinct_count        INTEGER,
        distinct_percentage   REAL,
        uniqueness_score      REAL,
        cardinality_tier      TEXT,
        min_value             TEXT,
        max_value             TEXT,
        avg_length            REAL,
        mean_value            REAL,
        std_deviation         REAL,
        null_count            INTEGER,
        populated_count       INTEGER,
        populated_percentage  REAL,
        empty_string_count    INTEGER,
        zero_count            INTEGER,
        min_length            INTEGER,
        max_length_observed   INTEGER,
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
        inference_method  TEXT    NOT NULL DEFAULT 'declared_fk',
        relationship_status TEXT  NOT NULL DEFAULT 'AUTO'
    );
    CREATE UNIQUE INDEX idx_tr_uniq ON table_relationships
        (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
"""


def _create_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _seed_full_scenario(path: str) -> None:
    """
    Seed the test scenario:
      customers ← orders ← invoices ← payments  (FK chain)
      products                                   (isolated)
    """
    conn = _conn(path)

    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-kg', 'KG Test DB', 'mssql', 'RELATIONAL')"
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count, view_count, column_count) "
        "VALUES (1, 1, 5, 0, 20)"
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, tables_profiled, columns_profiled, pii_columns_found) "
        "VALUES (1, 1, 1, 5, 20, 1)"
    )

    # Profiling table profiles
    profiles = [
        # (fqn, table_name, fk_count, referenced_by, is_root, is_leaf, table_class, cls_conf, pii_cols, confirmed_pii)
        ("dbo.customers", "customers", 0, 1, 1, 0, "Master",        0.90, 0, 0),
        ("dbo.orders",    "orders",    1, 1, 0, 0, "Transactional", 0.85, 0, 0),
        ("dbo.invoices",  "invoices",  1, 1, 0, 0, "Transactional", 0.80, 1, 0),
        ("dbo.payments",  "payments",  1, 0, 0, 1, "Transactional", 0.75, 0, 0),
        ("dbo.products",  "products",  0, 0, 1, 1, "Reference",     0.70, 0, 0),
    ]
    for fqn, tbl, fkc, rbc, root, leaf, cls, cc, pii, cpii in profiles:
        conn.execute(
            """INSERT INTO profiling_table_profiles
               (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name,
                fk_count, referenced_by_count, is_root_table, is_leaf_table,
                table_class, classification_confidence,
                pii_column_count, confirmed_pii_count, profiling_status)
               VALUES (1, 1, ?, ?, 'dbo', ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETE')""",
            (fqn, tbl, fkc, rbc, root, leaf, cls, cc, pii, cpii),
        )

    # PII column for invoices
    conn.execute(
        """INSERT INTO profiling_column_profiles
           (profiling_snapshot_id, source_id, table_fqn, column_name,
            data_type, pii_name_heuristic, pii_confirmed)
           VALUES (1, 1, 'dbo.invoices', 'billing_address', 'TEXT', 1, 0)"""
    )

    # FK relationships: orders→customers, invoices→orders, payments→invoices
    fks = [
        ("dbo.orders",   "orders",   "customer_id", "dbo.customers", "dbo", "customers", "id",         "FK_orders_customers"),
        ("dbo.invoices", "invoices", "order_id",    "dbo.orders",    "dbo", "orders",    "id",         "FK_invoices_orders"),
        ("dbo.payments", "payments", "invoice_id",  "dbo.invoices",  "dbo", "invoices",  "id",         "FK_payments_invoices"),
    ]
    for from_fqn, from_tbl, from_col, to_fqn, to_schema, to_tbl, to_col, fk_name in fks:
        conn.execute(
            """INSERT INTO table_relationships
               (source_id, snapshot_id, from_schema, from_table, from_table_fqn,
                from_column, to_schema, to_table, to_table_fqn, to_column,
                relationship_name, relationship_type, confidence)
               VALUES (1, 1, 'dbo', ?, ?, ?, ?, ?, ?, ?, ?, 'FOREIGN_KEY', 1.0)""",
            (from_tbl, from_fqn, from_col, to_schema, to_tbl, to_fqn, to_col, fk_name),
        )

    # Domain assignments
    domain_data = [
        ("dbo.customers", "Sales",    0.90),
        ("dbo.orders",    "Sales",    0.85),
        ("dbo.invoices",  "Finance",  0.80),
        ("dbo.payments",  "Finance",  0.75),
        ("dbo.products",  "Sales",    0.70),
    ]
    for fqn, dom, conf in domain_data:
        conn.execute(
            "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, confidence) "
            "VALUES (1, 1, ?, ?, ?)",
            (fqn, dom, conf),
        )

    # Entity assignments
    entity_data = [
        ("dbo.customers", "Customer", 0.92),
        ("dbo.orders",    "Order",    0.88),
        ("dbo.invoices",  "Invoice",  0.82),
        ("dbo.payments",  "Payment",  0.78),
        ("dbo.products",  "Product",  0.65),
    ]
    for fqn, ent, conf in entity_data:
        conn.execute(
            "INSERT INTO entity_assignments (source_id, profiling_snapshot_id, table_fqn, entity, confidence) "
            "VALUES (1, 1, ?, ?, ?)",
            (fqn, ent, conf),
        )

    # Dictionary entries
    dict_tables = [
        ("dbo.customers", "customers", "Customer",     "Stores customer master data",         "One row per customer", 1),
        ("dbo.orders",    "orders",    "Sales Order",  "Tracks all customer orders",           "One row per order",   0),
        ("dbo.invoices",  "invoices",  "Invoice",      "Financial invoices for orders",        "One row per invoice", 0),
        ("dbo.payments",  "payments",  "Payment",      "Payment records for invoices",         "One row per payment", 0),
        ("dbo.products",  "products",  "Product",      "Product catalog and pricing",          "One row per SKU",     0),
    ]
    for fqn, tbl, bname, desc, grain, approved in dict_tables:
        conn.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, business_name, description, grain, is_approved) "
            "VALUES (1, 1, ?, ?, ?, ?, ?, ?)",
            (fqn, tbl, bname, desc, grain, approved),
        )

    # Column dictionary entries
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, meaning, is_id) "
        "VALUES (1, 1, 'dbo.orders', 'customer_id', 'Customer ID', 'FK to customers', 1)"
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, meaning, is_id) "
        "VALUES (1, 1, 'dbo.orders', 'order_date', 'Order Date', 'Date the order was placed', 0)"
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# T1 — get_related_tables: FK-direct relationships rank first
def test_related_tables_fk_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "kg.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="user-kg", table_fqn="dbo.orders")

    assert result is not None
    assert result["table_fqn"] == "dbo.orders"
    fqns = [r["table_fqn"] for r in result["related_tables"]]

    # FK-direct must be present
    assert "dbo.customers" in fqns   # FK_INBOUND (orders.customer_id → customers.id)
    assert "dbo.invoices"  in fqns   # FK_OUTBOUND (invoices.order_id → orders.id) — wait, this is inbound from invoices perspective

    # FK-direct tables must rank before domain-only tables
    fk_indices   = [i for i, r in enumerate(result["related_tables"])
                    if any(t in r["relationship_types"] for t in ("FK_OUTBOUND", "FK_INBOUND"))]
    dom_only_idx = [i for i, r in enumerate(result["related_tables"])
                    if r["relationship_types"] == ["SAME_DOMAIN"]]
    if fk_indices and dom_only_idx:
        assert max(fk_indices) < min(dom_only_idx)

    assert result["fk_direct"] >= 1


# T2 — get_related_tables: same-domain peers included with lower confidence
def test_related_tables_domain_peers(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_dom.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["domain"] == "Sales"

    # customers is in Sales; orders and products are also in Sales
    related_fqns = {r["table_fqn"] for r in result["related_tables"]}
    assert "dbo.orders"   in related_fqns
    assert "dbo.products" in related_fqns  # same domain, no FK

    # Domain-only peer must have lower confidence than FK direct
    by_fqn = {r["table_fqn"]: r for r in result["related_tables"]}
    assert by_fqn["dbo.orders"]["confidence"] == 1.0          # FK_INBOUND confidence
    assert by_fqn["dbo.products"]["confidence"] < 1.0         # domain only → 0.7 * 0.5 = 0.35


# T3 — get_related_tables: unknown source returns None
def test_related_tables_unknown_source(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_none.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=99, user_id="user-kg", table_fqn="dbo.orders")
    assert result is None


# T4 — find_business_assets: domain filter
def test_find_business_assets_by_domain(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_assets_dom.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # Phase 2E.2: find_business_assets now calls
    # data.profiling_snapshot_resolver.get_latest_profiling_snapshot, which
    # holds its own independently imported get_connection — must be patched
    # too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    assert result is not None
    assert result["filters"]["domain"] == "Finance"
    fqns = {t["table_fqn"] for t in result["tables"]}
    assert "dbo.invoices" in fqns
    assert "dbo.payments" in fqns
    assert "dbo.orders"   not in fqns
    assert result["total_tables"] == 2


# T5 — find_business_assets: entity filter
def test_find_business_assets_by_entity(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_assets_ent.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # Phase 2E.2: find_business_assets now calls
    # data.profiling_snapshot_resolver.get_latest_profiling_snapshot, which
    # holds its own independently imported get_connection — must be patched
    # too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", entity="Customer")

    assert result is not None
    fqns = {t["table_fqn"] for t in result["tables"]}
    assert "dbo.customers" in fqns
    assert result["total_tables"] == 1


# T6 — find_business_assets: term text search
def test_find_business_assets_by_term(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_assets_term.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # Phase 2E.2: find_business_assets now calls
    # data.profiling_snapshot_resolver.get_latest_profiling_snapshot, which
    # holds its own independently imported get_connection — must be patched
    # too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    # "invoice" appears in dbo.invoices description
    result = find_business_assets(source_id=1, user_id="user-kg", term="invoice")

    assert result is not None
    fqns = {t["table_fqn"] for t in result["tables"]}
    assert "dbo.invoices" in fqns

    # "Customer ID" appears in data_dictionary_columns for dbo.orders
    result2 = find_business_assets(source_id=1, user_id="user-kg", term="Customer ID")
    fqns2 = {t["table_fqn"] for t in result2["tables"]}
    assert "dbo.orders" in fqns2


# T7 — explain_table: full explanation with all metadata layers
def test_explain_table_full(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_explain.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # Phase 2E.3: explain_table now calls
    # data.profiling_snapshot_resolver.get_latest_profiling_snapshot, which
    # holds its own independently imported get_connection — must be patched
    # too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["table_fqn"]      == "dbo.customers"
    assert result["business_domain"] == "Sales"
    assert result["business_entity"] == "Customer"
    assert result["business_name"]   == "Customer"
    assert result["classification"]  == "Master"
    # Root table = is_root_table → high importance
    assert result["business_importance"]["score"] > 0.3
    assert result["business_importance"]["label"] in ("CRITICAL", "HIGH", "MEDIUM")
    # Approved dictionary entry produces evidence
    assert any("approved" in e.lower() for e in result["evidence"])
    # governance_score: customers has approved dict, domain, entity, profiling
    assert result["governance_score"] >= 0.75
    # Gaps: no FK relationships extracted yet (relationships are populated, so this gap should not appear)
    assert "business_purpose" in result
    assert result["business_purpose"] != ""


# T8 — explain_table: graceful when most metadata is missing
def test_explain_table_missing_metadata(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_explain_miss.db")
    _create_db(db)

    # Seed only source + schema snapshot, no profiling/dict/domain/entity/relationships
    c = _conn(db)
    c.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (2, 'user-sparse', 'mssql', 'RELATIONAL')"
    )
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (2, 2, 1)"
    )
    c.commit()
    c.close()

    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # Phase 2E.3: explain_table now calls
    # data.profiling_snapshot_resolver.get_latest_profiling_snapshot, which
    # holds its own independently imported get_connection — must be patched
    # too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=2, user_id="user-sparse", table_fqn="dbo.orders")

    assert result is not None
    assert result["business_domain"] is None
    assert result["business_entity"] is None
    assert result["classification"]  is None
    # business_purpose falls back to table name
    assert "orders" in result["business_purpose"].lower()
    # Multiple gaps expected
    assert len(result["gaps"]) >= 3
    # governance_score = 0 (nothing is populated)
    assert result["governance_score"] == 0.0
    # importance score is low (no structural signals)
    assert result["business_importance"]["score"] <= 0.2


# T9 — trace_business_path: connected graph finds path in 3 hops
def test_trace_path_connected(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_path.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    # customers ← orders ← invoices ← payments → path length = 3
    result = trace_business_path(
        source_id=1, user_id="user-kg",
        from_fqn="dbo.customers", to_fqn="dbo.payments",
    )

    assert result is not None
    assert result["found"] is True
    assert result["hops"]  == 3
    assert result["path"][0]["table_fqn"] == "dbo.customers"
    assert result["path"][-1]["table_fqn"] == "dbo.payments"
    # Each intermediate hop has a 'via' describing the FK
    for hop in result["path"][1:]:
        assert hop["via"] is not None
        assert "from_column" in hop["via"]
        assert "direction"   in hop["via"]


# T10 — trace_business_path: disconnected graph returns not found
def test_trace_path_disconnected(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_path_dc.db")
    _create_db(db)
    _seed_full_scenario(db)

    # Add an isolated table with no FK connections
    c = _conn(db)
    # dbo.products already has no FK relationships in the seeded data
    c.close()

    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = trace_business_path(
        source_id=1, user_id="user-kg",
        from_fqn="dbo.products", to_fqn="dbo.payments",
    )

    assert result is not None
    assert result["found"] is False
    assert result["path"]  == []
    assert result["hops"]  == -1


# T11 — trace_business_path: same table returns trivial path
def test_trace_path_same_table(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_path_same.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = trace_business_path(
        source_id=1, user_id="user-kg",
        from_fqn="dbo.orders", to_fqn="dbo.orders",
    )

    assert result is not None
    assert result["found"] is True
    assert result["hops"]  == 0
    assert len(result["path"]) == 1
    assert result["path"][0]["table_fqn"] == "dbo.orders"


# T12 — knowledge_graph_summary: aggregate counts are accurate
def test_knowledge_graph_summary(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_summary.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # Phase 2E.1: knowledge_graph_summary now calls
    # data.profiling_snapshot_resolver.get_latest_profiling_snapshot, which
    # holds its own independently imported get_connection — must be patched
    # too or it hits the real DB.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    assert result["source"]["source_type"] == "mssql"

    # 5 tables, 0 views, 20 columns from schema snapshot
    assert result["nodes"]["total_tables"]  == 5
    assert result["nodes"]["total_views"]   == 0
    assert result["nodes"]["total_columns"] == 20

    # 3 FK edges
    assert result["edges"]["total_relationships"] == 3
    assert result["edges"]["tables_with_fks"]     == 3  # orders, invoices, payments

    # Domain: 5 tables assigned (all have domain), 2 unique domains
    assert result["domain_coverage"]["tables_assigned"] == 5
    assert result["domain_coverage"]["unique_domains"]  == 2
    assert result["domain_coverage"]["coverage_rate"]   == 1.0

    # Entity: 5 tables assigned, 5 unique entities
    assert result["entity_coverage"]["tables_assigned"] == 5
    assert result["entity_coverage"]["unique_entities"] == 5

    # Dictionary: 5 tables with definitions, 1 approved (customers)
    assert result["dictionary_coverage"]["tables_with_definitions"] == 5
    assert result["dictionary_coverage"]["tables_approved"]         == 1

    # Profiling: 5 tables profiled
    assert result["profiling_coverage"]["tables_profiled"] == 5
    assert result["profiling_coverage"]["pii_columns_found"] == 1

    # Confidence: all domain confidences are ≥ 0.5
    assert result["confidence_distribution"]["high"] + result["confidence_distribution"]["medium"] == 5
    assert result["confidence_distribution"]["low"] == 0

    # Unknown source returns None
    assert knowledge_graph_summary(source_id=99, user_id="user-kg") is None


# ---------------------------------------------------------------------------
# Phase 2E.1 — knowledge_graph_summary() call-site migration
#
# Of the three production callers of the module-local _latest_profiling_snap_id()
# wrapper (find_business_assets, explain_table, knowledge_graph_summary),
# knowledge_graph_summary was selected as the lowest-risk first migration:
# prof_snap_id feeds exactly one single-row, all-scalar profiling_snapshots
# fetch (tables_profiled/columns_profiled/pii_columns_found) with no
# graph-traversal, scoring, or evidence fan-out. The wrapper itself is left
# untouched — find_business_assets and explain_table still call it — and
# still runs its own inline SQL rather than delegating to the resolver.
# ---------------------------------------------------------------------------

def _seed_minimal_source(db: str, source_id: int = 1, user_id: str = "user-kg") -> None:
    conn = _conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (?, ?, 'mssql', 'RELATIONAL')",
        (source_id, user_id),
    )
    conn.commit()
    conn.close()


def _insert_profiling_snapshot(
    db: str,
    source_id: int,
    snapshot_version: int,
    status: str = "COMPLETE",
    tables_profiled: int = 0,
    columns_profiled: int = 0,
    pii_columns_found: int = 0,
) -> int:
    conn = _conn(db)
    cur = conn.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, status, "
        "tables_profiled, columns_profiled, pii_columns_found) "
        "VALUES (?, 1, ?, ?, ?, ?, ?)",
        (source_id, snapshot_version, status, tables_profiled, columns_profiled, pii_columns_found),
    )
    conn.commit()
    snap_id = cur.lastrowid
    conn.close()
    return snap_id


# 1 — get_latest_profiling_snapshot() called exactly once
def test_knowledge_graph_summary_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_a.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    with patch(
        "data.knowledge_graph_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_knowledge_graph_summary_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_b.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    with patch(
        "data.knowledge_graph_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        knowledge_graph_summary(source_id=1, user_id="user-kg")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by knowledge_graph_summary()
#
# Phase 2E.4 deleted _latest_profiling_snap_id()'s definition outright — it
# had zero remaining production call sites after Phases 2E.1-2E.3 moved all
# three knowledge-graph functions onto get_latest_profiling_snapshot()
# directly. The prior version of this test proved the wrapper wasn't called
# by monkeypatching it to raise; that mechanism no longer works once the
# attribute doesn't exist (monkeypatch on a missing attribute raises
# AttributeError by default). See
# test_knowledge_graph_service_has_no_legacy_snapshot_helper and
# test_all_knowledge_graph_consumers_still_use_shared_resolver near the end
# of this file for the structural replacement coverage.


# 4 — graph response shape unchanged
def test_knowledge_graph_summary_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_d.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "source", "nodes", "edges", "domain_coverage", "entity_coverage",
        "dictionary_coverage", "profiling_coverage", "relationship_coverage",
        "confidence_distribution", "governance_coverage",
    }
    assert result["source_id"] == 1


# 5 — enrichment (profiling_coverage) unchanged
def test_knowledge_graph_summary_enrichment_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_e.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    assert result["profiling_coverage"] == {
        "tables_profiled": 5,
        "columns_profiled": 20,
        "pii_columns_found": 1,
        "coverage_rate": 1.0,
    }


# 6 — graph metrics (nodes/edges/coverage sections independent of prof_snap_id) unchanged
def test_knowledge_graph_summary_graph_metrics_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_f.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    assert result["nodes"] == {"total_tables": 5, "total_views": 0, "total_columns": 20}
    assert result["edges"]["total_relationships"] == 3
    assert result["edges"]["tables_with_fks"] == 3
    assert result["domain_coverage"]["tables_assigned"] == 5
    assert result["domain_coverage"]["unique_domains"] == 2
    assert result["entity_coverage"]["tables_assigned"] == 5
    assert result["dictionary_coverage"]["tables_with_definitions"] == 5
    assert result["dictionary_coverage"]["tables_approved"] == 1
    assert result["relationship_coverage"]["tables_with_fks"] == 3
    assert result["governance_coverage"]["approved_definitions"] == 1
    assert result["governance_coverage"]["total_definitions"] == 5


# 7 — no-snapshot behavior unchanged: schema/domain/entity/dict sections present, profiling zeroed
def test_knowledge_graph_summary_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_g.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 3)")
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    assert result["nodes"]["total_tables"] == 3
    assert result["profiling_coverage"] == {
        "tables_profiled": 0,
        "columns_profiled": 0,
        "pii_columns_found": 0,
        "coverage_rate": 0.0,
    }


# 8 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_knowledge_graph_summary_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_h.db")
    _create_db(db)
    _seed_full_scenario(db)
    _insert_profiling_snapshot(
        db, source_id=1, snapshot_version=2, status="CANCELLED",
        tables_profiled=99, columns_profiled=999, pii_columns_found=42,
    )
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1,
    # tables_profiled=5/columns_profiled=20/pii_columns_found=1 from
    # _seed_full_scenario) wins over the newer CANCELLED snapshot (version
    # 2) — a newer non-COMPLETE snapshot must never shadow older, usable
    # COMPLETE evidence.
    assert result["profiling_coverage"]["tables_profiled"] == 5
    assert result["profiling_coverage"]["columns_profiled"] == 20
    assert result["profiling_coverage"]["pii_columns_found"] == 1


# 9 — snapshot policy: older COMPLETE beats newer RUNNING
def test_knowledge_graph_summary_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_i.db")
    _create_db(db)
    _seed_full_scenario(db)
    _insert_profiling_snapshot(
        db, source_id=1, snapshot_version=2, status="RUNNING",
        tables_profiled=77, columns_profiled=777, pii_columns_found=7,
    )
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    assert result["profiling_coverage"]["tables_profiled"] == 5
    assert result["profiling_coverage"]["columns_profiled"] == 20
    assert result["profiling_coverage"]["pii_columns_found"] == 1


# 10 — read-only behavior preserved
def test_knowledge_graph_summary_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_j.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
        "data_dictionary_tables",
    )
    before = {t: _dump(t) for t in tables}

    knowledge_graph_summary(source_id=1, user_id="user-kg")
    knowledge_graph_summary(source_id=999, user_id="user-kg")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 11 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_knowledge_graph_summary_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e1_k.db")
    _create_db(db)
    _seed_full_scenario(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _conn(db)

    monkeypatch.setattr("data.knowledge_graph_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = knowledge_graph_summary(source_id=1, user_id="user-kg")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2E.2 — find_business_assets() call-site migration
#
# The second of the two remaining production callers of the module-local
# _latest_profiling_snap_id() wrapper. prof_snap_id here gates a single
# query against profiling_column_profiles (PII columns matching the
# search-qualified table set) — it does not affect domain/entity/term
# matching, table/column/relationship inclusion, or business_definitions,
# all of which are dictionary/domain/entity-derived and profiling-independent.
#
# Deduplication note: find_business_assets has no row-level dedup step: its
# only "merge" behavior is the `qualified: set[str]` union across the
# domain/entity/term filters (test 8 below exercises this — a table matching
# two filters must still appear exactly once). The wrapper is left
# untouched — explain_table still calls it.
# ---------------------------------------------------------------------------

def _insert_pii_column(
    db: str, snapshot_id: int, source_id: int, table_fqn: str, column_name: str,
) -> None:
    conn = _conn(db)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        "pii_name_heuristic, pii_confirmed) "
        "VALUES (?, ?, ?, ?, 'TEXT', 1, 0)",
        (snapshot_id, source_id, table_fqn, column_name),
    )
    conn.commit()
    conn.close()


# 1 — get_latest_profiling_snapshot() called exactly once
def test_find_business_assets_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_a.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    with patch(
        "data.knowledge_graph_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_find_business_assets_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_b.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    with patch(
        "data.knowledge_graph_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by find_business_assets()
#
# See the equivalent note above test 3 in the Phase 2E.1
# knowledge_graph_summary section: this was one of the last production
# callers of the now-deleted wrapper. Structural replacement coverage lives
# in test_knowledge_graph_service_has_no_legacy_snapshot_helper and
# test_all_knowledge_graph_consumers_still_use_shared_resolver near the end
# of this file.


# 4 — response shape unchanged
def test_find_business_assets_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_d.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "filters", "tables", "columns", "relationships",
        "pii_assets", "business_definitions", "total_assets", "total_tables",
        "total_columns", "total_relationships", "total_pii_columns",
    }
    assert result["source_id"] == 1
    assert result["filters"] == {"domain": "Finance", "entity": None, "term": None}


# 5 — result membership unchanged
def test_find_business_assets_result_membership_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_e.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    assert result is not None
    table_fqns = {t["table_fqn"] for t in result["tables"]}
    assert table_fqns == {"dbo.invoices", "dbo.payments"}
    assert result["total_tables"] == 2
    rel_fqns = {r["from_table_fqn"] for r in result["relationships"]} | {
        r["to_table_fqn"] for r in result["relationships"]
    }
    assert "dbo.invoices" in rel_fqns
    def_fqns = {d["table_fqn"] for d in result["business_definitions"]}
    assert def_fqns == {"dbo.invoices", "dbo.payments"}


# 6 — filtering unchanged: domain/entity/term matching independent of profiling
def test_find_business_assets_filtering_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_f.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")
    fqns = {t["table_fqn"] for t in result["tables"]}
    assert "dbo.orders" not in fqns
    assert "dbo.customers" not in fqns

    result_term = find_business_assets(source_id=1, user_id="user-kg", term="Customer ID")
    assert "dbo.orders" in {t["table_fqn"] for t in result_term["tables"]}


# 7 — ordering unchanged: tables by (schema_name, table_name); pii_assets by (table_fqn, column_name)
def test_find_business_assets_ordering_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_g.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Sales")

    assert result is not None
    assert [t["table_fqn"] for t in result["tables"]] == [
        "dbo.customers", "dbo.orders", "dbo.products",
    ]


# 8 — deduplication (set-union merge across filters) unchanged
def test_find_business_assets_deduplication_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_h.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    # domain="Finance" matches {invoices, payments}; entity="Invoice" matches
    # {invoices} again — invoices must appear exactly once, not duplicated.
    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance", entity="Invoice")

    assert result is not None
    fqns = [t["table_fqn"] for t in result["tables"]]
    assert fqns.count("dbo.invoices") == 1
    assert set(fqns) == {"dbo.invoices", "dbo.payments"}
    assert result["total_tables"] == 2


# 9 — PII-derived results unchanged
def test_find_business_assets_pii_results_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_i.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    assert result is not None
    assert result["total_pii_columns"] == 1
    assert len(result["pii_assets"]) == 1
    pii = result["pii_assets"][0]
    assert pii["table_fqn"] == "dbo.invoices"
    assert pii["column_name"] == "billing_address"
    assert pii["pii_name_heuristic"] == 1
    assert pii["pii_confirmed"] == 0


# 10 — no-snapshot behavior unchanged: search results present, PII section empty
def test_find_business_assets_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_j.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _conn(db)
    conn.execute(
        "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, confidence) "
        "VALUES (1, 1, 'dbo.foo', 'Ops', 0.5)"
    )
    conn.commit()
    conn.close()
    # Deliberately no schema_snapshots or profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Ops")

    assert result is not None
    assert {t["table_fqn"] for t in result["tables"]} == {"dbo.foo"}
    assert result["relationships"] == []
    assert result["pii_assets"] == []
    assert result["total_pii_columns"] == 0


# 11 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_find_business_assets_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_k.db")
    _create_db(db)
    _seed_full_scenario(db)
    cancelled_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")
    _insert_pii_column(db, cancelled_id, source_id=1, table_fqn="dbo.payments", column_name="card_number")
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer CANCELLED snapshot (version 2) — a newer
    # non-COMPLETE snapshot must never shadow older, usable COMPLETE
    # evidence, so only the PII column tied to snapshot 1
    # (invoices.billing_address) is visible; the new snapshot's
    # payments.card_number (tied to the CANCELLED snapshot) is not.
    assert len(result["pii_assets"]) == 1
    assert result["pii_assets"][0]["table_fqn"] == "dbo.invoices"
    assert result["pii_assets"][0]["column_name"] == "billing_address"


# 12 — snapshot policy: older COMPLETE beats newer RUNNING
def test_find_business_assets_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_l.db")
    _create_db(db)
    _seed_full_scenario(db)
    running_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="RUNNING")
    _insert_pii_column(db, running_id, source_id=1, table_fqn="dbo.payments", column_name="cvv")
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    assert len(result["pii_assets"]) == 1
    assert result["pii_assets"][0]["table_fqn"] == "dbo.invoices"
    assert result["pii_assets"][0]["column_name"] == "billing_address"


# 13 — read-only behavior preserved
def test_find_business_assets_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_m.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
        "data_dictionary_tables", "data_dictionary_columns",
    )
    before = {t: _dump(t) for t in tables}

    find_business_assets(source_id=1, user_id="user-kg", domain="Finance")
    find_business_assets(source_id=999, user_id="user-kg", domain="Finance")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 14 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_find_business_assets_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e2_n.db")
    _create_db(db)
    _seed_full_scenario(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _conn(db)

    monkeypatch.setattr("data.knowledge_graph_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = find_business_assets(source_id=1, user_id="user-kg", domain="Finance")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2E.3 — explain_table() call-site migration (final Knowledge Graph caller)
#
# The last production caller of the module-local _latest_profiling_snap_id()
# wrapper. prof_snap_id here gates a single-row profiling_table_profiles
# fetch (table_profile) that feeds nearly every profiling-derived output:
# classification, the profiling/pii subdicts, the importance score/label
# (4 of 5 score components derive from table_profile), evidence lines, one
# gap line, and two governance flags. Expected values below were computed
# by hand from the _seed_full_scenario fixture:
#   dbo.customers: score = 0.06(ref=1) + 0.20(root) + 0.20(Master) + 0.10(dict
#     approved) = 0.56 -> label "HIGH"; governance_score 4/4 = 1.0
#   dbo.products:  score = 0.20(root) + 0.12(Reference) = 0.32 (no ref-count,
#     no PII, no dict approval) -> label "MEDIUM" — a distinct component mix
#     used to verify the score formula isn't collapsed to a single case.
#   dbo.invoices:  pii_column_count=1, confirmed_pii_count=0 ->
#     pii.review_needed True, governance.dictionary_approved False (not
#     approved in the fixture) -> governance_score 3/4 = 0.75.
#   dbo.orders:    dict entry present but not approved -> gaps ==
#     ["Dictionary entry not yet human-approved"] (domain/entity/profiling/
#     relationships are all otherwise present for orders).
# ---------------------------------------------------------------------------

def _insert_table_profile_row(
    db: str,
    snapshot_id: int,
    source_id: int,
    table_fqn: str,
    table_name: str,
    table_class: str,
    classification_confidence: float,
    referenced_by_count: int = 0,
    is_root_table: int = 0,
    pii_column_count: int = 0,
    confirmed_pii_count: int = 0,
) -> None:
    conn = _conn(db)
    conn.execute(
        """INSERT INTO profiling_table_profiles
           (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name,
            table_class, classification_confidence, referenced_by_count,
            is_root_table, pii_column_count, confirmed_pii_count, profiling_status)
           VALUES (?, ?, ?, ?, 'dbo', ?, ?, ?, ?, ?, ?, 'COMPLETE')""",
        (snapshot_id, source_id, table_fqn, table_name, table_class,
         classification_confidence, referenced_by_count, is_root_table,
         pii_column_count, confirmed_pii_count),
    )
    conn.commit()
    conn.close()


# 1 — get_latest_profiling_snapshot() called exactly once
def test_explain_table_calls_resolver_exactly_once(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_a.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    with patch(
        "data.knowledge_graph_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    spy.assert_called_once()


# 2 — correct source_id passed
def test_explain_table_passes_correct_source_id(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_b.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    with patch(
        "data.knowledge_graph_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    spy.assert_called_once_with(1)


# 3 — wrapper helper no longer exists to be called by explain_table()
#
# See the equivalent note above test 3 in the Phase 2E.1
# knowledge_graph_summary section: this was the last production caller of
# the now-deleted wrapper. Structural replacement coverage lives in
# test_knowledge_graph_service_has_no_legacy_snapshot_helper and
# test_all_knowledge_graph_consumers_still_use_shared_resolver near the end
# of this file.


# 4 — response shape unchanged
def test_explain_table_response_shape_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_d.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "table_fqn", "table_name", "schema_name", "business_purpose",
        "business_domain", "business_entity", "business_name", "grain",
        "classification", "classification_confidence", "profiling", "pii",
        "relationships", "business_importance", "governance", "governance_score",
        "evidence", "gaps", "domain_confidence", "entity_confidence",
    }
    assert set(result["profiling"].keys()) == {
        "row_count", "row_count_tier", "fk_count", "referenced_by",
        "is_root_table", "is_leaf_table", "is_junction_table",
        "completeness_score", "profiling_depth",
    }
    assert set(result["pii"].keys()) == {"pii_column_count", "confirmed_pii_count", "review_needed"}
    assert set(result["relationships"].keys()) == {"outbound", "inbound"}
    assert set(result["business_importance"].keys()) == {"score", "label"}
    assert set(result["governance"].keys()) == {
        "dictionary_approved", "domain_assigned", "entity_assigned",
        "profiling_complete", "pii_review_needed",
    }


# 5 — profiling fields unchanged
def test_explain_table_profiling_fields_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_e.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["profiling"] == {
        "row_count": None,
        "row_count_tier": None,
        "fk_count": 0,
        "referenced_by": 1,
        "is_root_table": True,
        "is_leaf_table": False,
        "is_junction_table": False,
        "completeness_score": None,
        "profiling_depth": "STRUCTURAL_ONLY",
    }


# 6 — PII fields unchanged (dbo.invoices: 1 unconfirmed PII column)
def test_explain_table_pii_fields_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_f.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.invoices")

    assert result is not None
    assert result["pii"] == {
        "pii_column_count": 1,
        "confirmed_pii_count": 0,
        "review_needed": True,
    }


# 7 — classification fields unchanged
def test_explain_table_classification_fields_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_g.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["classification"] == "Master"
    assert result["classification_confidence"] == 0.90


# 8 — importance score unchanged (exact formula output)
def test_explain_table_importance_score_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_h.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    # 0.06 (ref_count=1) + 0.20 (root) + 0.20 (Master) + 0.10 (dict approved) = 0.56
    assert result["business_importance"]["score"] == 0.56


# 9 — importance label unchanged
def test_explain_table_importance_label_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_i.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["business_importance"]["label"] == "HIGH"


# 10 — score components unchanged: a distinct component mix (root + classification
# only, no ref-count/PII/dict-approval terms) for dbo.products
def test_explain_table_score_components_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_j.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.products")

    assert result is not None
    # 0.20 (root) + 0.12 (Reference) = 0.32; no referenced_by, no PII, dict not approved
    assert result["business_importance"]["score"] == 0.32
    assert result["business_importance"]["label"] == "MEDIUM"


# 11 — evidence list unchanged (exact order and content)
def test_explain_table_evidence_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_k.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["evidence"] == [
        "Dictionary business name: Customer",
        "Dictionary entry approved by human reviewer",
        "Domain: Sales (90% confidence)",
        "Entity: Customer (92% confidence)",
        "Classification: Master (90%)",
        "Root table: no incoming FKs — primary business entity",
        "Referenced by 1 other table(s) via FK",
        "Referenced by FK from: dbo.orders",
    ]


# 12 — gaps list unchanged (dbo.orders: dictionary entry exists but unapproved;
# domain/entity/profiling/relationships all otherwise present)
def test_explain_table_gaps_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_l.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.orders")

    assert result is not None
    assert result["gaps"] == ["Dictionary entry not yet human-approved"]


# 13 — governance flags unchanged (dbo.invoices: unapproved dict + unconfirmed PII)
def test_explain_table_governance_flags_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_m.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.invoices")

    assert result is not None
    assert result["governance"] == {
        "dictionary_approved": False,
        "domain_assigned": True,
        "entity_assigned": True,
        "profiling_complete": True,
        "pii_review_needed": True,
    }
    assert result["governance_score"] == 0.75


# 14 — relationship context unchanged
def test_explain_table_relationship_context_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_n.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert result["relationships"]["outbound"] == []
    assert len(result["relationships"]["inbound"]) == 1
    assert result["relationships"]["inbound"][0]["from_table_fqn"] == "dbo.orders"
    assert result["relationships"]["inbound"][0]["from_column"] == "customer_id"


# 15 — no-snapshot behavior unchanged: dictionary/domain/entity/relationships
# present, profiling-derived fields absent/None
def test_explain_table_no_snapshot_behavior_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_o.db")
    _create_db(db)
    _seed_minimal_source(db)
    conn = _conn(db)
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, confidence) "
        "VALUES (1, 1, 'dbo.orders', 'Sales', 0.8)"
    )
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit()
    conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.orders")

    assert result is not None
    assert result["business_domain"] == "Sales"
    assert result["classification"] is None
    assert result["classification_confidence"] is None
    assert result["profiling"] == {
        "row_count": None, "row_count_tier": None, "fk_count": None,
        "referenced_by": None, "is_root_table": None, "is_leaf_table": None,
        "is_junction_table": None, "completeness_score": None, "profiling_depth": None,
    }
    assert result["pii"] == {"pii_column_count": 0, "confirmed_pii_count": 0, "review_needed": False}
    assert len(result["relationships"]["outbound"]) == 1
    assert "Not profiled — run structural profiling" in result["gaps"]


# 16 — snapshot policy: older COMPLETE beats newer CANCELLED
def test_explain_table_snapshot_policy_complete_beats_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_p.db")
    _create_db(db)
    _seed_full_scenario(db)
    cancelled_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")
    _insert_table_profile_row(
        db, cancelled_id, source_id=1, table_fqn="dbo.customers", table_name="customers",
        table_class="Transactional", classification_confidence=0.55,
        referenced_by_count=9, is_root_table=0, pii_column_count=3, confirmed_pii_count=1,
    )
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1
    # — table_class="Master", classification_confidence=0.90,
    # referenced_by_count=1, pii_column_count=0, from _seed_full_scenario)
    # wins over the newer CANCELLED snapshot (version 2) — a newer
    # non-COMPLETE snapshot must never shadow older, usable COMPLETE
    # evidence.
    assert result["classification"] == "Master"
    assert result["classification_confidence"] == 0.90
    assert result["profiling"]["referenced_by"] == 1
    assert result["pii"]["pii_column_count"] == 0


# 17 — snapshot policy: older COMPLETE beats newer RUNNING
def test_explain_table_snapshot_policy_complete_beats_running(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_q.db")
    _create_db(db)
    _seed_full_scenario(db)
    running_id = _insert_profiling_snapshot(db, source_id=1, snapshot_version=2, status="RUNNING")
    _insert_table_profile_row(
        db, running_id, source_id=1, table_fqn="dbo.customers", table_name="customers",
        table_class="Reference", classification_confidence=0.44,
        referenced_by_count=4, is_root_table=0, pii_column_count=2, confirmed_pii_count=2,
    )
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    # Status-aware selection: the seeded COMPLETE snapshot (version 1, id=1)
    # wins over the newer RUNNING snapshot (version 2).
    assert result["classification"] == "Master"
    assert result["classification_confidence"] == 0.90
    assert result["profiling"]["referenced_by"] == 1
    assert result["pii"]["pii_column_count"] == 0


# 18 — read-only behavior preserved
def test_explain_table_read_only_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_r.db")
    _create_db(db)
    _seed_full_scenario(db)
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    def _dump(table: str) -> list[dict]:
        conn = _conn(db)
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        conn.close()
        return rows

    tables = (
        "data_source_connections", "schema_snapshots", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "table_relationships", "domain_assignments", "entity_assignments",
        "data_dictionary_tables", "data_dictionary_columns",
    )
    before = {t: _dump(t) for t in tables}

    explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")
    explain_table(source_id=999, user_id="user-kg", table_fqn="dbo.customers")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# 19 — resolver connection count preserved: 1 service + 1 resolver = 2 total
def test_explain_table_connection_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "kg_p2e3_s.db")
    _create_db(db)
    _seed_full_scenario(db)

    counters = {"service_conn": 0, "resolver_conn": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _conn(db)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _conn(db)

    monkeypatch.setattr("data.knowledge_graph_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = explain_table(source_id=1, user_id="user-kg", table_fqn="dbo.customers")

    assert result is not None
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2


# ---------------------------------------------------------------------------
# Phase 2E.4 — Knowledge Graph wrapper retirement
#
# _latest_profiling_snap_id() had zero remaining production call sites after
# Phases 2E.1-2E.3 moved all three knowledge-graph functions
# (knowledge_graph_summary, find_business_assets, explain_table) onto
# get_latest_profiling_snapshot() directly. This phase deletes the dead
# wrapper's definition outright. Mirrors the Phase 2B.8 precedent in
# tests/test_phase6_semantic.py, the Phase 2C.3 precedent in
# tests/test_phase3_business_knowledge.py for the identically-named (but
# separately scoped) wrappers in those modules.
# ---------------------------------------------------------------------------

# 1 — knowledge_graph_service_has_no_legacy_snapshot_helper
def test_knowledge_graph_service_has_no_legacy_snapshot_helper():
    import data.knowledge_graph_service as kg_module
    assert not hasattr(kg_module, "_latest_profiling_snap_id")


# 2 — knowledge_graph_service_source_has_no_legacy_helper_reference
def test_knowledge_graph_service_source_has_no_legacy_helper_reference():
    import inspect
    import data.knowledge_graph_service as kg_module

    source = inspect.getsource(kg_module)
    assert "_latest_profiling_snap_id" not in source


# 3 — all_knowledge_graph_consumers_still_use_shared_resolver
#
# Narrow inventory assertion (not a duplicate of every prior delegation
# test): confirms, via source inspection, that each of the three migrated
# functions still references the shared resolver by name. Full behavioral
# delegation coverage (spy call counts, correct source_id, snapshot policy,
# etc.) already exists per-function earlier in this file — this test only
# guards against a future edit silently reverting one of the three call
# sites back to the deleted local helper without any test noticing.
def test_all_knowledge_graph_consumers_still_use_shared_resolver():
    import inspect
    import data.knowledge_graph_service as kg_module

    migrated = [
        kg_module.knowledge_graph_summary,
        kg_module.find_business_assets,
        kg_module.explain_table,
    ]
    for fn in migrated:
        source = inspect.getsource(fn)
        assert "get_latest_profiling_snapshot(" in source, (
            f"{fn.__name__} no longer appears to call the shared resolver"
        )
        assert "_latest_profiling_snap_id(" not in source, (
            f"{fn.__name__} appears to reference the retired local helper"
        )
