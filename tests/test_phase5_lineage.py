"""
Tests for Business Lineage & Impact Analysis — Phase 5.

Scenario — FK chain:
    dbo.customers ← dbo.orders ← dbo.invoices ← dbo.payments
    (customers is the foundation; payments is the terminal)
    dbo.products — isolated, no FK connections

Upstream of dbo.payments  = [invoices(1), orders(2), customers(3)]
Downstream of dbo.customers = [orders(1), invoices(2), payments(3)]

Run from the project root:
    venv/Scripts/pytest tests/test_phase5_lineage.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-lineage-phase5-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase5-long-enough-value-12345")

from data.lineage_service import (
    get_upstream_lineage,
    get_downstream_lineage,
    impact_analysis,
    critical_asset_analysis,
    lineage_summary,
)


# ---------------------------------------------------------------------------
# Minimal DB schema (only tables the lineage service reads from)
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Test',
        source_type TEXT NOT NULL DEFAULT 'mssql',
        source_category TEXT NOT NULL DEFAULT 'RELATIONAL',
        encrypted_config_json TEXT NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        source_status TEXT NOT NULL DEFAULT 'ACTIVE',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE schema_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        source_type TEXT NOT NULL DEFAULT 'mssql',
        table_count INTEGER NOT NULL DEFAULT 0,
        view_count INTEGER NOT NULL DEFAULT 0,
        column_count INTEGER NOT NULL DEFAULT 0,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        discovered_at TEXT NOT NULL DEFAULT '2026-01-01',
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        schema_snapshot_id INTEGER NOT NULL DEFAULT 1,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        mode TEXT NOT NULL DEFAULT 'full',
        sample_rate REAL NOT NULL DEFAULT 1.0,
        profiling_rules_version TEXT NOT NULL DEFAULT '1.0.0',
        status TEXT NOT NULL DEFAULT 'COMPLETE',
        tables_total INTEGER NOT NULL DEFAULT 0,
        tables_profiled INTEGER NOT NULL DEFAULT 0,
        columns_total INTEGER NOT NULL DEFAULT 0,
        columns_profiled INTEGER NOT NULL DEFAULT 0,
        pii_columns_found INTEGER NOT NULL DEFAULT 0,
        tables_skipped INTEGER NOT NULL DEFAULT 0,
        tables_failed INTEGER NOT NULL DEFAULT 0,
        tables_timed_out INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at TEXT, completed_at TEXT, duration_seconds INTEGER,
        resumable_state_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_table_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        exact_row_count INTEGER,
        estimated_row_count INTEGER,
        row_count_tier TEXT,
        has_date_column INTEGER NOT NULL DEFAULT 0,
        date_column_name TEXT, earliest_record TEXT, latest_record TEXT,
        data_span_days INTEGER, data_currency TEXT,
        column_count INTEGER NOT NULL DEFAULT 0,
        pk_column_count INTEGER NOT NULL DEFAULT 0,
        fk_count INTEGER NOT NULL DEFAULT 0,
        referenced_by_count INTEGER NOT NULL DEFAULT 0,
        is_junction_table INTEGER NOT NULL DEFAULT 0,
        is_root_table INTEGER NOT NULL DEFAULT 0,
        is_leaf_table INTEGER NOT NULL DEFAULT 0,
        has_identity_column INTEGER NOT NULL DEFAULT 0,
        avg_null_percentage REAL, completeness_score REAL,
        table_class TEXT, classification_confidence REAL,
        classification_evidence_json TEXT, competing_classes_json TEXT,
        classification_rule_version TEXT,
        pii_column_count INTEGER NOT NULL DEFAULT 0,
        confirmed_pii_count INTEGER NOT NULL DEFAULT 0,
        profiling_depth TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        skip_reason TEXT, profiled_at TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_column_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL,
        column_name TEXT NOT NULL,
        data_type TEXT NOT NULL DEFAULT 'TEXT',
        raw_type TEXT,
        is_nullable INTEGER NOT NULL DEFAULT 1,
        is_primary_key INTEGER NOT NULL DEFAULT 0,
        is_identity INTEGER NOT NULL DEFAULT 0,
        ordinal_position INTEGER NOT NULL DEFAULT 1,
        null_percentage REAL, distinct_count INTEGER, distinct_percentage REAL,
        uniqueness_score REAL, cardinality_tier TEXT,
        min_value TEXT, max_value TEXT, avg_length REAL,
        mean_value REAL, std_deviation REAL, null_count INTEGER,
        populated_count INTEGER, populated_percentage REAL,
        empty_string_count INTEGER, zero_count INTEGER,
        min_length INTEGER, max_length_observed INTEGER,
        p5_value TEXT, p95_value TEXT,
        dominant_pattern TEXT, pattern_coverage REAL,
        email_match_rate REAL, phone_match_rate REAL, guid_match_rate REAL,
        date_string_rate REAL, numeric_string_rate REAL, masked_value_rate REAL,
        semantic_type TEXT, semantic_confidence REAL,
        semantic_evidence_json TEXT, semantic_rule_version TEXT,
        pii_name_heuristic INTEGER NOT NULL DEFAULT 0,
        pii_confirmed INTEGER NOT NULL DEFAULT 0,
        pii_signals_json TEXT, top_values_coverage REAL,
        profiling_depth TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_tables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        business_name TEXT, description TEXT, domain TEXT, grain TEXT,
        is_approved INTEGER NOT NULL DEFAULT 0,
        approved_by TEXT, approved_at TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_columns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, column_name TEXT NOT NULL,
        business_label TEXT, meaning TEXT, semantic_type TEXT,
        is_metric INTEGER NOT NULL DEFAULT 0, is_dimension INTEGER NOT NULL DEFAULT 0,
        is_date INTEGER NOT NULL DEFAULT 0, is_id INTEGER NOT NULL DEFAULT 0,
        pii_risk INTEGER NOT NULL DEFAULT 0,
        is_approved INTEGER NOT NULL DEFAULT 0,
        approved_by TEXT, approved_at TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE domain_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, domain TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_domains_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE entity_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, entity TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_entities_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE table_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL,
        from_schema TEXT NOT NULL, from_table TEXT NOT NULL,
        from_table_fqn TEXT NOT NULL, from_column TEXT NOT NULL,
        to_schema TEXT NOT NULL, to_table TEXT NOT NULL,
        to_table_fqn TEXT NOT NULL, to_column TEXT NOT NULL,
        relationship_name TEXT,
        relationship_type TEXT NOT NULL DEFAULT 'FOREIGN_KEY',
        confidence REAL NOT NULL DEFAULT 1.0,
        evidence_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE UNIQUE INDEX idx_tr_uniq ON table_relationships
        (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
"""


def _create_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _seed(path: str) -> None:
    """
    Seed source=1 with the FK chain:
        customers ← orders ← invoices ← payments   (linear chain)
        products                                     (isolated)
    """
    conn = _db_conn(path)

    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-lin', 'Lineage Test', 'mssql', 'RELATIONAL')"
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 5)"
    )
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, tables_profiled, pii_columns_found) "
        "VALUES (1, 1, 1, 5, 2)"
    )

    # profiling table profiles
    # fk_count = number of outbound FKs; referenced_by_count = number of tables referencing this one
    profiles = [
        # fqn,             name,        fkc, rbc, pii, cls_conf, cls,           approved_dict
        ("dbo.customers", "customers",  0,   1,   0,   0.90, "Master",        True),
        ("dbo.orders",    "orders",     1,   1,   0,   0.85, "Transactional", False),
        ("dbo.invoices",  "invoices",   1,   1,   2,   0.80, "Transactional", False),
        ("dbo.payments",  "payments",   1,   0,   0,   0.75, "Transactional", False),
        ("dbo.products",  "products",   0,   0,   0,   0.70, "Reference",     False),
    ]
    for fqn, tbl, fkc, rbc, pii, cc, cls, _ in profiles:
        conn.execute(
            """INSERT INTO profiling_table_profiles
               (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name,
                fk_count, referenced_by_count, table_class, classification_confidence,
                pii_column_count, confirmed_pii_count, profiling_status)
               VALUES (1, 1, ?, ?, 'dbo', ?, ?, ?, ?, ?, 0, 'COMPLETE')""",
            (fqn, tbl, fkc, rbc, cls, cc, pii),
        )

    # PII columns for invoices
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, pii_name_heuristic) "
        "VALUES (1, 1, 'dbo.invoices', 'billing_address', 'TEXT', 1)"
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, pii_name_heuristic) "
        "VALUES (1, 1, 'dbo.invoices', 'bank_account_no', 'TEXT', 1)"
    )

    # FK chain
    fks = [
        ("orders",   "orders",   "customer_id", "dbo.customers", "dbo", "customers", "id",         "FK_orders_customers"),
        ("invoices", "invoices", "order_id",    "dbo.orders",    "dbo", "orders",    "id",         "FK_invoices_orders"),
        ("payments", "payments", "invoice_id",  "dbo.invoices",  "dbo", "invoices",  "id",         "FK_payments_invoices"),
    ]
    for from_tbl, tbl_name, from_col, to_fqn, to_schema, to_tbl, to_col, fk_name in fks:
        conn.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
            "from_column, to_schema, to_table, to_table_fqn, to_column, "
            "relationship_name, relationship_type, confidence) "
            "VALUES (1, 1, 'dbo', ?, ?, ?, ?, ?, ?, ?, ?, 'FOREIGN_KEY', 1.0)",
            (tbl_name, f"dbo.{from_tbl}", from_col, to_schema, to_tbl, to_fqn, to_col, fk_name),
        )

    # Domain assignments
    for fqn, dom, conf in [
        ("dbo.customers", "Sales",   0.90),
        ("dbo.orders",    "Sales",   0.85),
        ("dbo.invoices",  "Finance", 0.80),
        ("dbo.payments",  "Finance", 0.75),
        ("dbo.products",  "Sales",   0.65),
    ]:
        conn.execute(
            "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, confidence) "
            "VALUES (1, 1, ?, ?, ?)", (fqn, dom, conf)
        )

    # Entity assignments
    for fqn, ent, conf in [
        ("dbo.customers", "Customer", 0.92),
        ("dbo.orders",    "Order",    0.88),
        ("dbo.invoices",  "Invoice",  0.82),
        ("dbo.payments",  "Payment",  0.78),
        ("dbo.products",  "Product",  0.65),
    ]:
        conn.execute(
            "INSERT INTO entity_assignments (source_id, profiling_snapshot_id, table_fqn, entity, confidence) "
            "VALUES (1, 1, ?, ?, ?)", (fqn, ent, conf)
        )

    # Dictionary (customers is approved; others are generated only)
    for fqn, tbl, bname, approved in [
        ("dbo.customers", "customers", "Customer",    1),
        ("dbo.orders",    "orders",    "Sales Order", 0),
        ("dbo.invoices",  "invoices",  "Invoice",     0),
        ("dbo.payments",  "payments",  "Payment",     0),
        ("dbo.products",  "products",  "Product",     0),
    ]:
        conn.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, business_name, is_approved) "
            "VALUES (1, 1, ?, ?, ?, ?)", (fqn, tbl, bname, approved)
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# T1 — upstream: direct parent
def test_upstream_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "lin.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")

    assert result is not None
    assert result["table_fqn"] == "dbo.orders"
    fqns = [n["table_fqn"] for n in result["upstream"]]
    assert "dbo.customers" in fqns
    assert result["total_upstream"] == 1
    assert result["max_depth"] == 1
    assert result["has_cycle"] is False

    # Check first hop is a FK_PARENT
    first = result["upstream"][0]
    assert first["distance"] == 1
    assert first["relationship_type"] == "FK_PARENT"
    assert first["via_column"] == "customer_id"


# T2 — upstream: transitive ancestors
def test_upstream_transitive(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_up.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.payments")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["upstream"]}
    assert fqns == {"dbo.invoices", "dbo.orders", "dbo.customers"}
    assert result["total_upstream"] == 3
    assert result["max_depth"] == 3

    by_fqn = {n["table_fqn"]: n for n in result["upstream"]}
    assert by_fqn["dbo.invoices"]["distance"]  == 1
    assert by_fqn["dbo.orders"]["distance"]    == 2
    assert by_fqn["dbo.customers"]["distance"] == 3

    # Ancestors beyond distance-1 get FK_ANCESTOR relationship type
    assert by_fqn["dbo.customers"]["relationship_type"] == "FK_ANCESTOR"


# T3 — upstream: disconnected table returns empty
def test_upstream_disconnected(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_dc.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.products")

    assert result is not None
    assert result["upstream"] == []
    assert result["total_upstream"] == 0
    assert result["has_cycle"] is False


# T4 — downstream: direct child
def test_downstream_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_ds.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.invoices")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["downstream"]}
    assert "dbo.payments" in fqns
    assert result["total_downstream"] == 1

    first = result["downstream"][0]
    assert first["distance"] == 1
    assert first["relationship_type"] == "FK_CHILD"
    assert first["table_fqn"] == "dbo.payments"


# T5 — downstream: transitive descendants from foundation
def test_downstream_transitive(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_dstr.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    fqns = {n["table_fqn"] for n in result["downstream"]}
    assert fqns == {"dbo.orders", "dbo.invoices", "dbo.payments"}
    assert result["total_downstream"] == 3
    assert result["max_depth"] == 3

    by_fqn = {n["table_fqn"]: n for n in result["downstream"]}
    assert by_fqn["dbo.orders"]["distance"]    == 1
    assert by_fqn["dbo.invoices"]["distance"]  == 2
    assert by_fqn["dbo.payments"]["distance"]  == 3
    assert by_fqn["dbo.payments"]["relationship_type"] == "FK_DESCENDANT"


# T6 — cycle handling: BFS does not infinite-loop
def test_cycle_no_infinite_loop(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_cycle.db")
    _create_db(db)
    _seed(db)

    # Inject a cycle: dbo.customers.fk → dbo.orders  (circular with the existing one)
    c = _db_conn(db)
    try:
        c.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
            "from_column, to_schema, to_table, to_table_fqn, to_column, "
            "relationship_name, relationship_type, confidence) "
            "VALUES (1, 1, 'dbo', 'customers', 'dbo.customers', "
            "'order_ref_id', 'dbo', 'orders', 'dbo.orders', 'id', "
            "'FK_cycle_test', 'FOREIGN_KEY', 0.5)"
        )
        c.commit()
    finally:
        c.close()

    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    # Must terminate (not loop forever)
    result_up = get_upstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.orders")
    assert result_up is not None
    assert result_up["has_cycle"] is True

    result_dn = get_downstream_lineage(source_id=1, user_id="user-lin", table_fqn="dbo.customers")
    assert result_dn is not None


# T7 — unknown source returns None
def test_unknown_source_returns_none(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_none.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    assert get_upstream_lineage(99, "user-lin", "dbo.orders")          is None
    assert get_downstream_lineage(99, "user-lin", "dbo.orders")        is None
    assert impact_analysis(99, "user-lin", "dbo.orders")               is None
    assert critical_asset_analysis(99, "user-lin")                     is None
    assert lineage_summary(99, "user-lin")                             is None


# T8 — impact analysis: changing customers affects all 3 downstream tables
def test_impact_analysis_table(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_impact.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = impact_analysis(source_id=1, user_id="user-lin", table_fqn="dbo.customers")

    assert result is not None
    assert result["table_fqn"] == "dbo.customers"
    assert result["total_affected_tables"] == 3
    affected_fqns = {t["table_fqn"] for t in result["affected_tables"]}
    assert affected_fqns == {"dbo.orders", "dbo.invoices", "dbo.payments"}

    # invoices has 2 PII columns — must appear in affected_pii_assets
    assert result["total_pii_columns"] == 2
    pii_fqns = {p["table_fqn"] for p in result["affected_pii_assets"]}
    assert "dbo.invoices" in pii_fqns

    # Domain spread: Sales (orders) + Finance (invoices, payments)
    assert "Sales"   in result["affected_domains"]
    assert "Finance" in result["affected_domains"]

    # Impact score > 0
    assert result["impact_score"] > 0.0
    assert result["impact_label"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")


# T9 — impact analysis: column-scoped propagation paths
def test_impact_analysis_column(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_imp_col.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = impact_analysis(
        source_id=1, user_id="user-lin",
        table_fqn="dbo.customers", column_name="id",
    )

    assert result is not None
    assert result["column_name"] == "id"
    # FK: orders.customer_id → customers.id  → propagation path present
    paths = result["column_propagation_paths"]
    assert any(p["to_column"] == "id" or p["from_column"] == "id" for p in paths)


# T10 — critical asset analysis: categories are correct
def test_critical_asset_analysis(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_crit.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = critical_asset_analysis(source_id=1, user_id="user-lin")

    assert result is not None

    # Foundation tables: have inbound FKs but no outbound = customers
    # (customers is referenced by orders; customers has no FK to anyone)
    foundation_fqns = {t["table_fqn"] for t in result["foundation_tables"]}
    assert "dbo.customers" in foundation_fqns

    # Terminal tables: have outbound FKs but nothing references them = payments
    terminal_fqns = {t["table_fqn"] for t in result["terminal_tables"]}
    assert "dbo.payments" in terminal_fqns

    # Hub tables: both inbound and outbound = orders and invoices
    hub_fqns = {t["table_fqn"] for t in result["hub_tables"]}
    assert "dbo.orders"   in hub_fqns
    assert "dbo.invoices" in hub_fqns

    # Summary counts
    assert result["summary"]["total_foundation"] >= 1
    assert result["summary"]["total_terminal"]   >= 1
    assert result["summary"]["total_hub"]        >= 2

    # Business critical: customers has approved dict + domain + entity
    biz_fqns = {t["table_fqn"] for t in result["business_critical"]}
    assert "dbo.customers" in biz_fqns


# T11 — lineage_summary: correct structural breakdown
def test_lineage_summary(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_sum.db")
    _create_db(db)
    _seed(db)
    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    result = lineage_summary(source_id=1, user_id="user-lin")

    assert result is not None
    assert result["source"]["source_type"] == "mssql"

    # coverage: 4 tables in graph (customers, orders, invoices, payments)
    #           1 disconnected (products)
    assert result["coverage"]["tables_in_graph"] == 4
    assert result["coverage"]["total_schema_tables"] == 5

    # Root assets: foundation table(s)
    root_fqns = {t["table_fqn"] for t in result["root_assets"]}
    assert "dbo.customers" in root_fqns

    # Leaf assets: terminal table(s)
    leaf_fqns = {t["table_fqn"] for t in result["leaf_assets"]}
    assert "dbo.payments" in leaf_fqns

    # Hub assets: both inbound and outbound
    hub_fqns = {t["table_fqn"] for t in result["hub_assets"]}
    assert "dbo.orders" in hub_fqns

    # Disconnected: products
    disc_fqns = {t["table_fqn"] for t in result["disconnected_assets"]}
    assert "dbo.products" in disc_fqns

    # Longest chain: customers → orders → invoices → payments = 3 hops
    assert result["longest_chain"]["length"] == 3
    chain_path = result["longest_chain"]["path"]
    assert chain_path[0]  == "dbo.customers"
    assert chain_path[-1] == "dbo.payments"

    # Average depth > 0
    assert result["average_depth"] > 0.0


# T12 — empty graph (no table_relationships): all lists empty, graceful
def test_empty_graph_graceful(tmp_path, monkeypatch):
    db = str(tmp_path / "lin_empty.db")
    _create_db(db)

    c = _db_conn(db)
    c.execute(
        "INSERT INTO data_source_connections (id, user_id, source_type, source_category) "
        "VALUES (2, 'user-empty', 'mssql', 'RELATIONAL')"
    )
    c.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (2, 2, 3)"
    )
    c.commit()
    c.close()

    monkeypatch.setattr("data.lineage_service.get_connection", lambda: _db_conn(db))

    up  = get_upstream_lineage(2, "user-empty", "dbo.orders")
    dn  = get_downstream_lineage(2, "user-empty", "dbo.orders")
    imp = impact_analysis(2, "user-empty", "dbo.orders")
    crit = critical_asset_analysis(2, "user-empty")
    summ = lineage_summary(2, "user-empty")

    assert up  is not None and up["upstream"]    == []
    assert dn  is not None and dn["downstream"]  == []
    assert imp is not None and imp["affected_tables"] == []
    assert imp["impact_score"] == 0.0
    assert crit is not None and crit["foundation_tables"] == []
    assert summ is not None
    assert summ["coverage"]["tables_in_graph"]   == 0
    assert summ["coverage"]["tables_disconnected"] == 0
    assert summ["longest_chain"]["length"] == 0
