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
