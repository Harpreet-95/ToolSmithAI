"""
Tests for Sprint 1.1 — search_metadata() must not require a
profiling_table_profiles row to surface a table. Newly discovered tables
(schema_snapshots row exists, profiling/dictionary generation has not run
yet) must be searchable immediately, while already-profiled tables keep
their existing ranking fields and filter behavior unchanged.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern established by
test_semantic_retrieval_service.py / test_phase9_query_planning.py.

Run from the project root:
    venv/Scripts/pytest tests/test_search_unprofiled_tables.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-search-unprofiled-secret-long-enough-12")
os.environ.setdefault("USER_ID_SALT", "test-search-unprofiled-salt-long-enough-1234")

import data.models as models
from data.search_service import search_metadata, _discovered_tables
from data.semantic_retrieval_service import get_candidate_tables

_NOW = "2026-07-14T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _snapshot_json(*table_names_and_schemas):
    tables = [
        {
            "table_name": name, "schema_name": schema,
            "table_fqn": f"{schema}.{name}", "table_type": "TABLE",
            "row_count_estimate": None, "columns": [], "primary_keys": [], "foreign_keys": [],
        }
        for schema, name in table_names_and_schemas
    ]
    return json.dumps({
        "source_id": 1, "source_type": "mssql", "discovered_at": _NOW,
        "schemas": [{"schema_name": "dbo", "tables": tables}],
        "database_name": None, "server_name": None,
        "connector_version": None, "discovery_duration_ms": None, "warnings": [],
    })


def env(tmp_path, monkeypatch, *, discovered_tables):
    """Seed a source with a schema_snapshots row listing every discovered
    table, but only add profiling_table_profiles / data_dictionary_tables /
    domain_assignments rows for tables explicitly profiled via _add_profiling."""
    db_path = str(tmp_path / "search_unprofiled.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',?,?,?,?)",
        (len(discovered_tables), _snapshot_json(*discovered_tables), _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _c(db_path):
    return _db_conn(db_path)


def _add_profiling(db, table_fqn, *, domain=None, business_name=None):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash(table_fqn)) % 10000
    c.execute(
        "INSERT INTO profiling_snapshots (id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?) ON CONFLICT DO NOTHING", (_NOW,),
    )
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,'Transactional','COMPLETE',1000,?,?)",
        (tid, table_fqn, name, schema, _NOW, _NOW),
    )
    if business_name:
        c.execute(
            "INSERT OR REPLACE INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            " business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,?,'TABLE',?,1,'rule_based',?,?)",
            (table_fqn, name, schema, business_name, _NOW, _NOW),
        )
    if domain:
        c.execute(
            "INSERT OR REPLACE INTO domain_assignments "
            "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, "
            " evidence_json, competing_domains_json, created_at, updated_at) "
            "VALUES (1,1,?,?,0.9,'[]','[]',?,?)",
            (table_fqn, domain, _NOW, _NOW),
        )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# search_metadata — unprofiled tables become searchable
# ---------------------------------------------------------------------------

def test_unprofiled_discovered_table_is_returned_by_search_metadata(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch, discovered_tables=[("dbo", "Students")])
    # No profiling_table_profiles / data_dictionary_tables row is ever added.
    result = search_metadata(q="students", source_id=1, asset_type="table")
    fqns = {r["qualified_name"] for r in result["results"]}
    assert "dbo.Students" in fqns


def test_profiled_table_still_returns_same_ranking_fields(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch, discovered_tables=[("dbo", "Students")])
    _add_profiling(db, "dbo.Students", domain="Student Lifecycle", business_name="Students Master")
    result = search_metadata(q="students", source_id=1, asset_type="table")
    assert result["results"], "expected the profiled table to be found"
    row = result["results"][0]
    for field in (
        "asset_type", "display_name", "qualified_name", "source_id", "source_name",
        "schema_name", "table_name", "relevance_score", "match_reasons", "domain",
        "entity", "dictionary_status", "pii_indicator", "semantic_type", "table_type",
        "confidence", "profiled_at", "profiling_status", "nav_target",
    ):
        assert field in row, f"missing expected ranking field: {field}"
    assert row["domain"] == "Student Lifecycle"
    assert row["dictionary_status"] == "approved"


def test_domain_filtering_still_works_alongside_unprofiled_tables(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch, discovered_tables=[("dbo", "Students"), ("dbo", "StudentNotes")])
    _add_profiling(db, "dbo.Students", domain="Student Lifecycle", business_name="Students Master")
    # dbo.StudentNotes stays fully unprofiled/undictionaried/undomained.

    unfiltered = search_metadata(q="student", source_id=1, asset_type="table")
    unfiltered_fqns = {r["qualified_name"] for r in unfiltered["results"]}
    assert "dbo.Students" in unfiltered_fqns
    assert "dbo.StudentNotes" in unfiltered_fqns

    filtered = search_metadata(q="student", source_id=1, asset_type="table", domain="Student Lifecycle")
    filtered_fqns = {r["qualified_name"] for r in filtered["results"]}
    assert filtered_fqns == {"dbo.Students"}


def test_unprofiled_table_invisible_without_discovery_fix_is_now_visible(tmp_path, monkeypatch):
    # Directly exercises the new helper: a source with a snapshot but zero
    # profiling/dictionary rows must still enumerate its tables.
    db = env(tmp_path, monkeypatch, discovered_tables=[("dbo", "Courses"), ("dbo", "Enrollments")])
    conn = _c(db)
    discovered = _discovered_tables(conn, 1)
    conn.close()
    assert {t["table_fqn"] for t in discovered} == {"dbo.Courses", "dbo.Enrollments"}


# ---------------------------------------------------------------------------
# Semantic retrieval selects an unprofiled table
# ---------------------------------------------------------------------------

def test_semantic_retrieval_selects_unprofiled_table(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch, discovered_tables=[("dbo", "Students")])
    # No profiling/dictionary/domain rows at all — purely discovered.
    result = get_candidate_tables(
        1, "u1", "list all student enrollment records by course", ["student"],
    )
    assert "dbo.Students" in result
