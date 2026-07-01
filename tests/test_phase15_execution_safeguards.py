"""
Phase 15 tests — Execution Operational Safeguards (Phase 6.3).

Coverage:
  - Per-user 2-second rate limit blocks rapid re-execution
  - Rate limit passes after window elapses
  - Daily limit (DAILY_LIMIT) blocks on the N+1-th execution
  - Per-source-per-minute limit blocks when threshold reached
  - Repeated-query warning appears after REPEATED_QUERY_THRESHOLD runs
  - Repeated-query warning is not a block — status remains "success"
  - Rate-limited attempts are logged to query_execution_log
  - Rate-limited attempts still write audit event (never raw SQL)
  - get_execution_readiness returns all required keys
  - get_execution_readiness reflects current rate-limit state
  - get_execution_readiness reflects repeated_query_warnings count
  - GET /v1/query-executions/readiness endpoint returns 200
  - GET /v1/query-executions/readiness not swallowed by /{execution_id} route
  - No raw SQL stored in execution log for any path
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase15-safeguards-secret-long-enough-abc")
os.environ.setdefault("USER_ID_SALT", "test-phase15-salt-long-enough-value-1234567890")

import pytest

import data.query_execution_service as svc
from data.query_execution_service import (
    DAILY_LIMIT,
    RATE_LIMIT_WINDOW_S,
    REPEATED_QUERY_THRESHOLD,
    REPEATED_QUERY_WINDOW_S,
    SOURCE_RATE_PER_MINUTE,
    _check_daily_limit,
    _check_repeated_query,
    _check_source_rate,
    _check_user_rate_limit,
    execute_generated_query,
    get_execution_readiness,
    log_query_execution,
)


# ---------------------------------------------------------------------------
# In-memory DB fixture (reused pattern from Phase 14)
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
    """Proxy sqlite3.Connection with a no-op close() for in-memory DB tests."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):
        pass

    @property
    def row_factory(self):
        return self._inner.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._inner.row_factory = value


def _make_log_db() -> _KeepAliveConn:
    inner = sqlite3.connect(":memory:", check_same_thread=False)
    inner.row_factory = sqlite3.Row
    inner.execute(_QEL_DDL)
    inner.commit()
    return _KeepAliveConn(inner)


@pytest.fixture
def log_db(monkeypatch):
    conn = _make_log_db()
    monkeypatch.setattr(svc, "get_connection", lambda: conn)
    return conn


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_log(log_db, user_id="u1", source_id=1, status="success",
               sql_hash=None, executed_at=None):
    """Insert a row directly into query_execution_log."""
    if executed_at is None:
        executed_at = _iso(datetime.now(timezone.utc))
    log_db._inner.execute(
        """
        INSERT INTO query_execution_log
            (execution_id, user_id, source_id, sql_hash, tables_accessed_json,
             param_count, row_count, truncated, duration_ms, status,
             error_code, executed_at, created_at)
        VALUES (?, ?, ?, ?, '[]', 0, 0, 0, 10, ?, NULL, ?, ?)
        """,
        (str(uuid.uuid4()), user_id, source_id, sql_hash,
         status, executed_at, executed_at),
    )
    log_db._inner.commit()


def _gen_result(sql="SELECT [n] FROM [dbo].[t]", validated=True,
                select_only=True, params=None):
    p = params or []
    return {
        "sql":        sql,
        "parameters": {"values": p, "placeholder": "?", "count": len(p)},
        "dialect":    "mssql",
        "safety":     {"read_only": True, "parameterized": True,
                       "validated": validated, "select_only": select_only},
        "warnings":   [],
        "explanation": [],
    }


def _sql_plan(valid=True):
    return {
        "select":   [{"table_fqn": "dbo.t", "column_name": "n",
                      "alias": "n", "aggregation": None}],
        "from":     {"table_fqn": "dbo.t", "alias": "t"},
        "joins":    [],
        "where":    [],
        "group_by": [],
        "limits":   {"row_limit": 100},
        "warnings": [],
        "validation": {
            "valid": valid, "read_only": True, "checks": {},
            "blocking_reasons": [] if valid else ["Plan invalid."],
        },
    }


def _make_cursor(rows=None, col_names=None):
    cursor = MagicMock()
    col_names = col_names or ["n"]
    rows      = rows or [(1,)]
    cursor.description = [(n, None, None, None, None, None, None) for n in col_names]
    cursor.fetchmany.return_value = rows
    return cursor


def _patch_exec_deps(monkeypatch, cursor=None):
    cursor = cursor or _make_cursor()
    conn   = MagicMock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (conn, "mssql"))
    monkeypatch.setattr(svc, "_governance_recheck", lambda *a, **kw: ([], set(), []))
    monkeypatch.setattr(svc, "log_audit_event", lambda *a, **kw: None)


# ===========================================================================
# ── 1: _check_user_rate_limit ───────────────────────────────────────────────
# ===========================================================================

def test_rate_limit_not_triggered_on_first_execution(log_db):
    assert _check_user_rate_limit("new_user") is False


def test_rate_limit_triggered_within_window(log_db):
    # Insert an execution timestamped 0.5 seconds ago
    recent = _iso(datetime.now(timezone.utc) - timedelta(milliseconds=500))
    _write_log(log_db, user_id="u1", executed_at=recent)
    assert _check_user_rate_limit("u1") is True


def test_rate_limit_cleared_after_window(log_db):
    # Insert an execution timestamped RATE_LIMIT_WINDOW_S + 1 seconds ago
    old = _iso(
        datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_S + 1)
    )
    _write_log(log_db, user_id="u1", executed_at=old)
    assert _check_user_rate_limit("u1") is False


def test_rate_limit_isolates_by_user(log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(milliseconds=500))
    _write_log(log_db, user_id="alice", executed_at=recent)
    # alice is rate-limited; bob is not
    assert _check_user_rate_limit("alice") is True
    assert _check_user_rate_limit("bob")   is False


# ===========================================================================
# ── 2: _check_daily_limit ───────────────────────────────────────────────────
# ===========================================================================

def test_daily_limit_count_zero_for_new_user(log_db):
    assert _check_daily_limit("fresh_user") == 0


def test_daily_limit_counts_only_todays_executions(log_db):
    now      = datetime.now(timezone.utc)
    today    = _iso(now - timedelta(hours=1))
    yesterday = _iso(now - timedelta(hours=25))  # > 24h ago = yesterday

    _write_log(log_db, user_id="u1", executed_at=today)
    _write_log(log_db, user_id="u1", executed_at=today)
    _write_log(log_db, user_id="u1", executed_at=yesterday)

    assert _check_daily_limit("u1") == 2   # yesterday's row excluded


def test_daily_limit_counts_all_statuses(log_db):
    now = _iso(datetime.now(timezone.utc))
    for status in ("success", "failed", "timeout", "governance_block", "rate_limited"):
        _write_log(log_db, user_id="u1", status=status, executed_at=now)
    assert _check_daily_limit("u1") == 5


# ===========================================================================
# ── 3: _check_source_rate ───────────────────────────────────────────────────
# ===========================================================================

def test_source_rate_zero_for_no_recent_executions(log_db):
    assert _check_source_rate(99) == 0


def test_source_rate_counts_within_60_seconds(log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    for _ in range(5):
        _write_log(log_db, source_id=7, executed_at=recent)
    assert _check_source_rate(7) == 5


def test_source_rate_excludes_old_executions(log_db):
    old = _iso(datetime.now(timezone.utc) - timedelta(seconds=90))
    _write_log(log_db, source_id=7, executed_at=old)
    assert _check_source_rate(7) == 0


def test_source_rate_isolates_by_source(log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    for _ in range(3):
        _write_log(log_db, source_id=1, executed_at=recent)
    for _ in range(2):
        _write_log(log_db, source_id=2, executed_at=recent)

    assert _check_source_rate(1) == 3
    assert _check_source_rate(2) == 2


# ===========================================================================
# ── 4: _check_repeated_query ────────────────────────────────────────────────
# ===========================================================================

def test_repeated_query_zero_for_unknown_hash(log_db):
    assert _check_repeated_query("u1", "abc123") == 0


def test_repeated_query_returns_none_for_null_hash(log_db):
    assert _check_repeated_query("u1", None) == 0


def test_repeated_query_counts_matching_hash_in_window(log_db):
    h     = hashlib.sha256(b"SELECT 1").hexdigest()
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=60))
    for _ in range(3):
        _write_log(log_db, user_id="u1", sql_hash=h, executed_at=recent)
    assert _check_repeated_query("u1", h) == 3


def test_repeated_query_excludes_old_entries(log_db):
    h   = hashlib.sha256(b"SELECT 1").hexdigest()
    old = _iso(
        datetime.now(timezone.utc) - timedelta(seconds=REPEATED_QUERY_WINDOW_S + 60)
    )
    _write_log(log_db, user_id="u1", sql_hash=h, executed_at=old)
    assert _check_repeated_query("u1", h) == 0


def test_repeated_query_isolates_by_user(log_db):
    h     = hashlib.sha256(b"SELECT 1").hexdigest()
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
    for _ in range(3):
        _write_log(log_db, user_id="alice", sql_hash=h, executed_at=recent)
    assert _check_repeated_query("alice", h) == 3
    assert _check_repeated_query("bob",   h) == 0


# ===========================================================================
# ── 5: execute_generated_query — rate limit blocks ──────────────────────────
# ===========================================================================

def test_user_rate_limit_blocks_rapid_second_execution(monkeypatch, log_db):
    _patch_exec_deps(monkeypatch)

    # First execution — allowed
    r1 = execute_generated_query(1, "u1", _gen_result(), _sql_plan())
    assert r1["status"] in ("success", "governance_block", "failed")  # any non-rate_limited

    # Immediate second execution — should be rate-limited
    r2 = execute_generated_query(1, "u1", _gen_result(), _sql_plan())
    assert r2["status"] == "rate_limited"
    assert "rate limit" in r2["error"].lower()
    assert r2["row_count"] == 0


def test_user_rate_limit_passes_after_window(monkeypatch, log_db):
    # Write a log entry older than RATE_LIMIT_WINDOW_S
    old = _iso(
        datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_S + 1)
    )
    _write_log(log_db, user_id="u1", executed_at=old)
    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())
    assert result["status"] != "rate_limited"


def test_daily_limit_blocks_on_exceeded_count(monkeypatch, log_db):
    # Pre-seed DAILY_LIMIT rows for today
    now = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    for _ in range(DAILY_LIMIT):
        _write_log(log_db, user_id="u1", executed_at=now)

    # Also bypass per-user rate limit (last row is > 2 seconds old is fine because
    # rows are all 5 minutes ago — but that means rate limit not triggered)
    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())
    assert result["status"] == "rate_limited"
    assert "daily" in result["error"].lower()


def test_source_rate_limit_blocks_when_threshold_reached(monkeypatch, log_db):
    # Pre-seed SOURCE_RATE_PER_MINUTE rows for source 42 in the last 60 seconds
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    for _ in range(SOURCE_RATE_PER_MINUTE):
        _write_log(log_db, source_id=42, executed_at=recent)

    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(42, "u1", _gen_result(), _sql_plan())
    assert result["status"] == "rate_limited"
    assert "source" in result["error"].lower()


# ===========================================================================
# ── 6: Rate-limited attempts are still logged and audited ───────────────────
# ===========================================================================

def test_rate_limited_attempt_logged_to_execution_log(monkeypatch, log_db):
    # Trigger user rate limit by pre-seeding a very recent row
    recent = _iso(datetime.now(timezone.utc) - timedelta(milliseconds=100))
    _write_log(log_db, user_id="u1", executed_at=recent)
    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())
    assert result["status"] == "rate_limited"

    # Check that the rate-limited attempt was logged
    rows = log_db._inner.execute(
        "SELECT status, error_code FROM query_execution_log "
        "WHERE status = 'rate_limited' AND user_id = 'u1'"
    ).fetchall()
    assert len(rows) >= 1
    assert rows[-1]["error_code"] in (
        "user_rate_limit", "daily_limit", "source_rate_limit"
    )


def test_rate_limited_attempt_writes_audit_event(monkeypatch, log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(milliseconds=100))
    _write_log(log_db, user_id="u1", executed_at=recent)

    audit_calls: list = []
    monkeypatch.setattr(svc, "log_audit_event",
                        lambda t, user_id=None: audit_calls.append(t))
    monkeypatch.setattr(svc, "_load_source_connection",
                        lambda s, u: (MagicMock(), "mssql"))
    monkeypatch.setattr(svc, "_governance_recheck",
                        lambda *a, **kw: ([], set(), []))

    execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert len(audit_calls) >= 1
    last = audit_calls[-1]
    assert last["status"] == "rate_limited"
    # Verify raw SQL never appears in audit payload
    payload_str = str(last)
    assert "SELECT" not in payload_str


def test_no_raw_sql_in_rate_limited_log_row(monkeypatch, log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(milliseconds=100))
    _write_log(log_db, user_id="u1", executed_at=recent)
    _patch_exec_deps(monkeypatch)

    secret_sql = "SELECT [ssn] FROM [hr].[employees] WHERE [dept] = ?"
    execute_generated_query(1, "u1", _gen_result(sql=secret_sql), _sql_plan())

    rows = log_db._inner.execute(
        "SELECT * FROM query_execution_log WHERE status = 'rate_limited'"
    ).fetchall()
    for row in rows:
        for col in row.keys():
            val = str(row[col]) if row[col] is not None else ""
            assert secret_sql not in val, f"Raw SQL found in column '{col}'"


# ===========================================================================
# ── 7: Repeated-query warning ───────────────────────────────────────────────
# ===========================================================================

def test_repeated_query_warning_appears_after_threshold(monkeypatch, log_db):
    sql  = "SELECT [n] FROM [dbo].[t]"
    h    = hashlib.sha256(sql.encode()).hexdigest()
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))

    # Pre-seed REPEATED_QUERY_THRESHOLD rows so the next run triggers the warning
    for _ in range(REPEATED_QUERY_THRESHOLD):
        _write_log(log_db, user_id="u1", sql_hash=h, executed_at=recent)

    # Bypass rate limit (last entry was 10 seconds ago, > RATE_LIMIT_WINDOW_S)
    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(1, "u1", _gen_result(sql=sql), _sql_plan())

    # Must not be blocked — only a warning
    assert result["status"] == "success"
    warning_types = [w["type"] for w in result["warnings"]]
    assert "repeated_query" in warning_types


def test_repeated_query_does_not_block_execution(monkeypatch, log_db):
    sql  = "SELECT [n] FROM [dbo].[t]"
    h    = hashlib.sha256(sql.encode()).hexdigest()
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))

    for _ in range(REPEATED_QUERY_THRESHOLD + 5):
        _write_log(log_db, user_id="u1", sql_hash=h, executed_at=recent)

    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(1, "u1", _gen_result(sql=sql), _sql_plan())
    assert result["status"] == "success"
    assert result["row_count"] >= 0   # rows returned normally


def test_no_repeated_query_warning_below_threshold(monkeypatch, log_db):
    sql  = "SELECT [n] FROM [dbo].[t]"
    h    = hashlib.sha256(sql.encode()).hexdigest()
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))

    # One prior run (below threshold)
    _write_log(log_db, user_id="u1", sql_hash=h, executed_at=recent)

    _patch_exec_deps(monkeypatch)

    result = execute_generated_query(1, "u1", _gen_result(sql=sql), _sql_plan())

    warning_types = [w["type"] for w in result["warnings"]]
    assert "repeated_query" not in warning_types


# ===========================================================================
# ── 8: get_execution_readiness ──────────────────────────────────────────────
# ===========================================================================

def test_readiness_has_all_required_keys(log_db):
    r = get_execution_readiness("u1")
    required = {
        "executions_today", "user_rate_limited", "daily_remaining",
        "source_recent_count", "recent_failures", "recent_timeouts",
        "repeated_query_warnings",
    }
    assert required.issubset(r.keys())


def test_readiness_executions_today_counts_correctly(log_db):
    now = _iso(datetime.now(timezone.utc) - timedelta(minutes=2))
    for _ in range(7):
        _write_log(log_db, user_id="u1", executed_at=now)
    r = get_execution_readiness("u1")
    assert r["executions_today"] == 7


def test_readiness_daily_remaining_decreases(log_db):
    now = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    for _ in range(10):
        _write_log(log_db, user_id="u1", executed_at=now)
    r = get_execution_readiness("u1")
    assert r["daily_remaining"] == DAILY_LIMIT - 10


def test_readiness_user_rate_limited_true_when_recent(log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(milliseconds=500))
    _write_log(log_db, user_id="u1", executed_at=recent)
    r = get_execution_readiness("u1")
    assert r["user_rate_limited"] is True


def test_readiness_user_rate_limited_false_when_window_passed(log_db):
    old = _iso(datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_S + 5))
    _write_log(log_db, user_id="u1", executed_at=old)
    r = get_execution_readiness("u1")
    assert r["user_rate_limited"] is False


def test_readiness_source_recent_count_with_source_id(log_db):
    recent = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    for _ in range(5):
        _write_log(log_db, source_id=9, executed_at=recent)
    r = get_execution_readiness("u1", source_id=9)
    assert r["source_recent_count"] == 5


def test_readiness_recent_failures_and_timeouts(log_db):
    now = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    _write_log(log_db, user_id="u1", status="failed",  executed_at=now)
    _write_log(log_db, user_id="u1", status="failed",  executed_at=now)
    _write_log(log_db, user_id="u1", status="timeout", executed_at=now)
    r = get_execution_readiness("u1")
    assert r["recent_failures"]  == 2
    assert r["recent_timeouts"]  == 1


def test_readiness_repeated_query_warnings_count(log_db):
    h1 = hashlib.sha256(b"SELECT 1").hexdigest()
    h2 = hashlib.sha256(b"SELECT 2").hexdigest()
    now = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))

    # h1: runs REPEATED_QUERY_THRESHOLD times → 1 warning
    for _ in range(REPEATED_QUERY_THRESHOLD):
        _write_log(log_db, user_id="u1", sql_hash=h1, executed_at=now)

    # h2: runs REPEATED_QUERY_THRESHOLD - 1 times → no warning
    for _ in range(REPEATED_QUERY_THRESHOLD - 1):
        _write_log(log_db, user_id="u1", sql_hash=h2, executed_at=now)

    r = get_execution_readiness("u1")
    assert r["repeated_query_warnings"] == 1


# ===========================================================================
# ── 9: Readiness route ──────────────────────────────────────────────────────
# ===========================================================================

def _build_test_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from auth.jwt_auth import require_jwt
    from auth.api_key import AuthenticatedUser

    app = FastAPI()
    app.dependency_overrides[require_jwt] = lambda: AuthenticatedUser(
        user_id="route_user", role="user"
    )
    from api.v1.routes import router
    app.include_router(router, prefix="/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_readiness_route_returns_200(monkeypatch, log_db):
    monkeypatch.setattr(svc, "get_connection", lambda: log_db)
    client = _build_test_client()
    resp = client.get("/v1/query-executions/readiness")
    assert resp.status_code == 200
    data = resp.json()["data"]
    for key in ("executions_today", "user_rate_limited", "daily_remaining",
                "source_recent_count", "recent_failures", "recent_timeouts",
                "repeated_query_warnings"):
        assert key in data


def test_readiness_route_not_matched_as_execution_id(monkeypatch, log_db):
    """GET /readiness must NOT be caught by the /{execution_id} route."""
    monkeypatch.setattr(svc, "get_connection", lambda: log_db)
    client = _build_test_client()
    resp = client.get("/v1/query-executions/readiness")
    # If it were swallowed by /{execution_id}, it would return 404 with
    # "Query execution log not found." — we must get 200 instead.
    assert resp.status_code == 200
    assert "executions_today" in resp.json()["data"]


def test_readiness_route_accepts_source_id_param(monkeypatch, log_db):
    monkeypatch.setattr(svc, "get_connection", lambda: log_db)
    client = _build_test_client()
    resp = client.get("/v1/query-executions/readiness?source_id=5")
    assert resp.status_code == 200
    # source_recent_count present (0 since no rows for source 5)
    assert resp.json()["data"]["source_recent_count"] == 0
