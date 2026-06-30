"""
Tests for Enterprise Semantic Layer & Join Intelligence — Phase 6.

Scenario — FK chain:
    dbo.customers ← dbo.orders ← dbo.invoices ← dbo.payments
    dbo.products                                      (isolated)

    orders.customer_id → customers.id   (orders also has a second FK
    orders.billing_id  → customers.id   to create MULTIPLE_DIRECT_FKS)

Run from the project root:
    venv/Scripts/pytest tests/test_phase6_semantic.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-semantic-phase6-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase6-long-enough-value-123456")

from data.semantic_layer_service import (
    discover_business_joins,
    discover_join_paths,
    detect_join_ambiguity,
    semantic_table_profile,
    semantic_summary,
)


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT 'Test',
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
        source_id INTEGER NOT NULL, snapshot_version INTEGER NOT NULL DEFAULT 1,
        source_type TEXT NOT NULL DEFAULT 'mssql',
        table_count INTEGER NOT NULL DEFAULT 0, view_count INTEGER NOT NULL DEFAULT 0,
        column_count INTEGER NOT NULL DEFAULT 0,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        discovered_at TEXT NOT NULL DEFAULT '2026-01-01',
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        schema_snapshot_id INTEGER NOT NULL DEFAULT 1,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        mode TEXT NOT NULL DEFAULT 'full', sample_rate REAL NOT NULL DEFAULT 1.0,
        profiling_rules_version TEXT NOT NULL DEFAULT '1.0.0',
        status TEXT NOT NULL DEFAULT 'COMPLETE',
        tables_total INTEGER NOT NULL DEFAULT 0, tables_profiled INTEGER NOT NULL DEFAULT 0,
        columns_total INTEGER NOT NULL DEFAULT 0, columns_profiled INTEGER NOT NULL DEFAULT 0,
        pii_columns_found INTEGER NOT NULL DEFAULT 0,
        tables_skipped INTEGER NOT NULL DEFAULT 0, tables_failed INTEGER NOT NULL DEFAULT 0,
        tables_timed_out INTEGER NOT NULL DEFAULT 0, total_rows_profiled INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at TEXT, completed_at TEXT, duration_seconds INTEGER, resumable_state_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_table_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL, table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo', table_type TEXT NOT NULL DEFAULT 'TABLE',
        exact_row_count INTEGER, estimated_row_count INTEGER, row_count_tier TEXT,
        has_date_column INTEGER NOT NULL DEFAULT 0,
        date_column_name TEXT, earliest_record TEXT, latest_record TEXT,
        data_span_days INTEGER, data_currency TEXT,
        column_count INTEGER NOT NULL DEFAULT 0, pk_column_count INTEGER NOT NULL DEFAULT 0,
        fk_count INTEGER NOT NULL DEFAULT 0, referenced_by_count INTEGER NOT NULL DEFAULT 0,
        is_junction_table INTEGER NOT NULL DEFAULT 0, is_root_table INTEGER NOT NULL DEFAULT 0,
        is_leaf_table INTEGER NOT NULL DEFAULT 0, has_identity_column INTEGER NOT NULL DEFAULT 0,
        avg_null_percentage REAL, completeness_score REAL,
        table_class TEXT, classification_confidence REAL,
        classification_evidence_json TEXT, competing_classes_json TEXT,
        classification_rule_version TEXT,
        pii_column_count INTEGER NOT NULL DEFAULT 0, confirmed_pii_count INTEGER NOT NULL DEFAULT 0,
        profiling_depth TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER, profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        skip_reason TEXT, profiled_at TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_tables (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1, table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL, schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        business_name TEXT, description TEXT, domain TEXT, grain TEXT,
        is_approved INTEGER NOT NULL DEFAULT 0, approved_by TEXT, approved_at TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE domain_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL, domain TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_domains_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE entity_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
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
        source_id INTEGER NOT NULL, snapshot_id INTEGER NOT NULL,
        from_schema TEXT NOT NULL, from_table TEXT NOT NULL,
        from_table_fqn TEXT NOT NULL, from_column TEXT NOT NULL,
        to_schema TEXT NOT NULL, to_table TEXT NOT NULL,
        to_table_fqn TEXT NOT NULL, to_column TEXT NOT NULL,
        relationship_name TEXT,
        relationship_type TEXT NOT NULL DEFAULT 'FOREIGN_KEY',
        confidence REAL NOT NULL DEFAULT 1.0,
        evidence_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        relationship_status TEXT NOT NULL DEFAULT 'AUTO'
    );
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


def _seed(path: str, *, double_fk: bool = False) -> None:
    """
    Seed source=1.  Set double_fk=True to add a second FK between orders→customers
    so MULTIPLE_DIRECT_FKS ambiguity can be tested.
    """
    conn = _db_conn(path)

    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-sem', 'Semantic Test', 'mssql', 'RELATIONAL')"
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 5)"
    )
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, tables_profiled) "
        "VALUES (1, 1, 1, 5)"
    )

    profiles = [
        # fqn, name, fkc, rbc, junction, table_class, cls_conf
        ("dbo.customers", "customers", 0, 1, 0, "Master",        0.90),
        ("dbo.orders",    "orders",    1, 1, 0, "Transactional", 0.85),
        ("dbo.invoices",  "invoices",  1, 1, 0, "Transactional", 0.80),
        ("dbo.payments",  "payments",  1, 0, 0, "Transactional", 0.75),
        ("dbo.products",  "products",  0, 0, 0, "Reference",     0.70),
    ]
    for fqn, tbl, fkc, rbc, junc, cls, cc in profiles:
        conn.execute(
            "INSERT INTO profiling_table_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
            "fk_count, referenced_by_count, is_junction_table, "
            "table_class, classification_confidence, profiling_status) "
            "VALUES (1, 1, ?, ?, 'dbo', ?, ?, ?, ?, ?, 'COMPLETE')",
            (fqn, tbl, fkc, rbc, junc, cls, cc),
        )

    fks = [
        ("orders",   "orders",   "customer_id", "dbo.customers", "dbo", "customers", "id",  "FK_orders_customers"),
        ("invoices", "invoices", "order_id",    "dbo.orders",    "dbo", "orders",    "id",  "FK_invoices_orders"),
        ("payments", "payments", "invoice_id",  "dbo.invoices",  "dbo", "invoices",  "id",  "FK_payments_invoices"),
    ]
    if double_fk:
        fks.append(
            ("orders", "orders", "billing_id", "dbo.customers", "dbo", "customers", "id", "FK_orders_customers_billing")
        )
    for tbl_name, from_tbl, from_col, to_fqn, to_schema, to_tbl, to_col, fk_name in fks:
        conn.execute(
            "INSERT INTO table_relationships "
            "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
            "from_column, to_schema, to_table, to_table_fqn, to_column, "
            "relationship_name, relationship_type, confidence) "
            "VALUES (1, 1, 'dbo', ?, ?, ?, ?, ?, ?, ?, ?, 'FOREIGN_KEY', 1.0)",
            (tbl_name, f"dbo.{from_tbl}", from_col, to_schema, to_tbl, to_fqn, to_col, fk_name),
        )

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

# T1 — direct join: orders → customers (FK outbound from orders)
def test_discover_direct_join(tmp_path, monkeypatch):
    db = str(tmp_path / "sem.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["has_direct_join"] is True
    assert result["join_count"] == 1
    join = result["recommended_join"]
    assert join is not None
    assert join["fk_direction"] == "a_references_b"
    assert join["join_columns"]["left_column"] == "customer_id"
    assert join["join_columns"]["right_column"] == "id"
    assert join["confidence"] > 0
    assert join["business_explanation"] != ""


# T2 — direct join: reversed direction (customers ↔ orders)
def test_discover_join_reversed(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_rev.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = discover_business_joins(1, "user-sem", "dbo.customers", "dbo.orders")

    assert result is not None
    assert result["has_direct_join"] is True
    # The FK is on orders.customer_id → customers.id, so direction from customers perspective = b_references_a
    assert result["recommended_join"]["fk_direction"] == "b_references_a"


# T3 — no direct join between disconnected tables
def test_discover_join_no_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_nd.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = discover_business_joins(1, "user-sem", "dbo.customers", "dbo.payments")

    assert result is not None
    assert result["has_direct_join"] is False
    assert result["recommended_join"] is None
    assert result["message"] is not None  # guidance message


# T4 — join paths: direct 1-hop path
def test_join_paths_direct(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_jp.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = discover_join_paths(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["total_paths_found"] >= 1
    assert result["shortest_path"]["hops"] == 1
    assert "dbo.customers" in result["shortest_path"]["path"]


# T5 — join paths: indirect 3-hop path (customers → orders → invoices → payments)
def test_join_paths_indirect(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ji.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = discover_join_paths(1, "user-sem", "dbo.customers", "dbo.payments")

    assert result is not None
    assert result["total_paths_found"] >= 1
    sp = result["shortest_path"]
    assert sp is not None
    assert sp["hops"] == 3
    assert sp["path"][0]  == "dbo.customers"
    assert sp["path"][-1] == "dbo.payments"


# T6 — join paths: multiple routes exist after adding a second FK
def test_join_paths_multiple_routes(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_jm.db")
    _create_db(db)
    _seed(db)

    # Add a shortcut: invoices → customers directly (new FK)
    c = _db_conn(db)
    c.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'invoices', 'dbo.invoices', 'customer_id', "
        "'dbo', 'customers', 'dbo.customers', 'id', 'FK_invoices_customers', 'FOREIGN_KEY', 1.0)"
    )
    c.commit(); c.close()

    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = discover_join_paths(1, "user-sem", "dbo.invoices", "dbo.customers")

    assert result is not None
    # Now two paths: invoices→orders→customers AND invoices→customers (direct)
    assert result["total_paths_found"] >= 2
    # Shortest path should be the direct 1-hop
    assert result["shortest_path"]["hops"] == 1


# T7 — ambiguity: MULTIPLE_DIRECT_FKS between same tables
def test_ambiguity_multiple_direct_fks(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi.db")
    _create_db(db)
    _seed(db, double_fk=True)  # adds FK_orders_customers_billing
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    types = {a["type"] for a in result["ambiguities"]}
    assert "MULTIPLE_DIRECT_FKS" in types
    assert result["is_clean"] is False
    assert result["max_severity"] in ("HIGH", "MEDIUM")


# T8 — ambiguity: MISSING_JOIN for disconnected table pair
def test_ambiguity_missing_join(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_miss.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    # customers and payments: same domain (Sales vs Finance actually)
    # use products (isolated) and payments — no join path within 3 hops
    result = detect_join_ambiguity(1, "user-sem", "dbo.customers", "dbo.products")

    assert result is not None
    # No direct FK, multiple paths checked → MISSING_JOIN expected
    types = {a["type"] for a in result["ambiguities"]}
    assert "MISSING_JOIN" in types


# T9 — ambiguity: single-table scan detects CIRCULAR_JOIN after injecting cycle
def test_ambiguity_circular_join(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_circ.db")
    _create_db(db); _seed(db)

    # Inject: customers → orders (cycle since orders already → customers)
    c = _db_conn(db)
    c.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, "
        "from_column, to_schema, to_table, to_table_fqn, to_column, "
        "relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'customers', 'dbo.customers', 'order_ref', "
        "'dbo', 'orders', 'dbo.orders', 'id', 'FK_cycle', 'FOREIGN_KEY', 0.5)"
    )
    c.commit(); c.close()

    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.customers")

    assert result is not None
    types = {a["type"] for a in result["ambiguities"]}
    assert "CIRCULAR_JOIN" in types


# T10 — semantic table profile: customers = Dimension
def test_semantic_profile_dimension(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    assert result["table_fqn"] == "dbo.customers"
    # Master class → Dimension
    assert result["semantic_role"] == "Dimension"
    assert result["semantic_role_confidence"] >= 0.7
    assert result["business_importance"]["label"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    # customers is approved → trusted = True
    assert result["trusted"] is True
    # governance_score should be positive
    assert result["governance_score"] > 0.0
    # typical_consumers: orders references customers
    consumer_fqns = {c["from_table_fqn"] for c in result["typical_consumers"]}
    assert "dbo.orders" in consumer_fqns
    # related business processes derived from domain/entity
    assert len(result["related_business_processes"]) >= 1


# T11 — semantic table profile: orders = Fact
def test_semantic_profile_fact(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof2.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = semantic_table_profile(1, "user-sem", "dbo.orders")

    assert result is not None
    assert result["semantic_role"] == "Fact"
    # orders has outgoing FK to customers
    join_fqns = {j["to_table_fqn"] for j in result["typical_joins"]}
    assert "dbo.customers" in join_fqns
    # Orders not approved yet
    assert result["governance"]["dictionary_approved"] is False


# T12 — semantic summary: correct role distribution + unknown source
def test_semantic_summary(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = semantic_summary(1, "user-sem")

    assert result is not None
    assert result["source"]["source_type"] == "mssql"
    roles = result["semantic_roles"]
    # 1 Master → 1 Dimension
    assert roles["dimension_tables"]["count"] == 1
    assert "dbo.customers" in roles["dimension_tables"]["sample"]
    # 3 Transactional → 3 Fact
    assert roles["fact_tables"]["count"] == 3
    # 1 Reference → 1 Lookup
    assert roles["lookup_tables"]["count"] == 1
    assert "dbo.products" in roles["lookup_tables"]["sample"]
    # Metrics
    metrics = result["metrics"]
    assert metrics["total_profiled_tables"] == 5
    assert metrics["total_relationships"] == 3
    assert metrics["average_join_confidence"] == 1.0  # all FKs have confidence=1.0
    assert 0.0 <= metrics["semantic_coverage"] <= 1.0

    # Unknown source returns None
    assert semantic_summary(99, "user-sem") is None
