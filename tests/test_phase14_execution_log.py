"""
Phase 14 tests — Query Execution Audit Logging (Phase 6.2).

All service tests use a patched in-memory SQLite DB so no real DB is touched.
Route tests use FastAPI TestClient with all downstream services mocked.

Coverage:
  - log_query_execution writes one row; raw SQL never stored, only SHA-256 hash
  - get_query_execution_log retrieves by execution_id + user_id ownership
  - get_query_execution_log returns None for wrong user_id
  - list_query_executions returns newest-first rows
  - list_query_executions filters by source_id
  - list_query_executions filters by status
  - list_query_executions honours limit and offset
  - _extract_tables extracts FQNs from select, from, and joins
  - execute_generated_query calls log_query_execution on safety-gate governance_block
  - execute_generated_query calls log_query_execution on PII governance_block
  - execute_generated_query calls log_query_execution on connection failure
  - execute_generated_query calls log_query_execution on timeout
  - execute_generated_query calls log_query_execution on success
  - truncated stored as INTEGER 0/1 but returned as bool
  - _write_audit now includes execution_id in payload
  - GET /v1/query-executions/{id} returns 200 with record
  - GET /v1/query-executions/{id} returns 404 for wrong user
  - GET /v1/query-executions returns paginated list
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from unittest.mock import MagicMock, call, patch

from cryptography.fernet import Fernet

# Env vars must be set before any core.* import
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase14-exec-log-secret-long-enough-abc")
os.environ.setdefault("USER_ID_SALT", "test-phase14-salt-long-enough-value-1234567")

import pytest

import data.query_execution_service as svc
from data.query_execution_service import (
    _extract_tables,
    execute_generated_query,
    get_query_execution_log,
    list_query_executions,
    log_query_execution,
)


# ---------------------------------------------------------------------------
# In-memory SQLite helpers
# ---------------------------------------------------------------------------

_QEL_DDL = """
    CREATE TABLE IF NOT EXISTS query_execution_log (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        execution_id         TEXT    NOT NULL,
        user_id              TEXT    NOT NULL,
        source_id            INTEGER NOT NULL,
        sql_hash             TEXT,
        tables_accessed_json TEXT,
        param_count          INTEGER NOT NULL DEFAULT 0,
        row_count            INTEGER NOT NULL DEFAULT 0,
        truncated            INTEGER NOT NULL DEFAULT 0,
        duration_ms          INTEGER NOT NULL DEFAULT 0,
        status               TEXT    NOT NULL,
        error_code           TEXT,
        executed_at          TEXT    NOT NULL,
        created_at           TEXT    NOT NULL
    )
"""


class _KeepAliveConn:
    """Wrap a sqlite3.Connection so that close() is a no-op.

    Python 3.14 made sqlite3.Connection.close read-only, so we cannot
    monkey-patch it directly.  The service calls conn.close() after every
    write/read — this wrapper keeps the in-memory DB alive across those calls.
    """

    def __init__(self, inner):
        self._inner = inner

    # Forward every attribute/method except close
    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):  # no-op — keep in-memory DB alive
        pass

    # sqlite3.Row lookup requires row_factory to be set on the real conn
    @property
    def row_factory(self):
        return self._inner.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._inner.row_factory = value


def _make_log_db() -> _KeepAliveConn:
    """In-memory SQLite with query_execution_log schema, close() suppressed."""
    inner = sqlite3.connect(":memory:", check_same_thread=False)
    inner.row_factory = sqlite3.Row
    inner.execute(_QEL_DDL)
    inner.commit()
    return _KeepAliveConn(inner)


@pytest.fixture
def log_db(monkeypatch):
    """Patch svc.get_connection to return a fresh in-memory DB for each test."""
    conn = _make_log_db()
    monkeypatch.setattr(svc, "get_connection", lambda: conn)
    return conn


# ---------------------------------------------------------------------------
# Shared test data builders (mirrors Phase 13 helpers)
# ---------------------------------------------------------------------------

def _gen_result(
    sql="SELECT [amount] FROM [dbo].[orders]",
    validated=True,
    select_only=True,
    params=None,
    dialect="mssql",
):
    p = params or []
    return {
        "sql":        sql,
        "parameters": {"values": p, "placeholder": "?", "count": len(p)},
        "dialect":    dialect,
        "safety": {
            "read_only":     True,
            "parameterized": True,
            "validated":     validated,
            "select_only":   select_only,
        },
        "warnings":    [],
        "explanation": [],
    }


def _sql_plan(select=None, limits=None, valid=True, joins=None):
    select = select or [
        {"table_fqn": "dbo.orders", "column_name": "amount",
         "alias": "amount", "aggregation": "SUM"},
    ]
    return {
        "select":   select,
        "from":     {"table_fqn": "dbo.orders", "alias": "ord"},
        "joins":    joins or [],
        "where":    [],
        "group_by": [],
        "limits":   limits if limits is not None else {"row_limit": 1000},
        "warnings": [],
        "validation": {
            "valid":            valid,
            "read_only":        True,
            "checks":           {},
            "blocking_reasons": [] if valid else ["Plan invalid."],
        },
    }


def _make_cursor(rows, col_names):
    cursor = MagicMock()
    cursor.description = [(n, None, None, None, None, None, None) for n in col_names]
    cursor.fetchmany.return_value = rows
    return cursor


def _make_db_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _no_op_audit(*_a, **_kw):
    pass


def _no_governance(*_a, **_kw):
    return [], set(), []


def _patch_exec_deps(monkeypatch, db_conn=None, governance_fn=None):
    """Patch execution-layer deps for execute_generated_query tests."""
    _conn = db_conn or _make_db_conn(_make_cursor([], []))
    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (_conn, "mssql"))
    monkeypatch.setattr(svc, "_governance_recheck", governance_fn or _no_governance)
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)


# ===========================================================================
# ── 1: _extract_tables ──────────────────────────────────────────────────────
# ===========================================================================

def test_extract_tables_from_select_and_from():
    plan = {
        "select": [{"table_fqn": "dbo.orders", "column_name": "id"}],
        "from":   {"table_fqn": "dbo.orders"},
        "joins":  [],
    }
    assert _extract_tables(plan) == ["dbo.orders"]


def test_extract_tables_deduplicates_and_sorts():
    plan = {
        "select": [
            {"table_fqn": "dbo.orders", "column_name": "id"},
            {"table_fqn": "dbo.customers", "column_name": "name"},
        ],
        "from":  {"table_fqn": "dbo.orders"},
        "joins": [
            {"left_table": "dbo.orders", "right_table": "dbo.customers",
             "left_column": "cust_id", "right_column": "id"},
        ],
    }
    result = _extract_tables(plan)
    assert result == sorted(set(result))
    assert "dbo.orders" in result
    assert "dbo.customers" in result


def test_extract_tables_empty_plan():
    assert _extract_tables({}) == []


def test_extract_tables_join_tables_included():
    plan = {
        "select": [],
        "from":   {},
        "joins":  [
            {"left_table": "schema.A", "right_table": "schema.B",
             "left_column": "id", "right_column": "fk"},
        ],
    }
    result = _extract_tables(plan)
    assert "schema.A" in result
    assert "schema.B" in result


# ===========================================================================
# ── 2: log_query_execution writes correct row ───────────────────────────────
# ===========================================================================

def test_log_query_execution_writes_row(log_db):
    eid = str(uuid.uuid4())
    sql = "SELECT [amount] FROM [dbo].[orders]"
    expected_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()

    log_query_execution(
        eid, "user1", 42, sql, _sql_plan(),
        param_count=2, row_count=100, truncated=False,
        duration_ms=350, status="success",
        error_code=None, executed_at="2026-07-01T12:00:00+00:00",
    )

    row = log_db.execute(
        "SELECT * FROM query_execution_log WHERE execution_id = ?", (eid,)
    ).fetchone()

    assert row is not None
    assert row["execution_id"] == eid
    assert row["user_id"]      == "user1"
    assert row["source_id"]    == 42
    assert row["sql_hash"]     == expected_hash
    assert row["param_count"]  == 2
    assert row["row_count"]    == 100
    assert row["truncated"]    == 0          # stored as INTEGER
    assert row["duration_ms"]  == 350
    assert row["status"]       == "success"
    assert row["error_code"]   is None
    assert row["executed_at"]  == "2026-07-01T12:00:00+00:00"
    assert row["created_at"]   is not None


def test_log_query_execution_never_stores_raw_sql(log_db):
    eid = str(uuid.uuid4())
    secret_sql = "SELECT TOP (1000) [ssn] FROM [hr].[employees]"

    log_query_execution(
        eid, "user1", 1, secret_sql, _sql_plan(),
        param_count=0, row_count=50, truncated=False,
        duration_ms=100, status="success",
        error_code=None, executed_at="2026-07-01T00:00:00+00:00",
    )

    row = log_db.execute(
        "SELECT * FROM query_execution_log WHERE execution_id = ?", (eid,)
    ).fetchone()

    # Raw SQL must NOT appear anywhere in the stored row
    for col in row.keys():
        val = str(row[col]) if row[col] is not None else ""
        assert secret_sql not in val, f"Raw SQL found in column '{col}'"

    # But the hash must be the SHA-256 of the SQL
    assert row["sql_hash"] == hashlib.sha256(secret_sql.encode()).hexdigest()


def test_log_query_execution_stores_tables_json(log_db):
    eid = str(uuid.uuid4())
    plan = _sql_plan(select=[
        {"table_fqn": "sales.orders", "column_name": "total", "alias": "total"},
    ])
    plan["from"] = {"table_fqn": "sales.orders"}

    log_query_execution(
        eid, "user1", 1, "SELECT ...", plan,
        param_count=0, row_count=5, truncated=False,
        duration_ms=20, status="success",
        error_code=None, executed_at="2026-07-01T00:00:00+00:00",
    )

    row = log_db.execute(
        "SELECT tables_accessed_json FROM query_execution_log WHERE execution_id = ?",
        (eid,),
    ).fetchone()

    tables = json.loads(row["tables_accessed_json"])
    assert "sales.orders" in tables


def test_log_query_execution_truncated_stored_as_integer(log_db):
    eid = str(uuid.uuid4())
    log_query_execution(
        eid, "user1", 1, "SELECT 1", _sql_plan(),
        param_count=0, row_count=1000, truncated=True,
        duration_ms=200, status="success",
        error_code=None, executed_at="2026-07-01T00:00:00+00:00",
    )

    row = log_db.execute(
        "SELECT truncated FROM query_execution_log WHERE execution_id = ?", (eid,)
    ).fetchone()
    assert row["truncated"] == 1   # stored as INTEGER 1, not Python True


def test_log_query_execution_null_sql_stores_null_hash(log_db):
    eid = str(uuid.uuid4())
    log_query_execution(
        eid, "user1", 1, None, {},
        param_count=0, row_count=0, truncated=False,
        duration_ms=1, status="governance_block",
        error_code="safety_gate", executed_at="2026-07-01T00:00:00+00:00",
    )

    row = log_db.execute(
        "SELECT sql_hash FROM query_execution_log WHERE execution_id = ?", (eid,)
    ).fetchone()
    assert row["sql_hash"] is None


# ===========================================================================
# ── 3: get_query_execution_log ──────────────────────────────────────────────
# ===========================================================================

def test_get_query_execution_log_returns_correct_record(log_db):
    eid = str(uuid.uuid4())
    log_query_execution(
        eid, "user1", 5, "SELECT 1", _sql_plan(),
        param_count=1, row_count=10, truncated=False,
        duration_ms=55, status="success",
        error_code=None, executed_at="2026-07-01T08:00:00+00:00",
    )

    record = get_query_execution_log(eid, "user1")

    assert record is not None
    assert record["execution_id"] == eid
    assert record["user_id"]      == "user1"
    assert record["source_id"]    == 5
    assert record["param_count"]  == 1
    assert record["row_count"]    == 10
    assert record["truncated"]    is False   # returned as Python bool
    assert record["status"]       == "success"
    assert isinstance(record["tables_accessed"], list)


def test_get_query_execution_log_returns_none_for_wrong_user(log_db):
    eid = str(uuid.uuid4())
    log_query_execution(
        eid, "user1", 1, "SELECT 1", _sql_plan(),
        param_count=0, row_count=0, truncated=False,
        duration_ms=1, status="success",
        error_code=None, executed_at="2026-07-01T00:00:00+00:00",
    )

    # Different user_id → None (ownership enforced)
    result = get_query_execution_log(eid, "intruder")
    assert result is None


def test_get_query_execution_log_returns_none_for_unknown_id(log_db):
    result = get_query_execution_log("nonexistent-id", "user1")
    assert result is None


def test_get_query_execution_log_truncated_returned_as_bool(log_db):
    eid = str(uuid.uuid4())
    log_query_execution(
        eid, "user1", 1, "SELECT 1", _sql_plan(),
        param_count=0, row_count=1000, truncated=True,
        duration_ms=10, status="success",
        error_code=None, executed_at="2026-07-01T00:00:00+00:00",
    )

    record = get_query_execution_log(eid, "user1")
    assert record["truncated"] is True
    assert isinstance(record["truncated"], bool)


# ===========================================================================
# ── 4: list_query_executions ────────────────────────────────────────────────
# ===========================================================================

def _write_n_logs(n, user_id, source_id, status, log_db):
    """Helper: write n execution logs with incrementing executed_at timestamps."""
    ids = []
    for i in range(n):
        eid = str(uuid.uuid4())
        ids.append(eid)
        log_query_execution(
            eid, user_id, source_id, "SELECT 1", _sql_plan(),
            param_count=0, row_count=i, truncated=False,
            duration_ms=10, status=status,
            error_code=None, executed_at=f"2026-07-01T{i:02d}:00:00+00:00",
        )
    return ids


def test_list_query_executions_returns_rows_for_user(log_db):
    _write_n_logs(3, "alice", 1, "success", log_db)
    rows = list_query_executions("alice")
    assert len(rows) == 3


def test_list_query_executions_isolates_by_user(log_db):
    _write_n_logs(3, "alice", 1, "success", log_db)
    _write_n_logs(2, "bob",   2, "success", log_db)
    assert len(list_query_executions("alice")) == 3
    assert len(list_query_executions("bob"))   == 2
    assert len(list_query_executions("charlie")) == 0


def test_list_query_executions_filters_by_source_id(log_db):
    _write_n_logs(3, "alice", 1, "success", log_db)
    _write_n_logs(2, "alice", 2, "success", log_db)
    assert len(list_query_executions("alice", source_id=1)) == 3
    assert len(list_query_executions("alice", source_id=2)) == 2
    assert len(list_query_executions("alice", source_id=99)) == 0


def test_list_query_executions_filters_by_status(log_db):
    _write_n_logs(3, "alice", 1, "success",          log_db)
    _write_n_logs(2, "alice", 1, "governance_block",  log_db)
    assert len(list_query_executions("alice", status="success"))         == 3
    assert len(list_query_executions("alice", status="governance_block"))  == 2
    assert len(list_query_executions("alice", status="timeout"))          == 0


def test_list_query_executions_limit_and_offset(log_db):
    _write_n_logs(10, "alice", 1, "success", log_db)
    page1 = list_query_executions("alice", limit=4, offset=0)
    page2 = list_query_executions("alice", limit=4, offset=4)
    page3 = list_query_executions("alice", limit=4, offset=8)

    assert len(page1) == 4
    assert len(page2) == 4
    assert len(page3) == 2

    # No overlap
    ids1 = {r["execution_id"] for r in page1}
    ids2 = {r["execution_id"] for r in page2}
    assert ids1.isdisjoint(ids2)


def test_list_query_executions_newest_first(log_db):
    _write_n_logs(5, "alice", 1, "success", log_db)
    rows = list_query_executions("alice")
    executed_ats = [r["executed_at"] for r in rows]
    assert executed_ats == sorted(executed_ats, reverse=True)


# ===========================================================================
# ── 5: execute_generated_query calls log_query_execution at every exit ──────
# ===========================================================================

def test_log_called_on_safety_gate_block(monkeypatch, log_db):
    """Safety gate block → log_query_execution called with error_code='safety_gate'."""
    _patch_exec_deps(monkeypatch)
    log_calls: list[dict] = []

    orig = svc.log_query_execution
    def _capture(*args, **kwargs):
        log_calls.append(kwargs)
        return orig(*args, **kwargs)

    monkeypatch.setattr(svc, "log_query_execution", _capture)

    result = execute_generated_query(
        1, "user1",
        _gen_result(validated=False),
        _sql_plan(),
    )

    assert result["status"] == "governance_block"
    assert len(log_calls) == 1
    assert log_calls[0]["status"] == "governance_block"
    assert log_calls[0]["error_code"] == "safety_gate"
    assert log_calls[0]["row_count"] == 0
    assert log_calls[0]["truncated"] is False


def test_log_called_on_pii_governance_block(monkeypatch, log_db):
    """PII block → log_query_execution called with error_code='pii_blocked'."""
    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (MagicMock(), "mssql"))
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    def _pii_blocked(sid, uid, plan):
        return ["dbo.t.ssn"], set(), []

    monkeypatch.setattr(svc, "_governance_recheck", _pii_blocked)

    log_calls: list[dict] = []
    orig = svc.log_query_execution
    def _capture(*args, **kwargs):
        log_calls.append(kwargs)
        return orig(*args, **kwargs)

    monkeypatch.setattr(svc, "log_query_execution", _capture)

    result = execute_generated_query(1, "user1", _gen_result(), _sql_plan())

    assert result["status"] == "governance_block"
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "pii_blocked"


def test_log_called_on_connection_failure(monkeypatch, log_db):
    """Connection failure → log_query_execution called with error_code='connection_failed'."""
    monkeypatch.setattr(svc, "_governance_recheck", _no_governance)
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    def _fail(sid, uid):
        raise RuntimeError("ODBC error")

    monkeypatch.setattr(svc, "_load_source_connection", _fail)

    log_calls: list[dict] = []
    orig = svc.log_query_execution
    def _capture(*args, **kwargs):
        log_calls.append(kwargs)
        return orig(*args, **kwargs)

    monkeypatch.setattr(svc, "log_query_execution", _capture)

    result = execute_generated_query(1, "user1", _gen_result(), _sql_plan())

    assert result["status"] == "failed"
    assert len(log_calls) == 1
    assert log_calls[0]["error_code"] == "connection_failed"
    assert log_calls[0]["row_count"] == 0


def test_log_called_on_timeout(monkeypatch, log_db):
    """Timeout → log_query_execution called with status='timeout', error_code='timeout'."""
    monkeypatch.setattr(svc, "DEFAULT_QUERY_TIMEOUT_S", 1)
    monkeypatch.setattr(svc, "_governance_recheck", _no_governance)
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    def _slow_execute(sql, params):
        time.sleep(10)

    cursor = MagicMock()
    cursor.execute.side_effect = _slow_execute
    conn = _make_db_conn(cursor)
    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (conn, "mssql"))

    log_calls: list[dict] = []
    orig = svc.log_query_execution
    def _capture(*args, **kwargs):
        log_calls.append(kwargs)
        return orig(*args, **kwargs)

    monkeypatch.setattr(svc, "log_query_execution", _capture)

    result = execute_generated_query(1, "user1", _gen_result(), _sql_plan())

    assert result["status"] == "timeout"
    assert len(log_calls) == 1
    assert log_calls[0]["status"] == "timeout"
    assert log_calls[0]["error_code"] == "timeout"


def test_log_called_on_success_with_correct_counts(monkeypatch, log_db):
    """Success → log_query_execution called with actual row_count and truncated."""
    rows = [(i,) for i in range(5)]
    cursor = _make_cursor(rows, ["n"])
    conn   = _make_db_conn(cursor)
    _patch_exec_deps(monkeypatch, db_conn=conn)

    log_calls: list[dict] = []
    orig = svc.log_query_execution
    def _capture(*args, **kwargs):
        log_calls.append(kwargs)
        return orig(*args, **kwargs)

    monkeypatch.setattr(svc, "log_query_execution", _capture)

    result = execute_generated_query(
        1, "user1",
        _gen_result(params=["v1", "v2"]),
        _sql_plan(limits={"row_limit": 100}),
    )

    assert result["status"] == "success"
    assert len(log_calls) == 1
    kw = log_calls[0]
    assert kw["status"]      == "success"
    assert kw["error_code"]  is None
    assert kw["row_count"]   == 5
    assert kw["truncated"]   is False
    assert kw["param_count"] == 2


# ===========================================================================
# ── 6: _write_audit includes execution_id in payload ───────────────────────
# ===========================================================================

def test_write_audit_includes_execution_id_in_payload(monkeypatch, log_db):
    rows = [(1,)]
    cursor = _make_cursor(rows, ["n"])
    conn   = _make_db_conn(cursor)
    _patch_exec_deps(monkeypatch, db_conn=conn)

    audit_calls: list[dict] = []

    def _capture_audit(task_result, user_id=None):
        audit_calls.append(task_result)

    monkeypatch.setattr(svc, "log_audit_event", _capture_audit)
    monkeypatch.setattr(svc, "log_query_execution", lambda *a, **kw: None)

    result = execute_generated_query(1, "user1", _gen_result(), _sql_plan())

    assert len(audit_calls) == 1
    payload = json.loads(audit_calls[0]["original_input"])
    assert "execution_id" in payload
    assert payload["execution_id"] == result["execution_id"]
    assert payload["row_count"] >= 0
    assert isinstance(payload["truncated"], bool)


# ===========================================================================
# ── 7: Route tests
# ===========================================================================

def _build_test_client():
    """Build FastAPI TestClient with all auth/downstream deps mocked."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    # Minimal JWT stub
    from auth.jwt_auth import require_jwt
    from auth.api_key import AuthenticatedUser

    def _stub_jwt():
        return AuthenticatedUser(user_id="route_user", role="user")

    app.dependency_overrides[require_jwt] = _stub_jwt

    from api.v1.routes import router
    app.include_router(router, prefix="/v1")

    return TestClient(app, raise_server_exceptions=False)


def test_get_query_execution_route_returns_200(monkeypatch, log_db):
    eid = str(uuid.uuid4())
    log_query_execution(
        eid, "route_user", 7, "SELECT 1", _sql_plan(),
        param_count=0, row_count=3, truncated=False,
        duration_ms=20, status="success",
        error_code=None, executed_at="2026-07-01T10:00:00+00:00",
    )

    monkeypatch.setattr(svc, "get_connection", lambda: log_db)

    client = _build_test_client()
    resp = client.get(f"/v1/query-executions/{eid}")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["execution_id"] == eid
    assert data["status"]       == "success"
    assert data["row_count"]    == 3
    assert data["source_id"]    == 7


def test_get_query_execution_route_returns_404_for_wrong_user(monkeypatch, log_db):
    eid = str(uuid.uuid4())
    # Written under "other_user", route user is "route_user"
    log_query_execution(
        eid, "other_user", 1, "SELECT 1", _sql_plan(),
        param_count=0, row_count=0, truncated=False,
        duration_ms=1, status="success",
        error_code=None, executed_at="2026-07-01T00:00:00+00:00",
    )

    monkeypatch.setattr(svc, "get_connection", lambda: log_db)

    client = _build_test_client()
    resp = client.get(f"/v1/query-executions/{eid}")

    assert resp.status_code == 404


def test_list_query_executions_route_returns_paginated_list(monkeypatch, log_db):
    _write_n_logs(5, "route_user", 3, "success", log_db)

    monkeypatch.setattr(svc, "get_connection", lambda: log_db)

    client = _build_test_client()
    resp = client.get("/v1/query-executions?limit=3&offset=0")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 3
    # Each record belongs to the authenticated user
    for rec in data:
        assert rec["user_id"] == "route_user"


def test_list_query_executions_route_filters_by_source_id(monkeypatch, log_db):
    _write_n_logs(4, "route_user", 10, "success", log_db)
    _write_n_logs(2, "route_user", 20, "success", log_db)

    monkeypatch.setattr(svc, "get_connection", lambda: log_db)

    client = _build_test_client()
    resp = client.get("/v1/query-executions?source_id=10")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 4
    assert all(r["source_id"] == 10 for r in data)
