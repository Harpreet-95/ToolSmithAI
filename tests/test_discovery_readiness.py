"""
Tests for the "false Ready to Ask" fix in data.schema_service.run_discovery.

Before this change, any SchemaSnapshot with an empty schemas list — whether
because discovery genuinely failed (connection refused, TLS certificate
untrusted, authentication rejected, ...) or because the connected database
is legitimately empty — was persisted and the source status was
unconditionally set to 'ACTIVE'. data.datasource_service.is_ready_to_ask()
only checks for the *existence* of a schema_snapshots row, so a source
whose real connection was failing (e.g. a TLS handshake rejected by
pyodbc/SQL Server) still reported "Ready to ask" with zero known tables —
discovered live against a real reconnected the reference database source, see the
"CRITICAL FRESH-CONNECTION TEST FAILURE" investigation.

The fix: core.connectors.schema.SchemaSnapshot now carries an explicit
discovery_succeeded flag (defaults True), which every connector's failure
path sets False. run_discovery() only marks the source 'ACTIVE' /
Ready-to-ask when discovery_succeeded is True; otherwise it marks the
source 'ERROR' and records the connector's safe warning message in the
existing last_test_message column (already exposed to the frontend).

Run from the project root:
    venv/Scripts/pytest tests/test_discovery_readiness.py -v
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-discovery-readiness-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-discovery-readiness-salt-long-enough-654321")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.secrets.manager import get_secret_manager

import data.models as models
from data.datasource_service import is_ready_to_ask
from data.schema_service import run_discovery

_USER = "user-1"

_PATCHED_MODULES = ("data.schema_service", "data.relationship_service", "data.datasource_service")


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "discovery_readiness.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))
    return db_path


def _insert_source(db_path: str) -> int:
    conn = _db_conn(db_path)
    try:
        secret = get_secret_manager().encrypt_secret('{"host": "db.internal", "database": "EXAMPLECO"}')
        cur = conn.execute(
            """
            INSERT INTO data_source_connections
                (user_id, display_name, source_type, source_category,
                 encrypted_config_json, capabilities_json, created_at, updated_at)
            VALUES (?, 'Test Source', 'mssql', 'relational_db', ?,
                    '["schema_discovery"]',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (_USER, secret),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row(db_path: str, source_id: int) -> sqlite3.Row:
    conn = _db_conn(db_path)
    try:
        return conn.execute(
            "SELECT source_status, last_test_status, last_test_message "
            "FROM data_source_connections WHERE id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()


def _fake_success_snapshot(source_id: int) -> SchemaSnapshot:
    table = TableInfo(
        table_name="EDU_Student", schema_name="dbo", table_fqn="dbo.EDU_Student",
        table_type="TABLE",
        columns=[ColumnInfo(column_name="StudentID", ordinal_position=1, data_type="INTEGER",
                             raw_type="int", is_nullable=False, is_primary_key=True, is_identity=True)],
    )
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql",
        discovered_at="2026-08-24T00:00:00+00:00",
        schemas=[SchemaInfo(schema_name="dbo", tables=[table])],
        database_name="EXAMPLECO", server_name="db.internal",
    )


def _fake_empty_but_successful_snapshot(source_id: int) -> SchemaSnapshot:
    """A genuinely empty database: discovery ran fine, there's just nothing in it."""
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql",
        discovered_at="2026-08-24T00:00:00+00:00",
        schemas=[], database_name="EXAMPLECO", server_name="db.internal",
    )


def _fake_tls_failure_snapshot(source_id: int) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql",
        discovered_at="2026-08-24T00:00:00+00:00",
        schemas=[],
        warnings=[
            "Schema discovery failed. Check connection parameters and server availability.",
            "('08001', '[08001] [Microsoft][ODBC Driver 17 for SQL Server]SSL Provider: "
            "The certificate chain was issued by an authority that is not trusted.')",
        ],
        discovery_succeeded=False,
    )


def _fake_auth_failure_snapshot(source_id: int) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql",
        discovered_at="2026-08-24T00:00:00+00:00",
        schemas=[],
        warnings=[
            "Schema discovery failed. Check connection parameters and server availability.",
            "('28000', \"[28000] [Microsoft][ODBC Driver 17 for SQL Server]Login failed for user 'svc'.\")",
        ],
        discovery_succeeded=False,
    )


def test_successful_discovery_marks_source_ready(env, monkeypatch):
    source_id = _insert_source(env)
    monkeypatch.setattr(SQLServerConnector, "discover_schema",
                         lambda self, config: _fake_success_snapshot(source_id))

    result = run_discovery(source_id, _USER)

    assert result["discovery_succeeded"] is True
    row = _row(env, source_id)
    assert row["source_status"] == "ACTIVE"
    assert row["last_test_status"] == "success"
    assert is_ready_to_ask(source_id, _USER) is True


def test_genuinely_empty_database_is_still_ready(env, monkeypatch):
    """The critical distinction the fix must preserve: an empty database is
    not a failure and must not be misclassified as one."""
    source_id = _insert_source(env)
    monkeypatch.setattr(SQLServerConnector, "discover_schema",
                         lambda self, config: _fake_empty_but_successful_snapshot(source_id))

    result = run_discovery(source_id, _USER)

    assert result["discovery_succeeded"] is True
    assert result["table_count"] == 0
    row = _row(env, source_id)
    assert row["source_status"] == "ACTIVE"
    assert is_ready_to_ask(source_id, _USER) is True


def test_tls_certificate_failure_blocks_ready_state_with_safe_message(env, monkeypatch):
    source_id = _insert_source(env)
    monkeypatch.setattr(SQLServerConnector, "discover_schema",
                         lambda self, config: _fake_tls_failure_snapshot(source_id))

    result = run_discovery(source_id, _USER)

    assert result["discovery_succeeded"] is False
    row = _row(env, source_id)
    assert row["source_status"] == "ERROR"
    assert row["last_test_status"] == "failed"
    assert "certificate chain" in row["last_test_message"]
    assert is_ready_to_ask(source_id, _USER) is False


def test_authentication_failure_blocks_ready_state(env, monkeypatch):
    source_id = _insert_source(env)
    monkeypatch.setattr(SQLServerConnector, "discover_schema",
                         lambda self, config: _fake_auth_failure_snapshot(source_id))

    result = run_discovery(source_id, _USER)

    assert result["discovery_succeeded"] is False
    row = _row(env, source_id)
    assert row["source_status"] == "ERROR"
    assert is_ready_to_ask(source_id, _USER) is False


def test_generic_connectivity_failure_blocks_ready_state(env, monkeypatch):
    source_id = _insert_source(env)
    failure = SchemaSnapshot(
        source_id=source_id, source_type="mssql",
        discovered_at="2026-08-24T00:00:00+00:00",
        schemas=[],
        warnings=["Schema discovery failed. Check connection parameters and server availability."],
        discovery_succeeded=False,
    )
    monkeypatch.setattr(SQLServerConnector, "discover_schema", lambda self, config: failure)

    run_discovery(source_id, _USER)

    assert is_ready_to_ask(source_id, _USER) is False
