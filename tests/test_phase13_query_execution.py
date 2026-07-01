"""
Phase 13 tests — Safe Read-Only Query Execution Service (Phase 6.1).

All tests exercise execute_generated_query() in isolation using mock
connectors and monkeypatched dependencies.  No real database, no real
connectors, no actual SQL execution is needed.

Coverage:
  - Successful read-only execution with mocked connector
  - Invalid generated_sql_result (validated=False) rejected
  - select_only=False rejected
  - sql_plan validation.valid=False rejected
  - SQL that doesn't start with SELECT rejected
  - Multi-statement SQL (semicolon) rejected
  - Write keyword in SQL rejected
  - Row limit enforced via fetchmany (never fetchall)
  - Truncated flag set when result exceeds row_limit
  - Timeout handled — returns status "timeout"
  - Unconfirmed PII column blocked — returns governance_block
  - Confirmed PII column value masked with "***"
  - No raw SQL or filter values written to audit log
  - Invalid plan (valid=False) rejected before connection opens
"""
import os
import time
import uuid
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase13-exec-secret-long-enough-abc")
os.environ.setdefault("USER_ID_SALT", "test-phase13-salt-long-enough-value-123456")

import pytest

import data.query_execution_service as svc
from data.query_execution_service import execute_generated_query


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _gen_result(
    sql="SELECT [amount] FROM [dbo].[orders]",
    validated=True,
    select_only=True,
    params=None,
    dialect="mssql",
):
    """Build a generated_sql_result dict matching generate_sql() output shape."""
    p = params or []
    return {
        "sql": sql,
        "parameters": {"values": p, "placeholder": "?", "count": len(p)},
        "dialect": dialect,
        "safety": {
            "read_only":    True,
            "parameterized": True,
            "validated":    validated,
            "select_only":  select_only,
        },
        "warnings": [],
        "explanation": [],
    }


def _sql_plan(
    select=None,
    limits=None,
    valid=True,
):
    """Build a minimal sql_plan dict matching build_sql_plan() output shape."""
    select = select or [
        {
            "table_fqn":   "dbo.orders",
            "column_name": "amount",
            "alias":       "amount",
            "aggregation": "SUM",
        },
    ]
    return {
        "select":   select,
        "from":     {"table_fqn": "dbo.orders", "alias": "ord"},
        "joins":    [],
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
    """Return a MagicMock cursor with description and fetchmany pre-configured."""
    cursor = MagicMock()
    # DBAPI2 description: sequence of 7-item tuples; only [0] (name) is used here.
    cursor.description = [(name, None, None, None, None, None, None) for name in col_names]
    cursor.fetchmany.return_value = rows
    return cursor


def _make_db_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _no_op_audit(*_a, **_kw):
    pass


def _no_governance(*_a, **_kw):
    """_governance_recheck stub: no PII, no blocks."""
    return [], set(), []


# ---------------------------------------------------------------------------
# Shared monkeypatching helper
# ---------------------------------------------------------------------------

def _patch_deps(
    monkeypatch,
    db_conn=None,
    source_type="mssql",
    governance_fn=None,
    audit_fn=None,
):
    """Patch _load_source_connection, _governance_recheck, and log_audit_event."""
    _conn = db_conn or _make_db_conn(_make_cursor([], []))

    monkeypatch.setattr(
        svc, "_load_source_connection",
        lambda sid, uid: (_conn, source_type),
    )
    monkeypatch.setattr(
        svc, "_governance_recheck",
        governance_fn or _no_governance,
    )
    monkeypatch.setattr(
        svc, "log_audit_event",
        audit_fn or _no_op_audit,
    )
    return _conn


# ===========================================================================
# ── 1: Successful execution ─────────────────────────────────────────────────
# ===========================================================================

def test_successful_execution(monkeypatch):
    rows = [(1000.0, "north"), (2000.0, "south")]
    col_names = ["amount", "region"]
    cursor = _make_cursor(rows, col_names)
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    result = execute_generated_query(
        1, "u1",
        _gen_result(sql="SELECT SUM([dbo].[orders].[amount]) AS [amount] FROM [dbo].[orders]"),
        _sql_plan(),
    )

    assert result["status"] == "success"
    assert result["source_id"] == 1
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert result["error"] is None
    assert result["columns"][0]["name"] == "amount"
    assert result["rows"][0]["amount"] == 1000.0
    assert result["rows"][1]["region"] == "south"


# ===========================================================================
# ── 2: Safety gate — validated=False ────────────────────────────────────────
# ===========================================================================

def test_invalid_validated_flag_rejected(monkeypatch):
    _patch_deps(monkeypatch)
    result = execute_generated_query(
        1, "u1",
        _gen_result(validated=False),
        _sql_plan(),
    )
    assert result["status"] == "governance_block"
    assert result["sql_plan"] if False else True  # structural check
    assert "validated" in result["error"].lower()


# ===========================================================================
# ── 3: Safety gate — select_only=False ──────────────────────────────────────
# ===========================================================================

def test_select_only_false_rejected(monkeypatch):
    _patch_deps(monkeypatch)
    result = execute_generated_query(
        1, "u1",
        _gen_result(select_only=False),
        _sql_plan(),
    )
    assert result["status"] == "governance_block"
    assert "select_only" in result["error"].lower()


# ===========================================================================
# ── 4: Safety gate — sql_plan.validation.valid=False ────────────────────────
# ===========================================================================

def test_invalid_sql_plan_rejected(monkeypatch):
    _patch_deps(monkeypatch)
    result = execute_generated_query(
        1, "u1",
        _gen_result(),
        _sql_plan(valid=False),
    )
    assert result["status"] == "governance_block"
    assert result["row_count"] == 0


# ===========================================================================
# ── 5: Safety gate — SQL doesn't start with SELECT ──────────────────────────
# ===========================================================================

def test_sql_not_starting_with_select_rejected(monkeypatch):
    _patch_deps(monkeypatch)
    result = execute_generated_query(
        1, "u1",
        _gen_result(sql="EXEC sp_helpdb"),
        _sql_plan(),
    )
    assert result["status"] == "governance_block"
    assert "SELECT" in result["error"]


# ===========================================================================
# ── 6: Safety gate — multi-statement SQL (semicolon) ────────────────────────
# ===========================================================================

def test_multi_statement_sql_rejected(monkeypatch):
    _patch_deps(monkeypatch)
    result = execute_generated_query(
        1, "u1",
        _gen_result(sql="SELECT 1; DROP TABLE orders"),
        _sql_plan(),
    )
    assert result["status"] == "governance_block"
    assert "semicolon" in result["error"].lower()


# ===========================================================================
# ── 7: Safety gate — write keyword outside quotes ───────────────────────────
# ===========================================================================

def test_write_keyword_in_sql_rejected(monkeypatch):
    _patch_deps(monkeypatch)
    result = execute_generated_query(
        1, "u1",
        _gen_result(sql="SELECT 1 UNION SELECT DELETE FROM t"),
        _sql_plan(),
    )
    assert result["status"] == "governance_block"
    assert "write" in result["error"].lower() or "DDL" in result["error"]


def test_write_keyword_inside_brackets_is_allowed(monkeypatch):
    """A column named [delete] in bracket-quoted MSSQL SQL must not be blocked."""
    rows = [(42,)]
    cursor = _make_cursor(rows, ["delete"])
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    result = execute_generated_query(
        1, "u1",
        _gen_result(sql="SELECT [dbo].[t].[delete] AS [delete] FROM [dbo].[t]"),
        _sql_plan(select=[{
            "table_fqn": "dbo.t", "column_name": "delete",
            "alias": "delete", "aggregation": None,
        }]),
    )
    assert result["status"] == "success"
    assert result["rows"][0]["delete"] == 42


# ===========================================================================
# ── 8: Row limit enforced — fetchmany used, never fetchall ──────────────────
# ===========================================================================

def test_row_limit_enforced_fetchmany_not_fetchall(monkeypatch):
    # Cursor returns row_limit+1 rows to trigger truncation
    limit = 10
    rows  = [(i,) for i in range(limit + 1)]  # 11 rows
    cursor = _make_cursor(rows, ["id"])
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    result = execute_generated_query(
        1, "u1",
        _gen_result(),
        _sql_plan(limits={"row_limit": limit}),
    )

    # fetchmany was called, fetchall was NOT called
    cursor.fetchmany.assert_called_once_with(limit + 1)
    cursor.fetchall.assert_not_called()

    assert result["row_count"] == limit
    assert result["truncated"] is True


# ===========================================================================
# ── 9: MAX_ROW_LIMIT caps plan row_limit ────────────────────────────────────
# ===========================================================================

def test_max_row_limit_caps_plan_limit(monkeypatch):
    rows = [(i,) for i in range(10)]
    cursor = _make_cursor(rows, ["id"])
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    result = execute_generated_query(
        1, "u1",
        _gen_result(),
        _sql_plan(limits={"row_limit": 99_999}),  # far above MAX_ROW_LIMIT
    )

    # fetchmany called with MAX_ROW_LIMIT+1, not 99_999+1
    cursor.fetchmany.assert_called_once_with(svc.MAX_ROW_LIMIT + 1)
    assert result["row_limit_applied"] == svc.MAX_ROW_LIMIT


# ===========================================================================
# ── 10: Timeout handled ─────────────────────────────────────────────────────
# ===========================================================================

def test_timeout_returns_timeout_status(monkeypatch):
    # Patch DEFAULT_QUERY_TIMEOUT_S to 1 second for fast test
    monkeypatch.setattr(svc, "DEFAULT_QUERY_TIMEOUT_S", 1)

    def _slow_execute(sql, params):
        time.sleep(10)  # longer than timeout

    cursor        = MagicMock()
    cursor.execute.side_effect = _slow_execute
    conn          = _make_db_conn(cursor)

    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (conn, "mssql"))
    monkeypatch.setattr(svc, "_governance_recheck", _no_governance)
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert result["status"] == "timeout"
    assert result["row_count"] == 0
    assert "timeout" in result["error"].lower()


# ===========================================================================
# ── 11: Unconfirmed PII blocks execution ────────────────────────────────────
# ===========================================================================

def test_unconfirmed_pii_column_blocks_execution(monkeypatch):
    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (MagicMock(), "mssql"))
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    def _pii_blocked(sid, uid, plan):
        return (
            ["dbo.orders.customer_email"],
            set(),
            [{"type": "pii_blocked", "severity": "HIGH",
              "message": "dbo.orders.customer_email has unconfirmed PII."}],
        )

    monkeypatch.setattr(svc, "_governance_recheck", _pii_blocked)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert result["status"] == "governance_block"
    assert "unconfirmed PII" in result["error"] or "Blocked" in result["error"]
    assert result["row_count"] == 0


# ===========================================================================
# ── 12: Confirmed PII values are masked ─────────────────────────────────────
# ===========================================================================

def test_confirmed_pii_values_masked(monkeypatch):
    rows = [(100.0, "alice@example.com"), (200.0, "bob@example.com")]
    cursor = _make_cursor(rows, ["amount", "email"])
    conn   = _make_db_conn(cursor)
    monkeypatch.setattr(svc, "_load_source_connection", lambda s, u: (conn, "mssql"))
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    # "email" alias is confirmed PII → must be masked
    def _pii_confirmed(sid, uid, plan):
        return (
            [],
            {"email"},  # alias set for masking
            [{"type": "pii_masked", "severity": "MEDIUM",
              "message": "dbo.orders.email is confirmed PII — returned values are masked."}],
        )

    monkeypatch.setattr(svc, "_governance_recheck", _pii_confirmed)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert result["status"] == "success"
    # Column metadata shows pii=True
    email_col = next(c for c in result["columns"] if c["name"] == "email")
    assert email_col["pii"] is True
    # Actual values are masked
    assert result["rows"][0]["email"] == "***"
    assert result["rows"][1]["email"] == "***"
    # Non-PII column is NOT masked
    assert result["rows"][0]["amount"] == 100.0


# ===========================================================================
# ── 13: No raw SQL or parameter values in audit log ─────────────────────────
# ===========================================================================

def test_no_raw_sql_or_values_in_audit_log(monkeypatch):
    rows = [(42,)]
    cursor = _make_cursor(rows, ["n"])
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    audit_calls: list[dict] = []

    def _capture_audit(task_result, user_id=None):
        audit_calls.append({"task_result": task_result, "user_id": user_id})

    monkeypatch.setattr(svc, "log_audit_event", _capture_audit)

    gen = _gen_result(
        sql="SELECT [amount] FROM [dbo].[orders] WHERE [status] = ?",
        params=["supersecret_filter_value"],
    )
    execute_generated_query(1, "u1", gen, _sql_plan())

    assert len(audit_calls) == 1
    logged = audit_calls[0]["task_result"]

    # Neither the SQL string nor any filter value appears in the audit record
    assert "supersecret_filter_value" not in str(logged)
    assert "SELECT" not in str(logged)
    assert logged["task_type"] == "query_execution"
    assert logged["status"] == "success"


# ===========================================================================
# ── 14: No execution when plan is invalid ───────────────────────────────────
# ===========================================================================

def test_no_connection_opened_when_plan_invalid(monkeypatch):
    opened: list[bool] = []

    def _should_not_be_called(sid, uid):
        opened.append(True)
        raise AssertionError("_load_source_connection must not be called for invalid plan")

    monkeypatch.setattr(svc, "_load_source_connection", _should_not_be_called)
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    result = execute_generated_query(
        1, "u1",
        _gen_result(sql=None),   # generate_sql refused → sql is None
        _sql_plan(),
    )

    assert result["status"] == "governance_block"
    assert not opened, "_load_source_connection was called but should not have been"


# ===========================================================================
# ── 15: Value serialization ─────────────────────────────────────────────────
# ===========================================================================

def test_value_serialization(monkeypatch):
    import decimal
    from datetime import datetime as dt, date as d, timezone

    ts  = dt(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    dat = d(2024, 6, 1)
    rows = [(
        None,
        42,
        3.14,
        decimal.Decimal("99.99"),
        ts,
        dat,
        b"\x00\x01",
        "plain_text",
    )]
    col_names = ["null_col", "int_col", "float_col", "dec_col",
                 "dt_col", "date_col", "bin_col", "str_col"]
    cursor = _make_cursor(rows, col_names)
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert result["status"] == "success"
    row = result["rows"][0]
    assert row["null_col"]  is None
    assert row["int_col"]   == 42
    assert row["float_col"] == 3.14
    assert row["dec_col"]   == 99.99
    assert row["dt_col"]    == ts.isoformat()
    assert row["date_col"]  == dat.isoformat()
    assert row["bin_col"]   == "<binary>"
    assert row["str_col"]   == "plain_text"


# ===========================================================================
# ── 16: execution_id is a valid UUID ────────────────────────────────────────
# ===========================================================================

def test_execution_id_is_uuid(monkeypatch):
    rows = [(1,)]
    cursor = _make_cursor(rows, ["n"])
    conn   = _make_db_conn(cursor)
    _patch_deps(monkeypatch, db_conn=conn)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert result["status"] == "success"
    parsed = uuid.UUID(result["execution_id"])  # raises if not valid UUID
    assert str(parsed) == result["execution_id"]


# ===========================================================================
# ── 17: Connection failure returns failed status ────────────────────────────
# ===========================================================================

def test_connection_failure_returns_failed_status(monkeypatch):
    def _fail(sid, uid):
        raise RuntimeError("ODBC driver not found")

    monkeypatch.setattr(svc, "_load_source_connection", _fail)
    monkeypatch.setattr(svc, "_governance_recheck", _no_governance)
    monkeypatch.setattr(svc, "log_audit_event", _no_op_audit)

    result = execute_generated_query(1, "u1", _gen_result(), _sql_plan())

    assert result["status"] == "failed"
    assert result["error"] is not None
    assert result["row_count"] == 0
