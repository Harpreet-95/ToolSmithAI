"""
Focused unit tests — SQL rebuild Phase 5, Step 6: authoritative SQL Server
view-dependency discovery (data/view_dependency_service.py) and its
connection-time bootstrap wiring (data/datasource_service.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db. The live SQL Server catalog query itself is always
faked (a scripted cursor) — this file never opens a real ODBC connection
and never depends on pyodbc/a real driver being installed.

Run:
    venv/Scripts/pytest tests/test_view_dependency_service.py -v
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import data.view_dependency_service as view_dependency_service_module
import data.datasource_service as datasource_service_module
import data.relationship_service as relationship_service_module
import core.connectors.registry as registry_module
from core.profiling.sql.mssql import build_view_dependency_query
from data.relationship_service import get_relationships_for_source
from data.view_dependency_service import (
    STATUS_CONNECTION_FAILED,
    STATUS_NO_SNAPSHOT,
    STATUS_NOT_MSSQL,
    STATUS_OK,
    STATUS_QUERY_FAILED,
    STATUS_UNOWNED,
    discover_and_persist_view_dependencies,
)

_NOW = "2026-09-08T00:00:00Z"


# ── Fixture plumbing ─────────────────────────────────────────────────────


def _make_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("template") / "template.db")
    original = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original
    return path


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(view_dependency_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(datasource_service_module, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id=1, user_id="alice", source_type="mssql"):
    conn = _make_connection(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " created_at, updated_at) VALUES (?, ?, 'Test Source', ?, 'database', '{}', ?, ?)",
        (source_id, user_id, source_type, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, source_id=1, snapshot_id=1, version=1):
    conn = _make_connection(db_path)
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_fk_relationship(db_path, *, source_id=1, snapshot_id=1, from_fqn="dbo.a", from_col="b_id", to_fqn="dbo.b", to_col="id"):
    conn = _make_connection(db_path)
    from_schema, from_table = from_fqn.split(".", 1)
    to_schema, to_table = to_fqn.split(".", 1)
    conn.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fk_x', 'FOREIGN_KEY', 1.0, '{}', ?)",
        (source_id, snapshot_id, from_schema, from_table, from_fqn, from_col, to_schema, to_table, to_fqn, to_col, _NOW),
    )
    conn.commit()
    conn.close()


class _FakeCursor:
    """Scripted stand-in for a pyodbc cursor — never opens a real
    connection, never touches an actual database. `rows` are RAW tuples in
    (view_schema, view_name, ref_schema, ref_name, ref_type) order, exactly
    what a real pyodbc cursor.fetchall() over build_view_dependency_query()
    would return."""

    _COLUMNS = ("view_schema", "view_name", "ref_schema", "ref_name", "ref_type")

    def __init__(self, rows=(), *, raise_on_execute=None):
        self._rows = list(rows)
        self._raise_on_execute = raise_on_execute
        self.description = [(c,) for c in self._COLUMNS]
        self.executed_sql = None
        self.timeout = None

    def execute(self, sql):
        self.executed_sql = sql
        if self._raise_on_execute is not None:
            raise self._raise_on_execute

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeLiveConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class _FakeMssqlConnector:
    """Minimal stand-in registered under registry.get('mssql') for one
    test — never the real SQLServerConnector, so no pyodbc/driver
    dependency and no risk of colliding with the real class's own
    @register call (this is never passed through registry.register())."""

    def __init__(self, live_connection):
        self._live_connection = live_connection

    def __call__(self):
        return self

    def open_connection(self, config, timeout_s=None):
        return self._live_connection


def _install_fake_registry(monkeypatch, cursor):
    live_connection = _FakeLiveConnection(cursor)
    fake_connector_cls = _FakeMssqlConnector(live_connection)
    monkeypatch.setattr(registry_module, "get", lambda source_type: fake_connector_cls if source_type == "mssql" else None)
    return live_connection


def _install_fake_get_connection_config(monkeypatch, *, source_type="mssql"):
    monkeypatch.setattr(
        datasource_service_module, "get_connection_config",
        lambda source_id, user_id: {"source_type": source_type, "params": {}},
    )


def _prepared(db_path, monkeypatch, cursor, *, source_id=1, user_id="alice", snapshot_id=1):
    _insert_source(db_path, source_id=source_id, user_id=user_id)
    _insert_snapshot(db_path, source_id=source_id, snapshot_id=snapshot_id)
    _install_fake_get_connection_config(monkeypatch)
    return _install_fake_registry(monkeypatch, cursor)


def _all_relationships(db_path, source_id=1):
    conn = _make_connection(db_path)
    rows = conn.execute("SELECT * FROM table_relationships WHERE source_id = ?", (source_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 1-3. view -> table, view -> view, multiple dependencies ─────────────


def test_view_to_table_dependency_discovered_and_persisted(db_path, monkeypatch):
    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)

    result = discover_and_persist_view_dependencies(1, "alice")

    assert result.status == STATUS_OK
    assert result.view_table_edges == 1
    assert result.view_view_edges == 0
    assert result.edges_persisted == 1
    rows = _all_relationships(db_path)
    assert len(rows) == 1
    assert rows[0]["relationship_type"] == "VIEW_DEPENDENCY"
    assert rows[0]["from_table_fqn"] == "dbo.vw_x"
    assert rows[0]["to_table_fqn"] == "dbo.base_table"


def test_view_to_view_dependency_discovered(db_path, monkeypatch):
    cursor = _FakeCursor([("dbo", "vw_outer", "dbo", "vw_inner", "V")])
    _prepared(db_path, monkeypatch, cursor)

    result = discover_and_persist_view_dependencies(1, "alice")

    assert result.status == STATUS_OK
    assert result.view_view_edges == 1
    assert result.view_table_edges == 0


def test_multiple_dependencies_across_views(db_path, monkeypatch):
    cursor = _FakeCursor([
        ("dbo", "vw_a", "dbo", "t1", "U"),
        ("dbo", "vw_a", "dbo", "t2", "U"),
        ("dbo", "vw_b", "dbo", "vw_a", "V"),
    ])
    _prepared(db_path, monkeypatch, cursor)

    result = discover_and_persist_view_dependencies(1, "alice")

    assert result.status == STATUS_OK
    assert result.view_table_edges == 2
    assert result.view_view_edges == 1
    assert result.views_with_dependencies == 2
    assert result.edges_persisted == 3


# ── 4. Duplicate catalog rows ─────────────────────────────────────────────


def test_duplicate_catalog_rows_collapse_to_one_edge(db_path, monkeypatch):
    cursor = _FakeCursor([
        ("dbo", "vw_x", "dbo", "base_table", "U"),
        ("dbo", "vw_x", "dbo", "base_table", "U"),  # same pair, e.g. two referenced columns
    ])
    _prepared(db_path, monkeypatch, cursor)

    result = discover_and_persist_view_dependencies(1, "alice")

    assert result.view_table_edges == 1
    assert result.edges_persisted == 1
    assert len(_all_relationships(db_path)) == 1


# ── 5-7. Query-text exclusions (cross-db, unresolved, system/self) ──────


def test_query_excludes_unresolved_references():
    sql = build_view_dependency_query()
    assert "d.referenced_id IS NOT NULL" in sql


def test_query_excludes_cross_database_and_linked_server_references():
    sql = build_view_dependency_query()
    assert "referenced_server_name" in sql
    assert "referenced_database_name" in sql
    assert "DB_NAME()" in sql


def test_query_excludes_ambiguous_references():
    sql = build_view_dependency_query()
    assert "is_ambiguous = 0" in sql


def test_query_excludes_system_objects():
    sql = build_view_dependency_query()
    assert "is_ms_shipped = 0" in sql
    assert "'sys'" in sql


def test_query_excludes_self_references():
    sql = build_view_dependency_query()
    assert "referenced_id <> v.object_id" in sql


def test_query_only_tables_and_views():
    sql = build_view_dependency_query()
    assert "'U', 'V'" in sql
    # Functions/procedures/triggers are never in this allow-list.
    for banned_type in ("'P'", "'FN'", "'TF'", "'TR'"):
        assert banned_type not in sql


def test_query_never_parses_view_sql_text():
    """Structural guard: this module must never read/parse the view's own
    definition text — it only ever queries the compiled dependency
    catalog."""
    sql = build_view_dependency_query()
    for banned in ("OBJECT_DEFINITION", "sys.sql_modules", "sm.definition"):
        assert banned not in sql


# ── 8. Canonical FQN casing preserved ─────────────────────────────────────


def test_canonical_fqn_casing_preserved_exactly(db_path, monkeypatch):
    cursor = _FakeCursor([("Dbo", "Vw_MixedCase", "dbo", "Base_Table", "U")])
    _prepared(db_path, monkeypatch, cursor)

    discover_and_persist_view_dependencies(1, "alice")

    rows = _all_relationships(db_path)
    assert rows[0]["from_table_fqn"] == "Dbo.Vw_MixedCase"
    assert rows[0]["to_table_fqn"] == "dbo.Base_Table"


# ── 9. Snapshot / source isolation ────────────────────────────────────────


def test_snapshot_isolation_stale_rows_not_visible_under_a_different_snapshot(db_path, monkeypatch):
    _insert_source(db_path, source_id=1)
    _insert_snapshot(db_path, source_id=1, snapshot_id=1)
    _insert_snapshot(db_path, source_id=1, snapshot_id=2, version=2)
    _install_fake_get_connection_config(monkeypatch)

    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _install_fake_registry(monkeypatch, cursor)

    result = discover_and_persist_view_dependencies(1, "alice", snapshot_id=1)
    assert result.status == STATUS_OK
    assert result.snapshot_id == 1

    monkeypatch.setattr(relationship_service_module, "get_connection", lambda: _make_connection(db_path))
    for_snapshot_1 = get_relationships_for_source(1, "alice", snapshot_id=1)
    for_snapshot_2 = get_relationships_for_source(1, "alice", snapshot_id=2)
    assert len(for_snapshot_1) == 1
    assert len(for_snapshot_2) == 0  # never leaks into a different snapshot


def test_source_isolation_different_sources_never_cross_contaminate(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    _insert_source(db_path, source_id=2, user_id="alice")
    _insert_snapshot(db_path, source_id=1, snapshot_id=1)
    _insert_snapshot(db_path, source_id=2, snapshot_id=2)
    _install_fake_get_connection_config(monkeypatch)

    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _install_fake_registry(monkeypatch, cursor)

    discover_and_persist_view_dependencies(1, "alice")

    assert len(_all_relationships(db_path, source_id=1)) == 1
    assert len(_all_relationships(db_path, source_id=2)) == 0


# ── 10. Atomic — a query failure persists nothing ─────────────────────────


def test_query_failure_persists_nothing(db_path, monkeypatch):
    cursor = _FakeCursor(raise_on_execute=RuntimeError("driver exploded with password=hunter2"))
    _prepared(db_path, monkeypatch, cursor)

    result = discover_and_persist_view_dependencies(1, "alice")

    assert result.status == STATUS_QUERY_FAILED
    assert result.edges_persisted == 0
    assert _all_relationships(db_path) == []


# ── 11. Idempotent rerun ───────────────────────────────────────────────────


def test_idempotent_rerun_does_not_duplicate(db_path, monkeypatch):
    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U"), ("dbo", "vw_y", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)

    first = discover_and_persist_view_dependencies(1, "alice")
    second = discover_and_persist_view_dependencies(1, "alice")

    assert first.edges_persisted == 2
    assert second.status == STATUS_OK
    assert second.edges_persisted == 0  # nothing new — already recorded
    assert len(_all_relationships(db_path)) == 2


# ── 12. FK relationships (and unrelated types) preserved ──────────────────


def test_declared_fk_relationships_untouched(db_path, monkeypatch):
    _insert_source(db_path, source_id=1)
    _insert_snapshot(db_path, source_id=1, snapshot_id=1)
    _insert_fk_relationship(db_path, source_id=1, snapshot_id=1)
    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _install_fake_get_connection_config(monkeypatch)
    _install_fake_registry(monkeypatch, cursor)

    before = _all_relationships(db_path)
    assert any(r["relationship_type"] == "FOREIGN_KEY" for r in before)

    discover_and_persist_view_dependencies(1, "alice")

    after = _all_relationships(db_path)
    fk_rows_after = [r for r in after if r["relationship_type"] == "FOREIGN_KEY"]
    assert len(fk_rows_after) == 1
    assert fk_rows_after[0]["from_table_fqn"] == "dbo.a"
    view_dep_rows = [r for r in after if r["relationship_type"] == "VIEW_DEPENDENCY"]
    assert len(view_dep_rows) == 1


# ── 13. Correct confirmed classification ───────────────────────────────────


def test_persisted_rows_classify_as_confirmed(db_path, monkeypatch):
    from core.sqlservice.retrieval_documents import classify_relationship_row

    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)

    discover_and_persist_view_dependencies(1, "alice")

    row = _all_relationships(db_path)[0]
    assert classify_relationship_row(row) == "confirmed"
    assert row["inference_method"] == "catalog_view_dependency"
    assert row["relationship_status"] == "AUTO"


# ── 14-15. Inbound/outbound document metadata + relationship-graph visibility ──


def test_confirmed_edges_reach_retrieval_document_metadata_both_directions(db_path, monkeypatch):
    from core.connectors.schema import SchemaInfo, SchemaSnapshot, TableInfo, ColumnInfo, PrimaryKeyInfo
    from core.sqlservice.retrieval_documents import build_retrieval_documents

    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)
    discover_and_persist_view_dependencies(1, "alice")
    relationships = _all_relationships(db_path)

    snapshot = SchemaSnapshot(
        source_id=1, source_type="mssql", discovered_at=_NOW,
        schemas=[SchemaInfo(schema_name="dbo", tables=[
            TableInfo(table_name="vw_x", schema_name="dbo", table_fqn="dbo.vw_x", table_type="VIEW",
                      columns=[ColumnInfo("id", 1, "INTEGER", "int", True, False, False)],
                      primary_keys=[], foreign_keys=[]),
            TableInfo(table_name="base_table", schema_name="dbo", table_fqn="dbo.base_table", table_type="TABLE",
                      columns=[ColumnInfo("id", 1, "INTEGER", "int", False, True, True)],
                      primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[]),
        ])],
    )
    docs = build_retrieval_documents(1, snapshot, relationships=relationships)
    by_fqn = {d.object_fqn: d for d in docs}

    outbound_targets = by_fqn["dbo.vw_x"].metadata["confirmed_relationship_targets"]
    inbound_sources = by_fqn["dbo.base_table"].metadata["confirmed_relationship_sources"]
    assert any(t.rpartition(".")[0] == "dbo.base_table" for t in outbound_targets)
    assert any(s.rpartition(".")[0] == "dbo.vw_x" for s in inbound_sources)


def test_confirmed_edges_visible_to_relationship_graph(db_path, monkeypatch):
    from core.connectors.schema import SchemaInfo, SchemaSnapshot, TableInfo, ColumnInfo, PrimaryKeyInfo
    from core.sqlservice.retrieval_documents import build_retrieval_documents
    from core.sqlservice.relationship_graph import build_relationship_graph, find_shortest_relationship_path

    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)
    discover_and_persist_view_dependencies(1, "alice")
    relationships = _all_relationships(db_path)

    snapshot = SchemaSnapshot(
        source_id=1, source_type="mssql", discovered_at=_NOW,
        schemas=[SchemaInfo(schema_name="dbo", tables=[
            TableInfo(table_name="vw_x", schema_name="dbo", table_fqn="dbo.vw_x", table_type="VIEW",
                      columns=[ColumnInfo("id", 1, "INTEGER", "int", True, False, False)],
                      primary_keys=[], foreign_keys=[]),
            TableInfo(table_name="base_table", schema_name="dbo", table_fqn="dbo.base_table", table_type="TABLE",
                      columns=[ColumnInfo("id", 1, "INTEGER", "int", False, True, True)],
                      primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[]),
        ])],
    )
    docs = build_retrieval_documents(1, snapshot, relationships=relationships)

    class _Doc:
        def __init__(self, d):
            self.object_fqn = d.object_fqn
            self.retrieval_tier = d.retrieval_tier
            self.metadata = d.metadata

    graph = build_relationship_graph([_Doc(d) for d in docs])
    path = find_shortest_relationship_path(graph, "dbo.vw_x", "dbo.base_table", max_path_length=1)
    assert path == ("dbo.vw_x", "dbo.base_table")


# ── 16. Safe failure logging ────────────────────────────────────────────


def test_connection_failure_never_leaks_raw_exception_text(db_path, monkeypatch, caplog):
    _insert_source(db_path)
    _insert_snapshot(db_path)
    _install_fake_get_connection_config(monkeypatch)
    monkeypatch.setattr(
        registry_module, "get",
        lambda source_type: (_ for _ in ()).throw(RuntimeError("secret password=hunter2 in connection string")) if source_type == "mssql" else None,
    )

    with caplog.at_level("DEBUG"):
        result = discover_and_persist_view_dependencies(1, "alice")

    assert result.status == STATUS_CONNECTION_FAILED
    assert result.error is not None
    assert "hunter2" not in result.error
    assert "password" not in result.error
    all_logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in all_logged


def test_query_failure_never_leaks_raw_exception_text(db_path, monkeypatch, caplog):
    cursor = _FakeCursor(raise_on_execute=RuntimeError("Login failed for user 'svc'. PWD=S3cr3t!"))
    _prepared(db_path, monkeypatch, cursor)

    with caplog.at_level("DEBUG"):
        result = discover_and_persist_view_dependencies(1, "alice")

    assert result.status == STATUS_QUERY_FAILED
    assert "S3cr3t" not in (result.error or "")
    all_logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "S3cr3t" not in all_logged
    assert "PWD=" not in all_logged


def test_no_traceback_logged_on_failure(db_path, monkeypatch, caplog):
    cursor = _FakeCursor(raise_on_execute=RuntimeError("boom"))
    _prepared(db_path, monkeypatch, cursor)

    with caplog.at_level("DEBUG"):
        discover_and_persist_view_dependencies(1, "alice")

    own_records = [r for r in caplog.records if r.name == "data.view_dependency_service"]
    assert len(own_records) >= 1
    for record in own_records:
        assert record.exc_info is None


# ── Ownership / snapshot / dialect gates ───────────────────────────────


def test_unowned_source_returns_unowned(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, user_id="alice")
    result = discover_and_persist_view_dependencies(1, "mallory")
    assert result.status == STATUS_UNOWNED


def test_no_snapshot_returns_no_snapshot(db_path, monkeypatch):
    _insert_source(db_path, source_id=1)
    result = discover_and_persist_view_dependencies(1, "alice")
    assert result.status == STATUS_NO_SNAPSHOT


def test_non_mssql_source_returns_not_mssql(db_path, monkeypatch):
    _insert_source(db_path, source_id=1, source_type="postgresql")
    _insert_snapshot(db_path, source_id=1)
    result = discover_and_persist_view_dependencies(1, "alice")
    assert result.status == STATUS_NOT_MSSQL


# ── 17. Connection-time ordering before retrieval preparation ─────────────


def test_view_dependency_discovery_runs_before_retrieval_preparation(db_path, monkeypatch):
    """Behavioral proof of ordering (never string-position fragility):
    both steps record their call order into a shared list."""
    from data.datasource_service import run_connect_bootstrap_job

    call_order = []

    def _fake_discovery(source_id, user_id, **kwargs):
        call_order.append("view_dependency_discovery")
        from data.view_dependency_service import ViewDependencyDiscoveryResult
        return ViewDependencyDiscoveryResult(
            status=STATUS_OK, source_id=source_id, snapshot_id=1,
            view_table_edges=0, view_view_edges=0, views_with_dependencies=0,
            edges_persisted=0, error=None,
        )

    def _fake_prepare(source_id, user_id, **kwargs):
        call_order.append("retrieval_preparation")
        return "Retrieval preparation is disabled."

    _insert_source(db_path)
    conn = _make_connection(db_path)
    cursor = conn.execute(
        "INSERT INTO metadata_jobs (source_id, user_id, job_type, status, created_at, updated_at) "
        "VALUES (1, 'alice', 'initial_metadata', 'QUEUED', ?, ?)", (_NOW, _NOW),
    )
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()

    with patch("data.schema_service.run_discovery"), \
         patch("data.profiling_service.run_structural_profiling"), \
         patch("data.pii_classification_service.run_pii_classification"), \
         patch("data.view_dependency_service.discover_and_persist_view_dependencies", _fake_discovery), \
         patch.object(datasource_service_module, "_prepare_retrieval_for_source", _fake_prepare):
        run_connect_bootstrap_job(job_id)

    assert call_order == ["view_dependency_discovery", "retrieval_preparation"]


def test_view_dependency_discovery_failure_does_not_fail_the_bootstrap_job(db_path, monkeypatch):
    from data.datasource_service import run_connect_bootstrap_job

    _insert_source(db_path)
    conn = _make_connection(db_path)
    cursor = conn.execute(
        "INSERT INTO metadata_jobs (source_id, user_id, job_type, status, created_at, updated_at) "
        "VALUES (1, 'alice', 'initial_metadata', 'QUEUED', ?, ?)", (_NOW, _NOW),
    )
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()

    def _raise(*a, **k):
        raise RuntimeError("unexpected failure")

    with patch("data.schema_service.run_discovery"), \
         patch("data.profiling_service.run_structural_profiling"), \
         patch("data.pii_classification_service.run_pii_classification"), \
         patch("data.view_dependency_service.discover_and_persist_view_dependencies", _raise), \
         patch.object(datasource_service_module, "_prepare_retrieval_for_source", lambda *a, **k: "Retrieval preparation is disabled."):
        run_connect_bootstrap_job(job_id)

    conn = _make_connection(db_path)
    row = dict(conn.execute("SELECT * FROM metadata_jobs WHERE id = ?", (job_id,)).fetchone())
    conn.close()
    assert row["status"] == "COMPLETE"


# ── SQL rebuild Phase 5, Step 6B: explicit AUTO provenance + classification ──


def test_catalog_dependency_rows_persist_explicit_auto_status(db_path, monkeypatch):
    """This is an authoritative, deterministic, read-only catalog query —
    genuinely AUTO-appropriate — but must still pass relationship_status
    explicitly (never rely on persist_relationships()'s DEFAULT-mirroring
    fallback), per Step 6B's own 'every writer supplies its status
    explicitly' requirement."""
    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)

    discover_and_persist_view_dependencies(1, "alice")

    rows = _all_relationships(db_path)
    assert len(rows) == 1
    assert rows[0]["relationship_status"] == "AUTO"
    assert rows[0]["inference_method"] == "catalog_view_dependency"


def test_catalog_dependency_rows_classify_as_confirmed(db_path, monkeypatch):
    from core.sqlservice.retrieval_documents import classify_relationship_row

    cursor = _FakeCursor([("dbo", "vw_x", "dbo", "base_table", "U")])
    _prepared(db_path, monkeypatch, cursor)

    discover_and_persist_view_dependencies(1, "alice")

    row = _all_relationships(db_path)[0]
    assert classify_relationship_row(row) == "confirmed"
