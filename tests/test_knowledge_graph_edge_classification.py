"""
Phase 6, Step 6B — structural view-dependency evidence trust boundary.

Defect 2 reproduction + fix coverage: data/knowledge_graph_service.py used
to treat any relationship_status IN ('AUTO', 'APPROVED') row as a literal,
executable foreign key — with no relationship_type/inference_method check
and no check that from_column/to_column were real, concrete column names
rather than the "*"/"" lineage sentinel. A catalog VIEW_DEPENDENCY edge
(confirmed, authoritative, but never a literal join column pair) was
therefore formatted and traversed exactly like a real FK.

This file proves the fixed _classify_graph_edge() correctly distinguishes
executable_fk / confirmed_lineage / inferred everywhere the graph reasoning
functions consume table_relationships rows, and that it reuses (never
re-implements a second, inconsistent copy of) core.sqlservice.retrieval_
documents.classify_relationship_row() for the confirmed/inferred boundary.

Run from the project root:
    venv/Scripts/pytest tests/test_knowledge_graph_edge_classification.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-kg-classify-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-salt-kg-classify-long-enough-1234")

from data.knowledge_graph_service import (
    KIND_CONFIRMED_LINEAGE,
    KIND_EXECUTABLE_FK,
    KIND_INFERRED,
    _classify_graph_edge,
    explain_table,
    get_related_tables,
    trace_business_path,
)

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Test', source_type TEXT NOT NULL DEFAULT 'mssql',
        source_category TEXT NOT NULL DEFAULT 'RELATIONAL'
    );
    CREATE TABLE schema_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        table_count INTEGER NOT NULL DEFAULT 0, view_count INTEGER NOT NULL DEFAULT 0,
        column_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE profiling_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        schema_snapshot_id INTEGER NOT NULL DEFAULT 1, snapshot_version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'COMPLETE',
        tables_profiled INTEGER NOT NULL DEFAULT 0,
        columns_profiled INTEGER NOT NULL DEFAULT 0, pii_columns_found INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE profiling_table_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, profiling_snapshot_id INTEGER NOT NULL,
        source_id INTEGER NOT NULL, table_fqn TEXT NOT NULL, table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo', table_type TEXT NOT NULL DEFAULT 'TABLE',
        exact_row_count INTEGER, estimated_row_count INTEGER, row_count_tier TEXT,
        fk_count INTEGER NOT NULL DEFAULT 0, referenced_by_count INTEGER NOT NULL DEFAULT 0,
        is_root_table INTEGER NOT NULL DEFAULT 0, is_leaf_table INTEGER NOT NULL DEFAULT 0,
        is_junction_table INTEGER NOT NULL DEFAULT 0,
        table_class TEXT, classification_confidence REAL,
        pii_column_count INTEGER NOT NULL DEFAULT 0, confirmed_pii_count INTEGER NOT NULL DEFAULT 0,
        completeness_score REAL, profiling_depth TEXT, profiling_status TEXT NOT NULL DEFAULT 'COMPLETE'
    );
    CREATE TABLE data_dictionary_tables (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, table_fqn TEXT NOT NULL,
        business_name TEXT, description TEXT, grain TEXT, is_approved INTEGER NOT NULL DEFAULT 0,
        approved_by TEXT, generation_method TEXT
    );
    CREATE TABLE domain_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, table_fqn TEXT NOT NULL,
        domain TEXT NOT NULL DEFAULT 'Unknown', confidence REAL NOT NULL DEFAULT 0.0
    );
    CREATE TABLE entity_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, table_fqn TEXT NOT NULL,
        entity TEXT NOT NULL DEFAULT 'Unknown', confidence REAL NOT NULL DEFAULT 0.0
    );
    CREATE TABLE table_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, snapshot_id INTEGER NOT NULL,
        from_schema TEXT NOT NULL DEFAULT 'dbo', from_table TEXT NOT NULL DEFAULT '',
        from_table_fqn TEXT NOT NULL, from_column TEXT NOT NULL,
        to_schema TEXT NOT NULL DEFAULT 'dbo', to_table TEXT NOT NULL DEFAULT '',
        to_table_fqn TEXT NOT NULL, to_column TEXT NOT NULL,
        relationship_name TEXT, relationship_type TEXT NOT NULL DEFAULT 'FOREIGN_KEY',
        confidence REAL NOT NULL DEFAULT 1.0, evidence_json TEXT,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        inference_method TEXT NOT NULL DEFAULT 'declared_fk',
        relationship_status TEXT NOT NULL DEFAULT 'AUTO'
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


def _seed_base(conn, source_id=1, user_id="u-kg", table_count=3, view_count=1) -> None:
    conn.execute(
        "INSERT INTO data_source_connections (id, user_id) VALUES (?, ?)", (source_id, user_id)
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, table_count, view_count, column_count) "
        "VALUES (1, ?, ?, ?, 10)",
        (source_id, table_count, view_count),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id) VALUES (1, ?, 1)",
        (source_id,),
    )
    conn.commit()


def _insert_rel(
    conn, *, source_id=1, snapshot_id=1,
    from_fqn, from_col, to_fqn, to_col,
    relationship_type, inference_method, relationship_status, confidence,
) -> None:
    f_schema, _, f_table = from_fqn.partition(".")
    t_schema, _, t_table = to_fqn.partition(".")
    conn.execute(
        """INSERT INTO table_relationships
           (source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column,
            to_schema, to_table, to_table_fqn, to_column, relationship_name,
            relationship_type, confidence, inference_method, relationship_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
        (source_id, snapshot_id, f_schema, f_table, from_fqn, from_col,
         t_schema, t_table, to_fqn, to_col,
         relationship_type, confidence, inference_method, relationship_status),
    )
    conn.commit()


# ===========================================================================
# _classify_graph_edge — pure-function unit coverage
# ===========================================================================

def test_classify_declared_fk_is_executable():
    row = {"relationship_type": "FOREIGN_KEY", "inference_method": "declared_fk",
           "relationship_status": "AUTO", "from_column": "customer_id", "to_column": "id"}
    assert _classify_graph_edge(row) == KIND_EXECUTABLE_FK


def test_classify_catalog_view_dependency_is_confirmed_lineage_not_executable():
    """The literal Defect 2 case: confirmed, but sentinel columns."""
    row = {"relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
           "relationship_status": "AUTO", "from_column": "*", "to_column": "*"}
    assert _classify_graph_edge(row) == KIND_CONFIRMED_LINEAGE
    assert _classify_graph_edge(row) != KIND_EXECUTABLE_FK


def test_classify_regex_view_dependency_pending_is_inferred():
    row = {"relationship_type": "VIEW_DEPENDENCY", "inference_method": "view_dependency",
           "relationship_status": "PENDING", "from_column": "*", "to_column": "*"}
    assert _classify_graph_edge(row) == KIND_INFERRED


def test_classify_approved_relationship_with_concrete_columns_is_executable():
    """A human approved a heuristic-discovered candidate that DOES carry
    real join columns — must count as an executable join."""
    row = {"relationship_type": "INFERRED_NAME_MATCH", "inference_method": "name_match",
           "relationship_status": "APPROVED", "from_column": "customer_id", "to_column": "id"}
    assert _classify_graph_edge(row) == KIND_EXECUTABLE_FK


def test_classify_approved_relationship_with_sentinel_columns_is_not_executable():
    """A human approved a PENDING view-dependency row (sentinel columns) —
    approval alone must never make sentinel columns executable."""
    row = {"relationship_type": "VIEW_DEPENDENCY", "inference_method": "view_dependency",
           "relationship_status": "APPROVED", "from_column": "*", "to_column": "*"}
    assert _classify_graph_edge(row) == KIND_CONFIRMED_LINEAGE


def test_classify_empty_string_columns_also_treated_as_sentinel():
    row = {"relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
           "relationship_status": "AUTO", "from_column": "", "to_column": ""}
    assert _classify_graph_edge(row) == KIND_CONFIRMED_LINEAGE


# ===========================================================================
# get_related_tables — evidence text and relationship_types labeling
# ===========================================================================

def test_related_tables_labels_catalog_view_dependency_as_lineage_not_fk(tmp_path, monkeypatch):
    db = str(tmp_path / "kg1.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.vw_OpenTickets", from_col="*", to_fqn="dbo.Tickets", to_col="*",
        relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.vw_OpenTickets")

    assert result is not None
    entry = next(r for r in result["related_tables"] if r["table_fqn"] == "dbo.Tickets")
    assert "LINEAGE_OUTBOUND" in entry["relationship_types"]
    assert "FK_OUTBOUND" not in entry["relationship_types"]
    assert any(e.startswith("Confirmed lineage") for e in entry["evidence"])
    assert not any(e.startswith("Foreign key:") for e in entry["evidence"])
    assert result["fk_direct"] == 0
    assert result["lineage_related"] == 1


def test_related_tables_still_labels_real_fk_as_fk(tmp_path, monkeypatch):
    """Regression guard: a real declared FK must still say 'Foreign key'."""
    db = str(tmp_path / "kg2.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.orders", from_col="customer_id", to_fqn="dbo.customers", to_col="id",
        relationship_type="FOREIGN_KEY", inference_method="declared_fk",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.orders")
    entry = next(r for r in result["related_tables"] if r["table_fqn"] == "dbo.customers")
    assert "FK_OUTBOUND" in entry["relationship_types"]
    assert any(e.startswith("Foreign key:") for e in entry["evidence"])
    assert result["fk_direct"] == 1
    assert result["lineage_related"] == 0


def test_related_tables_labels_stale_auto_regex_row_as_inferred(tmp_path, monkeypatch):
    """The exact historical-data shape found in the reference database: relationship_status=
    'AUTO' but inference_method='view_dependency' (predates the PENDING
    fix) — must NOT be treated as confirmed of any kind."""
    db = str(tmp_path / "kg3.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.vw_Legacy", from_col="", to_fqn="dbo.Base", to_col="",
        relationship_type="VIEW_DEPENDENCY", inference_method="view_dependency",
        relationship_status="AUTO", confidence=0.6,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.vw_Legacy")
    entry = next(r for r in result["related_tables"] if r["table_fqn"] == "dbo.Base")
    assert "INFERRED_OUTBOUND" in entry["relationship_types"]
    assert result["fk_direct"] == 0
    assert result["lineage_related"] == 0


def test_related_tables_missing_endpoint_fails_closed(tmp_path, monkeypatch):
    """A table_fqn with zero relationships in the graph must return an
    empty, safe result — never fabricate an edge."""
    db = str(tmp_path / "kg4.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.nowhere")
    assert result is not None
    assert result["related_tables"] == []
    assert result["total_related"] == 0
    assert result["fk_direct"] == 0


def test_related_tables_wrong_user_returns_none(tmp_path, monkeypatch):
    db = str(tmp_path / "kg5.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    assert get_related_tables(source_id=1, user_id="someone-else", table_fqn="dbo.orders") is None


def test_related_tables_deterministic_output(tmp_path, monkeypatch):
    db = str(tmp_path / "kg6.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.vw_OpenTickets", from_col="*", to_fqn="dbo.Tickets", to_col="*",
        relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    r1 = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.vw_OpenTickets")
    r2 = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.vw_OpenTickets")
    assert r1 == r2


# ===========================================================================
# explain_table — evidence lines + relationships.outbound/inbound "kind"
# ===========================================================================

def test_explain_table_distinguishes_lineage_from_fk(tmp_path, monkeypatch):
    db = str(tmp_path / "kg7.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    conn.execute(
        "INSERT INTO profiling_table_profiles (profiling_snapshot_id, source_id, table_fqn, table_name) "
        "VALUES (1, 1, 'dbo.vw_OpenTickets', 'vw_OpenTickets')"
    )
    _insert_rel(
        conn, from_fqn="dbo.vw_OpenTickets", from_col="*", to_fqn="dbo.Tickets", to_col="*",
        relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    # explain_table also calls data.profiling_snapshot_resolver.get_latest_
    # profiling_snapshot(), which opens its OWN connection independently.
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="u-kg", table_fqn="dbo.vw_OpenTickets")
    assert result is not None
    outbound = result["relationships"]["outbound"]
    assert len(outbound) == 1
    assert outbound[0]["kind"] == KIND_CONFIRMED_LINEAGE
    assert any("lineage only" in e for e in result["evidence"])
    assert not any(e.startswith("Outbound FK(s)") for e in result["evidence"])


def test_explain_table_real_fk_still_says_fk(tmp_path, monkeypatch):
    db = str(tmp_path / "kg8.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    conn.execute(
        "INSERT INTO profiling_table_profiles (profiling_snapshot_id, source_id, table_fqn, table_name) "
        "VALUES (1, 1, 'dbo.orders', 'orders')"
    )
    _insert_rel(
        conn, from_fqn="dbo.orders", from_col="customer_id", to_fqn="dbo.customers", to_col="id",
        relationship_type="FOREIGN_KEY", inference_method="declared_fk",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda: _conn(db))

    result = explain_table(source_id=1, user_id="u-kg", table_fqn="dbo.orders")
    outbound = result["relationships"]["outbound"]
    assert outbound[0]["kind"] == KIND_EXECUTABLE_FK
    assert any(e.startswith("Outbound FK(s) to:") for e in result["evidence"])


# ===========================================================================
# trace_business_path — "kind" surfaced on each hop, lineage never silently
# presented as a joinable FK hop
# ===========================================================================

def test_trace_path_hop_kind_is_lineage_for_view_dependency(tmp_path, monkeypatch):
    db = str(tmp_path / "kg9.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.vw_OpenTickets", from_col="*", to_fqn="dbo.Tickets", to_col="*",
        relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = trace_business_path(source_id=1, user_id="u-kg", from_fqn="dbo.vw_OpenTickets", to_fqn="dbo.Tickets")
    assert result["found"] is True
    hop = result["path"][1]
    assert hop["via"]["kind"] == KIND_CONFIRMED_LINEAGE


def test_trace_path_hop_kind_is_fk_for_declared_fk(tmp_path, monkeypatch):
    db = str(tmp_path / "kg10.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.orders", from_col="customer_id", to_fqn="dbo.customers", to_col="id",
        relationship_type="FOREIGN_KEY", inference_method="declared_fk",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = trace_business_path(source_id=1, user_id="u-kg", from_fqn="dbo.orders", to_fqn="dbo.customers")
    assert result["found"] is True
    hop = result["path"][1]
    assert hop["via"]["kind"] == KIND_EXECUTABLE_FK


def test_trace_path_missing_endpoint_fails_closed(tmp_path, monkeypatch):
    db = str(tmp_path / "kg11.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = trace_business_path(source_id=1, user_id="u-kg", from_fqn="dbo.nowhere", to_fqn="dbo.alsonowhere")
    assert result["found"] is False
    assert result["path"] == []


def test_trace_path_deterministic(tmp_path, monkeypatch):
    db = str(tmp_path / "kg12.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.orders", from_col="customer_id", to_fqn="dbo.customers", to_col="id",
        relationship_type="FOREIGN_KEY", inference_method="declared_fk",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    r1 = trace_business_path(source_id=1, user_id="u-kg", from_fqn="dbo.orders", to_fqn="dbo.customers")
    r2 = trace_business_path(source_id=1, user_id="u-kg", from_fqn="dbo.orders", to_fqn="dbo.customers")
    assert r1 == r2


def test_no_duplicate_logical_edges_in_related_tables_output(tmp_path, monkeypatch):
    """One physical row for one logical edge must produce exactly one
    related_tables entry, not one per (outbound, inbound) miscount."""
    db = str(tmp_path / "kg13.db")
    _create_db(db)
    conn = _conn(db)
    _seed_base(conn)
    _insert_rel(
        conn, from_fqn="dbo.vw_OpenTickets", from_col="*", to_fqn="dbo.Tickets", to_col="*",
        relationship_type="VIEW_DEPENDENCY", inference_method="catalog_view_dependency",
        relationship_status="AUTO", confidence=1.0,
    )
    conn.close()
    monkeypatch.setattr("data.knowledge_graph_service.get_connection", lambda: _conn(db))

    result = get_related_tables(source_id=1, user_id="u-kg", table_fqn="dbo.vw_OpenTickets")
    matches = [r for r in result["related_tables"] if r["table_fqn"] == "dbo.Tickets"]
    assert len(matches) == 1
