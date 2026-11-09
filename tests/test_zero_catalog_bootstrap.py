"""
Tests for the zero-catalog bootstrap added to data.datasource_service.
create_data_source() during Enterprise Phase 1 closure.

Before this change, a freshly connected source had zero schema_snapshots
rows until a user separately ran the queued metadata_jobs row or clicked
"Discover Schema" — and data.query_planning_service.plan_business_query's
candidate-table retrieval can only find a candidate table once at least one
schema_snapshots row exists, so the very first question against a new
connection always refused with zero candidates regardless of Phase 1's
autonomous column-metadata preparation (which only prepares columns for
tables already found as candidates — it never discovers tables).

Run from the project root:
    python -m pytest tests/test_zero_catalog_bootstrap.py -v
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-zero-catalog-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-zero-catalog-salt-long-enough-0987654321")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo

import data.models as models
from data.datasource_service import create_data_source

_USER = "user-1"

_PATCHED_MODULES = (
    "data.datasource_service",
    "data.schema_service",
    "data.relationship_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "zerocatalog.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))
    return db_path


def _fake_snapshot(source_id: int) -> SchemaSnapshot:
    """One table, no row sampling — mirrors exactly what a real
    INFORMATION_SCHEMA-based discover_schema() call returns: shape only."""
    table = TableInfo(
        table_name="orders",
        schema_name="dbo",
        table_fqn="dbo.orders",
        table_type="TABLE",
        columns=[
            ColumnInfo(
                column_name="OrderID", ordinal_position=1, data_type="INTEGER",
                raw_type="int", is_nullable=False, is_primary_key=True, is_identity=True,
            ),
        ],
    )
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql",
        discovered_at="2026-07-23T00:00:00+00:00",
        schemas=[SchemaInfo(schema_name="dbo", tables=[table])],
    )


def _create_source_payload():
    return {
        "display_name": "Prod SQL Server",
        "source_type": "mssql",
        "config": {"host": "db.internal", "database": "EXAMPLECO", "auth_type": "sql",
                    "username": "svc", "password": "secret"},
        "metadata": {},
    }


def _schema_snapshot_count(db_path, source_id):
    conn = _db_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM schema_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


def _profiling_snapshot_count(db_path, source_id):
    conn = _db_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM profiling_snapshots WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Connecting a source automatically bootstraps a schema_snapshots row.
# ---------------------------------------------------------------------------

def test_create_source_bootstraps_schema_snapshot(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )

    record = create_data_source(_USER, _create_source_payload())

    assert _schema_snapshot_count(db_path, record["id"]) == 1


# ---------------------------------------------------------------------------
# 2. No full/structural profiling job runs automatically.
# ---------------------------------------------------------------------------

def test_create_source_triggers_no_automatic_profiling(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )

    record = create_data_source(_USER, _create_source_payload())

    assert _profiling_snapshot_count(db_path, record["id"]) == 0


# ---------------------------------------------------------------------------
# 3. The bootstrap is best-effort: a discovery failure never blocks source
#    creation (bad credentials, unreachable host, transient network error).
# ---------------------------------------------------------------------------

def test_create_source_succeeds_even_if_discovery_bootstrap_fails(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def _boom(self, config):
        raise ConnectionError("could not reach host")

    monkeypatch.setattr(SQLServerConnector, "discover_schema", _boom)

    record = create_data_source(_USER, _create_source_payload())

    assert record["id"] is not None
    assert record["display_name"] == "Prod SQL Server"


# ---------------------------------------------------------------------------
# 4. The question path continues after bootstrap: a candidate table is now
#    findable without any separate manual scan action.
# ---------------------------------------------------------------------------

def test_candidate_table_findable_immediately_after_connect(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )
    monkeypatch.setattr("data.search_service.get_connection", lambda p=db_path: _db_conn(p))

    record = create_data_source(_USER, _create_source_payload())

    from data.search_service import search_metadata

    result = search_metadata("orders", source_id=record["id"])
    fqns = {r["qualified_name"] for r in result["results"] if r["asset_type"] == "table"}
    assert "dbo.orders" in fqns


# ---------------------------------------------------------------------------
# 5. Idempotent: running discovery again for the same source does not fail
#    or duplicate in a way that breaks candidate retrieval.
# ---------------------------------------------------------------------------

def test_rerunning_discovery_for_same_source_is_idempotent(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )

    record = create_data_source(_USER, _create_source_payload())
    assert _schema_snapshot_count(db_path, record["id"]) == 1

    from data.schema_service import run_discovery
    run_discovery(record["id"], _USER)  # simulates a manual "Discover Schema" re-run

    assert _schema_snapshot_count(db_path, record["id"]) == 2  # versioned, not duplicated/corrupted
