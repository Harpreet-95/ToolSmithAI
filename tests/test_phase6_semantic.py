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
from unittest.mock import patch

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
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot


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
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

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
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

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
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

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
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

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
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

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
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))

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


# ---------------------------------------------------------------------------
# Phase 2B.1 — semantic_summary() resolver-delegation regression tests
#
# semantic_summary() now resolves prof_snap_id via the shared
# profiling_snapshot_resolver.get_latest_profiling_snapshot() instead of the
# module-local _latest_profiling_snap_id() helper (still used by the other
# five consumers in this file, unchanged). These tests pin down that the
# swap is behavior-preserving: same highest-snapshot_version, status-
# independent selection policy, same aggregation/sample/no-write behavior.
# Both data.semantic_layer_service.get_connection and
# data.profiling_snapshot_resolver.get_connection must be patched to the
# same on-disk sqlite file, since the resolver now opens its own connection.
# ---------------------------------------------------------------------------

def _patch_both_connections(monkeypatch, db: str) -> None:
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _db_conn(db))


def _insert_profiling_snapshot(db: str, *, snap_id: int, snapshot_version: int, status: str) -> None:
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, status, tables_profiled) "
        "VALUES (?, 1, 1, ?, ?, 1)",
        (snap_id, snapshot_version, status),
    )
    conn.commit()
    conn.close()


def _insert_table_profile(
    db: str, *, snap_id: int, table_fqn: str, table_name: str,
    fkc: int = 0, rbc: int = 0, junc: int = 0, table_class: str = "Master", cc: float = 0.9,
    pii: int = 0,
) -> None:
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        "fk_count, referenced_by_count, is_junction_table, "
        "table_class, classification_confidence, pii_column_count, profiling_status) "
        "VALUES (?, 1, ?, ?, 'dbo', ?, ?, ?, ?, ?, ?, 'COMPLETE')",
        (snap_id, table_fqn, table_name, fkc, rbc, junc, table_class, cc, pii),
    )
    conn.commit()
    conn.close()


# 1 — delegates_to_shared_resolver
def test_semantic_summary_delegates_to_shared_resolver(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_delegate.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        result = semantic_summary(1, "user-sem")

    assert result is not None
    spy.assert_called_once_with(1)


# 2 — latest_snapshot_version_wins
def test_semantic_summary_latest_snapshot_version_wins(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_version.db")
    _create_db(db); _seed(db)  # snapshot id=1, snapshot_version=1 (default), 5 profiles
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="COMPLETE")
    _insert_table_profile(db, snap_id=2, table_fqn="dbo.new_master", table_name="new_master", rbc=3, table_class="Master")
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    # snapshot_version=2 has exactly 1 profiled table — v1's 5 tables must not be counted
    assert result["metrics"]["total_profiled_tables"] == 1
    assert result["semantic_roles"]["dimension_tables"]["count"] == 1
    assert "dbo.new_master" in result["semantic_roles"]["dimension_tables"]["sample"]


# 3 — older_complete_beats_newer_cancelled
def test_semantic_summary_older_complete_beats_newer_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_cancelled.db")
    _create_db(db); _seed(db)  # snapshot id=1, COMPLETE, 5 profiles
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="CANCELLED")
    _insert_table_profile(db, snap_id=2, table_fqn="dbo.cancelled_table", table_name="cancelled_table", table_class="Reference")
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    # Status-aware selection: the older COMPLETE snapshot (v1, 5 profiles)
    # wins over the newer CANCELLED one (v2) — a newer non-COMPLETE snapshot
    # must never shadow older, usable COMPLETE evidence.
    assert result["metrics"]["total_profiled_tables"] == 5
    assert result["semantic_roles"]["dimension_tables"]["count"] == 1
    assert result["semantic_roles"]["fact_tables"]["count"] == 3
    assert result["semantic_roles"]["lookup_tables"]["count"] == 1
    assert "dbo.cancelled_table" not in result["semantic_roles"]["lookup_tables"]["sample"]


# 4 — older_complete_beats_newer_running
def test_semantic_summary_older_complete_beats_newer_running(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_running.db")
    _create_db(db); _seed(db)  # snapshot id=1, COMPLETE, 5 profiles
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="RUNNING")
    _insert_table_profile(db, snap_id=2, table_fqn="dbo.running_table", table_name="running_table", table_class="Transactional")
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    # Status-aware selection: the older COMPLETE snapshot (v1, 5 profiles)
    # wins over the newer RUNNING one (v2).
    assert result["metrics"]["total_profiled_tables"] == 5
    assert result["semantic_roles"]["dimension_tables"]["count"] == 1
    assert result["semantic_roles"]["fact_tables"]["count"] == 3
    assert result["semantic_roles"]["lookup_tables"]["count"] == 1
    assert "dbo.running_table" not in result["semantic_roles"]["fact_tables"]["sample"]


# 5 — no_snapshot_behavior_preserved
def test_semantic_summary_no_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_none.db")
    _create_db(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-sem', 'Semantic Test', 'mssql', 'RELATIONAL')"
    )
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 0)")
    conn.commit(); conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    assert result["metrics"]["total_profiled_tables"] == 0
    roles = result["semantic_roles"]
    assert roles["fact_tables"]["count"] == 0
    assert roles["dimension_tables"]["count"] == 0
    assert roles["lookup_tables"]["count"] == 0
    assert roles["bridge_tables"]["count"] == 0
    assert roles["fact_tables"]["sample"] == []


# 6 — class_aggregation_preserved
def test_semantic_summary_class_aggregation_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_class.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    roles = result["semantic_roles"]
    assert roles["dimension_tables"]["count"] == 1   # 1 Master -> Dimension
    assert roles["fact_tables"]["count"] == 3        # 3 Transactional -> Fact
    assert roles["lookup_tables"]["count"] == 1       # 1 Reference -> Lookup


# 7 — junction_count_preserved
def test_semantic_summary_junction_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_junction.db")
    _create_db(db); _seed(db)
    _insert_table_profile(
        db, snap_id=1, table_fqn="dbo.order_items", table_name="order_items",
        junc=1, table_class="Transactional",
    )
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    assert result["semantic_roles"]["bridge_tables"]["count"] == 1
    assert "dbo.order_items" in result["semantic_roles"]["bridge_tables"]["sample"]


# 8 — profiled_table_count_preserved
def test_semantic_summary_profiled_table_count_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_count.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    assert result["metrics"]["total_profiled_tables"] == 5


# 9 — sample_ordering_preserved
def test_semantic_summary_sample_ordering_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_order.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = semantic_summary(1, "user-sem")

    assert result is not None
    # Fact tables: orders (rbc=1), invoices (rbc=1), payments (rbc=0).
    # ORDER BY referenced_by_count DESC, table_fqn ASC as tiebreak:
    # invoices before orders (rbc tie, 'dbo.invoices' < 'dbo.orders'), payments last.
    assert result["semantic_roles"]["fact_tables"]["sample"] == [
        "dbo.invoices", "dbo.orders", "dbo.payments",
    ]


# 10 — performs_no_writes
def test_semantic_summary_performs_no_writes(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_sum_nowrites.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    conn = _db_conn(db)
    before_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    before_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    conn.close()

    semantic_summary(1, "user-sem")

    conn = _db_conn(db)
    after_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    after_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    conn.close()

    assert before_snapshots == after_snapshots
    assert before_profiles == after_profiles


# ---------------------------------------------------------------------------
# Phase 2B.2 — semantic_table_profile() resolver-delegation regression tests
#
# semantic_table_profile() now resolves prof_snap_id via the shared
# profiling_snapshot_resolver.get_latest_profiling_snapshot() instead of the
# module-local _latest_profiling_snap_id() helper (still used by the other
# four remaining consumers in this file, unchanged). Same connection-boundary
# requirement as Phase 2B.1: both data.semantic_layer_service.get_connection
# and data.profiling_snapshot_resolver.get_connection must be patched to the
# same on-disk sqlite file, reusing the _patch_both_connections /
# _insert_profiling_snapshot / _insert_table_profile helpers already added
# for semantic_summary()'s migration.
# ---------------------------------------------------------------------------

# 1 — delegates_to_shared_resolver
def test_semantic_table_profile_delegates_to_shared_resolver(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_delegate.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    spy.assert_called_once_with(1)


# 2 — latest_snapshot_version_wins
def test_semantic_table_profile_latest_snapshot_version_wins(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_version.db")
    _create_db(db); _seed(db)  # snapshot id=1, snapshot_version=1 (default)
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="COMPLETE")
    _insert_table_profile(
        db, snap_id=2, table_fqn="dbo.customers", table_name="customers",
        rbc=9, table_class="Reference", cc=0.99, pii=3,
    )
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    # v2's profile (table_class=Reference, pii_column_count=3) must win over
    # v1's seeded row (table_class=Master, pii_column_count=0) — both fields
    # are read directly off the profiling_table_profiles row, unlike
    # fk_count/referenced_by_count which are graph-degree derived and
    # therefore snapshot-independent (not useful as a selection witness).
    assert result["profiling"]["table_class"] == "Reference"
    assert result["profiling"]["pii_column_count"] == 3


# 3 — older_complete_beats_newer_cancelled
def test_semantic_table_profile_older_complete_beats_newer_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_cancelled.db")
    _create_db(db); _seed(db)  # snapshot id=1, COMPLETE
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="CANCELLED")
    _insert_table_profile(
        db, snap_id=2, table_fqn="dbo.customers", table_name="customers",
        rbc=7, table_class="Reference", cc=0.5, pii=2,
    )
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    # Status-aware selection: the older COMPLETE snapshot (v1, table_class=
    # "Master", classification_confidence=0.90, pii_column_count=0 from
    # _seed) wins over the newer CANCELLED one (v2) — a newer non-COMPLETE
    # snapshot must never shadow older, usable COMPLETE evidence.
    assert result["profiling"]["table_class"] == "Master"
    assert result["profiling"]["pii_column_count"] == 0


# 4 — older_complete_beats_newer_running
def test_semantic_table_profile_older_complete_beats_newer_running(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_running.db")
    _create_db(db); _seed(db)  # snapshot id=1, COMPLETE
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="RUNNING")
    _insert_table_profile(
        db, snap_id=2, table_fqn="dbo.customers", table_name="customers",
        rbc=4, table_class="Transactional", cc=0.6, pii=5,
    )
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    # Status-aware selection: the older COMPLETE snapshot (v1) wins over the
    # newer RUNNING one (v2).
    assert result["profiling"]["table_class"] == "Master"
    assert result["profiling"]["pii_column_count"] == 0


# 5 — no_snapshot_behavior_preserved
def test_semantic_table_profile_no_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_none.db")
    _create_db(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-sem', 'Semantic Test', 'mssql', 'RELATIONAL')"
    )
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 0)")
    conn.commit(); conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    # profile=None fallback preserved: profiling section blank, role degrades to Unknown/no-degree branch
    assert result["profiling"]["table_class"] is None
    assert result["profiling"]["pii_column_count"] == 0
    assert result["business_importance"]["score"] == 0.1  # _importance_score(None) fallback
    assert result["business_importance"]["label"] == "LOW"


# 6 — profile_fields_preserved
def test_semantic_table_profile_profile_fields_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_fields.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    assert result["table_fqn"] == "dbo.customers"
    assert result["table_name"] == "customers"
    assert result["business_domain"] == "Sales"
    assert result["business_entity"] == "Customer"
    assert result["profiling"]["table_class"] == "Master"
    assert result["profiling"]["referenced_by_count"] == 1
    assert result["profiling"]["fk_count"] == 0


# 7 — semantic_role_preserved
def test_semantic_table_profile_semantic_role_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_role.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    dim_result = semantic_table_profile(1, "user-sem", "dbo.customers")
    fact_result = semantic_table_profile(1, "user-sem", "dbo.orders")

    assert dim_result is not None and dim_result["semantic_role"] == "Dimension"
    assert fact_result is not None and fact_result["semantic_role"] == "Fact"


# 8 — importance_score_preserved
def test_semantic_table_profile_importance_score_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_importance.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    assert 0.0 <= result["business_importance"]["score"] <= 1.0
    assert result["business_importance"]["label"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")


# 9 — readiness_output_preserved (governance/trust shape)
def test_semantic_table_profile_readiness_output_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_readiness.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = semantic_table_profile(1, "user-sem", "dbo.customers")

    assert result is not None
    assert result["trusted"] is True
    gov = result["governance"]
    assert gov["dictionary_approved"] is True
    assert gov["domain_assigned"] is True
    assert gov["entity_assigned"] is True
    assert gov["profiling_complete"] is True
    assert result["governance_score"] == 1.0


# 10 — performs_no_writes
def test_semantic_table_profile_performs_no_writes(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_prof_nowrites.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    conn = _db_conn(db)
    before_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    before_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    conn.close()

    semantic_table_profile(1, "user-sem", "dbo.customers")

    conn = _db_conn(db)
    after_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    after_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    conn.close()

    assert before_snapshots == after_snapshots
    assert before_profiles == after_profiles


# ---------------------------------------------------------------------------
# Phase 2B.3 — discover_business_joins() resolver-delegation regression tests
#
# discover_business_joins() now resolves prof_snap_id via the shared
# profiling_snapshot_resolver.get_latest_profiling_snapshot() instead of the
# module-local _latest_profiling_snap_id() helper (still used by
# analyze_join_quality, recommend_best_join_path, and detect_join_ambiguity,
# unchanged). Same connection-boundary requirement as 2B.1/2B.2: both
# data.semantic_layer_service.get_connection and
# data.profiling_snapshot_resolver.get_connection must be patched to the
# same on-disk sqlite file.
#
# Unlike semantic_table_profile()'s "profiling" output dict,
# _enrich_pair()'s table_a_metadata/table_b_metadata fields (table_class,
# fk_count, referenced_by_count, pii_column_count, estimated_row_count,
# exact_row_count) are read directly off the profiling_table_profiles row
# selected by prof_snap_id — not derived from table_relationships FK-graph
# degree. table_class and pii_column_count are used below as the
# snapshot-selection witnesses.
# ---------------------------------------------------------------------------

# 1 — delegates_to_shared_resolver
def test_discover_business_joins_delegates_to_shared_resolver(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_delegate.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    spy.assert_called_once_with(1)


# 2 — latest_snapshot_version_wins
def test_discover_business_joins_latest_snapshot_version_wins(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_version.db")
    _create_db(db); _seed(db)  # snapshot id=1, snapshot_version=1 (default)
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="COMPLETE")
    _insert_table_profile(
        db, snap_id=2, table_fqn="dbo.orders", table_name="orders",
        table_class="Reference", cc=0.99, pii=6,
    )
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    # v2's profile (table_class=Reference, pii=6) must win over v1's seeded
    # value (table_class=Transactional, pii=0) for table_a (dbo.orders)
    assert result["table_a_metadata"]["table_class"] == "Reference"
    assert result["table_a_metadata"]["pii_column_count"] == 6


# 3 — older_complete_beats_newer_cancelled
def test_discover_business_joins_older_complete_beats_newer_cancelled(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_cancelled.db")
    _create_db(db); _seed(db)  # snapshot id=1, COMPLETE
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="CANCELLED")
    _insert_table_profile(
        db, snap_id=2, table_fqn="dbo.orders", table_name="orders",
        table_class="Lookup-ish", cc=0.4, pii=2,
    )
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    # Status-aware selection: the older COMPLETE snapshot (v1, table_class=
    # "Transactional", pii_column_count=0 from _seed) wins over the newer
    # CANCELLED one (v2) — a newer non-COMPLETE snapshot must never shadow
    # older, usable COMPLETE evidence.
    assert result["table_a_metadata"]["table_class"] == "Transactional"
    assert result["table_a_metadata"]["pii_column_count"] == 0


# 4 — older_complete_beats_newer_running
def test_discover_business_joins_older_complete_beats_newer_running(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_running.db")
    _create_db(db); _seed(db)  # snapshot id=1, COMPLETE
    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="RUNNING")
    _insert_table_profile(
        db, snap_id=2, table_fqn="dbo.orders", table_name="orders",
        table_class="Staging", cc=0.3, pii=9,
    )
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    # Status-aware selection: the older COMPLETE snapshot (v1) wins over the
    # newer RUNNING one (v2).
    assert result["table_a_metadata"]["table_class"] == "Transactional"
    assert result["table_a_metadata"]["pii_column_count"] == 0


# 5 — no_snapshot_behavior_preserved
def test_discover_business_joins_no_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_none.db")
    _create_db(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-sem', 'Semantic Test', 'mssql', 'RELATIONAL')"
    )
    conn.execute("INSERT INTO schema_snapshots (id, source_id, table_count) VALUES (1, 1, 2)")
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        "to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'customer_id', 'dbo', 'customers', "
        "'dbo.customers', 'id', 'FK_orders_customers', 'FOREIGN_KEY', 1.0)"
    )
    conn.commit(); conn.close()
    # Deliberately no profiling_snapshots row for source_id=1
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["has_direct_join"] is True
    # profile=None fallback preserved: profiling-derived keys never merged in
    assert "table_class" not in result["table_a_metadata"]
    assert "table_class" not in result["table_b_metadata"]
    assert result["table_a_metadata"]["table_fqn"] == "dbo.orders"


# 6 — table_profile_enrichment_preserved
def test_discover_business_joins_table_profile_enrichment_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_enrichment.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    a_meta = result["table_a_metadata"]
    assert a_meta["table_class"] == "Transactional"
    assert a_meta["fk_count"] == 1
    assert a_meta["referenced_by_count"] == 1
    assert a_meta["pii_column_count"] == 0
    assert a_meta["is_junction_table"] is False
    assert a_meta["profiling_status"] == "COMPLETE"
    b_meta = result["table_b_metadata"]
    assert b_meta["table_class"] == "Master"


# 7 — confidence_preserved
def test_discover_business_joins_confidence_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_confidence.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    join = result["recommended_join"]
    # Base FK confidence 1.0 + domain-match boost (both Sales), capped at 1.0
    assert join["confidence"] == 1.0


# 8 — result_ordering_preserved
def test_discover_business_joins_result_ordering_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_ordering.db")
    _create_db(db); _seed(db)
    conn = _db_conn(db)
    # Second, lower-confidence FK between the same two tables
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        "to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, confidence) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'billing_id', 'dbo', 'customers', "
        "'dbo.customers', 'id', 'FK_orders_customers_billing', 'FOREIGN_KEY', 0.5)"
    )
    conn.commit(); conn.close()
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["join_count"] == 2
    confidences = [result["recommended_join"]["confidence"]] + [
        j["confidence"] for j in result["alternative_joins"]
    ]
    assert confidences == sorted(confidences, reverse=True)
    # Higher-confidence edge (customer_id, ~1.0) recommended over billing_id (0.5)
    assert result["recommended_join"]["join_columns"]["left_column"] == "customer_id"


# 9 — relationship_status_behavior_preserved
def test_discover_business_joins_relationship_status_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_status.db")
    _create_db(db); _seed(db)
    conn = _db_conn(db)
    # PENDING relationship must be excluded from _load_edges (AUTO/APPROVED only)
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        "to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        "confidence, relationship_status) "
        "VALUES (1, 1, 'dbo', 'orders', 'dbo.orders', 'promo_code', 'dbo', 'products', "
        "'dbo.products', 'code', 'FK_orders_products_pending', 'FOREIGN_KEY', 0.9, 'PENDING')"
    )
    conn.commit(); conn.close()
    _patch_both_connections(monkeypatch, db)

    result = discover_business_joins(1, "user-sem", "dbo.orders", "dbo.products")

    assert result is not None
    assert result["has_direct_join"] is False


# 10 — performs_no_writes
def test_discover_business_joins_performs_no_writes(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_biz_nowrites.db")
    _create_db(db); _seed(db)
    _patch_both_connections(monkeypatch, db)

    conn = _db_conn(db)
    before_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    before_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    conn.close()

    discover_business_joins(1, "user-sem", "dbo.orders", "dbo.customers")

    conn = _db_conn(db)
    after_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    after_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    conn.close()

    assert before_snapshots == after_snapshots
    assert before_profiles == after_profiles


# ---------------------------------------------------------------------------
# Phase 2B.7 — detect_join_ambiguity() dead profiling-snapshot lookup removal
#
# detect_join_ambiguity() never had a profiling-snapshot dependency: its
# prof_snap_id was assigned and never read anywhere in the function. This
# phase removes that dead statement — no migration to the shared resolver
# occurs, since there is nothing for the resolver to feed. These tests
# demonstrate the absence of the dependency directly (forbidding both the
# old helper and the shared resolver from being called at all) and confirm
# every other behavior is byte-for-byte unchanged.
# ---------------------------------------------------------------------------

# 1 — semantic_layer_service_has_no_legacy_snapshot_helper (Phase 2B.8)
#
# Phase 2B.8 deleted _latest_profiling_snap_id()'s definition outright — it
# had zero remaining call sites after Phase 2B.7 removed the last (dead) one
# from detect_join_ambiguity(). The prior version of this test proved the
# helper wasn't called by monkeypatching it to raise; that mechanism no
# longer works once the attribute doesn't exist (monkeypatch.setattr on a
# missing attribute raises AttributeError by default), so this test now
# proves the stronger claim directly: the symbol is gone entirely.
def test_semantic_layer_service_has_no_legacy_snapshot_helper():
    import data.semantic_layer_service as sls_module
    assert not hasattr(sls_module, "_latest_profiling_snap_id")


# 2 — migrated_consumers_still_use_shared_resolver
#
# Narrow inventory assertion (not a duplicate of every prior delegation
# test): confirms, via source inspection, that each of the five functions
# migrated in Phases 2B.1-2B.6 still references the shared resolver by
# name. Full behavioral delegation coverage (spy call counts, snapshot
# policy, etc.) already exists per-function in this file and in
# tests/test_phase8_join_intelligence.py — this test only guards against a
# future edit silently reverting one of the five call sites back to the
# deleted local helper without any test noticing.
def test_migrated_consumers_still_use_shared_resolver():
    import inspect
    import data.semantic_layer_service as sls_module

    migrated = [
        sls_module.discover_business_joins,
        sls_module.analyze_join_quality,
        sls_module.recommend_best_join_path,
        sls_module.semantic_table_profile,
        sls_module.semantic_summary,
    ]
    for fn in migrated:
        source = inspect.getsource(fn)
        assert "get_latest_profiling_snapshot(" in source, (
            f"{fn.__name__} no longer appears to call the shared resolver"
        )
        assert "_latest_profiling_snap_id(" not in source, (
            f"{fn.__name__} appears to reference the retired local helper"
        )


# 3 — detect_join_ambiguity_remains_snapshot_independent
#
# Consolidates the three characterizations required by this phase into one
# test: the shared resolver is never called, zero profiling_snapshots
# queries are executed, and no resolver connection is ever opened. Reuses
# the _AmbiguityCountingConn wrapper defined below (Phase 2B.7) rather than
# duplicating a second counting mechanism.
def test_detect_join_ambiguity_remains_snapshot_independent(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_independent.db")
    _create_db(db); _seed(db)

    def _forbidden_resolver(*args, **kwargs):
        raise AssertionError("get_latest_profiling_snapshot must not be called by detect_join_ambiguity")

    counters = {
        "service_conn": 0, "resolver_conn": 0, "total_sql": 0,
        "profiling_snapshot_queries": 0, "relationship_queries": 0, "schema_queries": 0,
    }

    def counting_service_conn():
        counters["service_conn"] += 1
        return _AmbiguityCountingConn(_db_conn(db), counters)

    def resolver_conn_forbidden():
        counters["resolver_conn"] += 1
        raise AssertionError("detect_join_ambiguity must not open a resolver connection")

    monkeypatch.setattr("data.semantic_layer_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.semantic_layer_service.get_latest_profiling_snapshot", _forbidden_resolver)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", resolver_conn_forbidden)

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert counters["profiling_snapshot_queries"] == 0
    assert counters["resolver_conn"] == 0
    assert counters["service_conn"] == 1


# 4 — no_legacy_helper_reference_in_semantic_layer_service
def test_no_legacy_helper_reference_in_semantic_layer_service():
    import inspect
    import data.semantic_layer_service as sls_module
    source = inspect.getsource(sls_module)
    assert "_latest_profiling_snap_id" not in source


# 5 — does_not_call_shared_resolver (Phase 2B.7 original — retained)
def test_detect_join_ambiguity_does_not_call_shared_resolver(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_dead2.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    def _forbidden(*args, **kwargs):
        raise AssertionError("get_latest_profiling_snapshot must not be called by detect_join_ambiguity")
    monkeypatch.setattr("data.semantic_layer_service.get_latest_profiling_snapshot", _forbidden)

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None


# 3 — ambiguity_result_preserved
def test_detect_join_ambiguity_ambiguity_result_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_result.db")
    _create_db(db)
    _seed(db, double_fk=True)  # adds FK_orders_customers_billing
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    types = {a["type"] for a in result["ambiguities"]}
    assert "MULTIPLE_DIRECT_FKS" in types
    assert result["is_clean"] is False
    assert result["max_severity"] in ("HIGH", "MEDIUM")
    multi_fk = next(a for a in result["ambiguities"] if a["type"] == "MULTIPLE_DIRECT_FKS")
    assert len(multi_fk["edges"]) == 2
    assert multi_fk["severity"] == "MEDIUM"
    assert multi_fk["recommendation"] != ""


# 4 — no_ambiguity_result_preserved
def test_detect_join_ambiguity_no_ambiguity_result_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_clean.db")
    _create_db(db); _seed(db)  # single FK, no double_fk -> clean
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["ambiguities"] == []
    assert result["total_ambiguities"] == 0
    assert result["max_severity"] is None
    assert result["is_clean"] is True


# 5 — pending_or_untrusted_relationship_behavior_preserved
def test_detect_join_ambiguity_pending_or_untrusted_relationship_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_pending.db")
    _create_db(db); _seed(db)
    # PENDING relationship between two otherwise-disconnected tables must be
    # excluded from the join graph (_load_edges: AUTO/APPROVED only), so it
    # must NOT prevent MISSING_JOIN from firing for that pair.
    c = _db_conn(db)
    c.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        "to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        "confidence, relationship_status) "
        "VALUES (1, 1, 'dbo', 'customers', 'dbo.customers', 'product_ref', 'dbo', 'products', "
        "'dbo.products', 'id', 'FK_pending', 'FOREIGN_KEY', 0.9, 'PENDING')"
    )
    c.commit(); c.close()
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.customers", "dbo.products")

    assert result is not None
    types = {a["type"] for a in result["ambiguities"]}
    assert "MISSING_JOIN" in types  # PENDING edge did not silently connect the pair


# 6 — no_schema_snapshot_behavior_preserved
def test_detect_join_ambiguity_no_schema_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_noschema.db")
    _create_db(db)
    conn = _db_conn(db)
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id, display_name, source_type, source_category) "
        "VALUES (1, 'user-sem', 'Semantic Test', 'mssql', 'RELATIONAL')"
    )
    # Deliberately no schema_snapshots row for source_id=1
    conn.commit(); conn.close()
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["ambiguities"] == []
    assert result["total_ambiguities"] == 0
    assert result["max_severity"] is None
    assert result["message"] == "No schema snapshot available."


# 7 — response_shape_preserved
def test_detect_join_ambiguity_response_shape_preserved(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_shape.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert set(result.keys()) == {
        "source_id", "table_a", "table_b",
        "ambiguities", "total_ambiguities", "max_severity", "is_clean",
    }
    assert result["source_id"] == 1
    assert result["table_a"] == "dbo.orders"
    assert result["table_b"] == "dbo.customers"


# 8 — performs_no_writes
def test_detect_join_ambiguity_performs_no_writes(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_nowrites.db")
    _create_db(db); _seed(db)
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda: _db_conn(db))

    conn = _db_conn(db)
    before_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    before_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    before_rels = [dict(r) for r in conn.execute("SELECT * FROM table_relationships").fetchall()]
    conn.close()

    detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    conn = _db_conn(db)
    after_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    after_profiles = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    after_rels = [dict(r) for r in conn.execute("SELECT * FROM table_relationships").fetchall()]
    conn.close()

    assert before_snapshots == after_snapshots
    assert before_profiles == after_profiles
    assert before_rels == after_rels


# 9 — query-count validation: zero profiling_snapshots queries, no resolver
# connection opened, relationship/schema queries remain present.
class _AmbiguityCountingConn:
    def __init__(self, real_conn, counters):
        self._real = real_conn
        self._counters = counters

    def execute(self, sql, *args, **kwargs):
        self._counters["total_sql"] += 1
        if "FROM profiling_snapshots" in sql:
            self._counters["profiling_snapshot_queries"] += 1
        if "FROM table_relationships" in sql:
            self._counters["relationship_queries"] += 1
        if "FROM schema_snapshots" in sql:
            self._counters["schema_queries"] += 1
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_detect_join_ambiguity_query_count(tmp_path, monkeypatch):
    db = str(tmp_path / "sem_ambi_querycount.db")
    _create_db(db); _seed(db)

    counters = {
        "service_conn": 0, "resolver_conn": 0, "total_sql": 0,
        "profiling_snapshot_queries": 0, "relationship_queries": 0, "schema_queries": 0,
    }

    def counting_service_conn():
        counters["service_conn"] += 1
        return _AmbiguityCountingConn(_db_conn(db), counters)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _AmbiguityCountingConn(_db_conn(db), counters)

    monkeypatch.setattr("data.semantic_layer_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = detect_join_ambiguity(1, "user-sem", "dbo.orders", "dbo.customers")

    assert result is not None
    assert counters["profiling_snapshot_queries"] == 0
    assert counters["relationship_queries"] >= 1
    assert counters["schema_queries"] >= 1
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 0  # no resolver connection ever opened
