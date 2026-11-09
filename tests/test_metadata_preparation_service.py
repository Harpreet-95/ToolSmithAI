"""
Tests for data.metadata_preparation_service — the targeted, bounded
column-level metadata preparation step that backs the autonomous live-query
retry loop in core.orchestrator.context_builder._live_query (see
tests/test_live_query_pipeline.py::
test_missing_column_metadata_triggers_targeted_preparation_not_immediate_refusal
for the end-to-end contract this module exists to satisfy).

Run from the project root:
    python -m pytest tests/test_metadata_preparation_service.py -v
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-metadata-prep-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-metadata-prep-salt-long-enough-0987654321")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector

import data.datasource_service as datasource_service
import data.models as models
from data.metadata_preparation_service import prepare_selected_tables

_NOW = "2026-07-08T00:00:00+00:00"
_USER = "user-1"

_PATCHED_MODULES = (
    "data.metadata_preparation_service",
    "data.query_planning_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _mssql_record(**overrides):
    base = {
        "source_type": "mssql",
        "source_category": "relational_db",
        "display_name": "Prod SQL Server",
        "is_active": True,
        "source_status": "ACTIVE",
        "capabilities": ["connection_test", "schema_discovery", "sql_query"],
        "live_query_enabled": True,
        "params": {"host": "db.internal", "database": "EXAMPLECO"},
    }
    base.update(overrides)
    return base


class _FakeCursor:
    def __init__(self, description, rows, on_execute=None):
        self.description = description
        self._rows = rows
        self._on_execute = on_execute

    def execute(self, sql, params):
        if self._on_execute:
            self._on_execute(sql, params)

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _wire_fake_connector(monkeypatch, *, rows, description, on_execute=None):
    fake_conn = _FakeConnection(_FakeCursor(description, rows, on_execute=on_execute))
    monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
    return fake_conn


def env(tmp_path, monkeypatch, *, with_profiling_snapshot=True):
    """One mssql source owning two tables (dbo.orders, dbo.customers), both
    discoverable at the table level, neither with any column-level metadata."""
    db_path = str(tmp_path / "metaprep.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, live_query_enabled, created_at, updated_at) "
        "VALUES (1,?,'Prod SQL Server','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,1,?,?)",
        (_USER, _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    if with_profiling_snapshot:
        conn.execute(
            "INSERT INTO profiling_snapshots "
            "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
            "VALUES (1,1,1,1,?)", (_NOW,),
        )
    for tid, fqn, name, business in (
        (1, "dbo.orders", "orders", "Orders"),
        (2, "dbo.customers", "customers", "Customers"),
    ):
        if with_profiling_snapshot:
            conn.execute(
                "INSERT INTO profiling_table_profiles "
                "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
                " table_class, profiling_status, exact_row_count, created_at, updated_at) "
                "VALUES (?,1,1,?,?,'dbo','Transactional','COMPLETE',1000,?,?)",
                (tid, fqn, name, _NOW, _NOW),
            )
        conn.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            " business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,'dbo','TABLE',?,1,'rule_based',?,?)",
            (fqn, name, business, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return db_path


def _column_counts(db_path):
    conn = _db_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT table_fqn, COUNT(*) c FROM profiling_column_profiles "
            "WHERE source_id = 1 GROUP BY table_fqn"
        ).fetchall()
        return {r["table_fqn"]: r["c"] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Only the selected table is prepared — the unrelated table is untouched.
# ---------------------------------------------------------------------------

def test_prepares_only_selected_table_not_unrelated(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("order_count",)], rows=[(1000,)])

    result = prepare_selected_tables(1, _USER, ["dbo.orders"])

    assert result.prepared == ["dbo.orders"]
    assert result.skipped == []
    assert result.failed == {}

    counts = _column_counts(db_path)
    assert counts.get("dbo.orders", 0) == 1
    assert "dbo.customers" not in counts


# ---------------------------------------------------------------------------
# 2. Data type is inferred from a real sampled value, not fabricated.
# ---------------------------------------------------------------------------

def test_data_type_inferred_from_sample_values(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(
        monkeypatch,
        description=[("amount",), ("status",)],
        rows=[(19.99, "Approved")],
    )

    result = prepare_selected_tables(1, _USER, ["dbo.orders"])
    assert result.prepared == ["dbo.orders"]

    conn = _db_conn(db_path)
    try:
        rows = {
            r["column_name"]: r["data_type"]
            for r in conn.execute(
                "SELECT column_name, data_type FROM profiling_column_profiles WHERE table_fqn = 'dbo.orders'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows == {"amount": "DECIMAL", "status": "TEXT"}


# ---------------------------------------------------------------------------
# 3. Idempotent — a table that already has column metadata is skipped, not
#    re-probed (the connector must not even be opened for it).
# ---------------------------------------------------------------------------

def test_idempotent_skips_table_with_existing_column_metadata(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_nullable, is_primary_key, is_identity, ordinal_position, "
        " profiling_depth, profiling_status, created_at, updated_at) "
        "VALUES (1,1,'dbo.orders','amount','DECIMAL',0,0,0,1,'STRUCTURAL_ONLY','COMPLETE',?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    def _forbidden_open(self, config):
        raise AssertionError("open_connection must not be called for an already-prepared table")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = prepare_selected_tables(1, _USER, ["dbo.orders"])

    assert result.prepared == []
    assert result.skipped == ["dbo.orders"]
    assert result.failed == {}


# ---------------------------------------------------------------------------
# 4. A table_fqn that doesn't belong to the source is never invented/probed.
# ---------------------------------------------------------------------------

def test_table_not_belonging_to_source_is_never_prepared(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_open(self, config):
        raise AssertionError("open_connection must not be called for an unowned table_fqn")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = prepare_selected_tables(1, _USER, ["dbo.not_a_real_table"])

    assert result.prepared == []
    assert result.skipped == []
    assert "dbo.not_a_real_table" in result.failed


# ---------------------------------------------------------------------------
# 5. Ownership failure (wrong user_id) never triggers any preparation.
# ---------------------------------------------------------------------------

def test_ownership_failure_prepares_nothing(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: None)

    def _forbidden_open(self, config):
        raise AssertionError("open_connection must not be called when ownership fails")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = prepare_selected_tables(1, "someone-else", ["dbo.orders"])

    assert result.prepared == []
    assert "dbo.orders" in result.failed
    assert _column_counts(db_path) == {}


# ---------------------------------------------------------------------------
# 6. A live probe failure for one table is reported, never raised, and never
#    blocks other tables in the same call.
# ---------------------------------------------------------------------------

def test_probe_failure_is_reported_not_raised(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _broken_open(self, config):
        raise RuntimeError("simulated connection failure")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _broken_open)

    result = prepare_selected_tables(1, _USER, ["dbo.orders"])

    assert result.prepared == []
    assert "dbo.orders" in result.failed
    assert _column_counts(db_path) == {}


# ---------------------------------------------------------------------------
# 7. Multiple selected tables in one call: each is prepared independently,
#    still never touching a table outside the requested list.
# ---------------------------------------------------------------------------

def test_multiple_selected_tables_each_prepared_independently(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("id",)], rows=[(1,)])

    result = prepare_selected_tables(1, _USER, ["dbo.orders", "dbo.customers"])

    assert set(result.prepared) == {"dbo.orders", "dbo.customers"}
    counts = _column_counts(db_path)
    assert counts.get("dbo.orders", 0) == 1
    assert counts.get("dbo.customers", 0) == 1


# ---------------------------------------------------------------------------
# 8. Empty input list is a no-op — no connection is opened.
# ---------------------------------------------------------------------------

def test_empty_table_list_is_a_no_op(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_open(self, config):
        raise AssertionError("open_connection must not be called for an empty table list")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = prepare_selected_tables(1, _USER, [])

    assert result.prepared == []
    assert result.skipped == []
    assert result.failed == {}


# ---------------------------------------------------------------------------
# 9. No profiling snapshot exists at all on the source — nothing to attach
#    column metadata to; reported as a controlled failure, not a crash.
# ---------------------------------------------------------------------------

def test_no_profiling_snapshot_is_a_controlled_failure(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch, with_profiling_snapshot=False)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_open(self, config):
        raise AssertionError("open_connection must not be called when there is no profiling snapshot")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = prepare_selected_tables(1, _USER, ["dbo.orders"])

    assert result.prepared == []
    assert "dbo.orders" in result.failed
    assert _column_counts(db_path) == {}


# ---------------------------------------------------------------------------
# 10. Idempotency at the persistence layer: calling prepare twice for the
#     same never-before-seen table does not raise on the second call and
#     does not duplicate rows (the UNIQUE index + ON CONFLICT DO NOTHING,
#     backed by the pre-check skip).
# ---------------------------------------------------------------------------

def test_calling_prepare_twice_is_idempotent(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("order_count",)], rows=[(1000,)])

    first = prepare_selected_tables(1, _USER, ["dbo.orders"])
    second = prepare_selected_tables(1, _USER, ["dbo.orders"])

    assert first.prepared == ["dbo.orders"]
    assert second.prepared == []
    assert second.skipped == ["dbo.orders"]

    counts = _column_counts(db_path)
    assert counts.get("dbo.orders", 0) == 1
