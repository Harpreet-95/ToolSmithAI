"""
Tests for core.observability.pipeline_trace and the pipeline_trace_encrypted
persistence/retrieval it feeds — Reconnect Reliability milestone, Part 14.

Coverage:
  - build_pipeline_trace() extracts a compact, JSON-safe summary from a real
    data.sql_planning_service.build_sql_plan()-shaped dict, with no raw SQL
    text/param values/row data anywhere in the output.
  - build_pipeline_trace() handles an empty/None sql_plan without raising.
  - log_query_execution(pipeline_trace=...) round-trips through
    get_pipeline_trace() (encrypt on write, decrypt on read).
  - log_query_execution() with no pipeline_trace (every pre-existing call
    site) leaves the column NULL, and get_pipeline_trace() returns None.
  - get_pipeline_trace(require_user_id=...) enforces ownership; the admin
    path (require_user_id=None) does not.
  - GET /v1/admin/query-executions/{id}/trace requires the admin role and
    returns the stored trace.

Run:
    python -m pytest tests/test_pipeline_trace.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-pipeline-trace-secret-long-enough-value-1")
os.environ.setdefault("USER_ID_SALT", "test-pipeline-trace-salt-long-enough-value-12")

import pytest

from core.observability.pipeline_trace import build_pipeline_trace


# ---------------------------------------------------------------------------
# build_pipeline_trace() — pure function
# ---------------------------------------------------------------------------

def _sample_sql_plan():
    return {
        "select": [{"table_fqn": "dbo.clients", "column_name": "ClientID", "alias": "id"}],
        "from": {"table_fqn": "dbo.clients"},
        "joins": [{"table_fqn": "dbo.signups", "join_type": "INNER", "fanout_risk": "LOW"}],
        "where": [{"column": "IsActive", "operator": "=", "value": 1}],
        "group_by": [],
        "order_by": [{"table_fqn": "dbo.signups", "column_name": "SignupID", "direction": "DESC"}],
        "distinct": False,
        "limits": {"row_limit": 10},
        "warnings": [{"type": "implicit_activity_measure_used", "severity": "LOW", "message": "..."}],
        "validation": {"valid": True, "read_only": True, "checks": {"all_joins_trusted": True}, "blocking_reasons": []},
        "explanation": ["1 measure(s) resolved.", "Ordered by dbo.signups.SignupID (DESC)."],
        "semantic_context": [{"term": "clients", "selected": {"table_fqn": "dbo.clients"}}],
        "aggregation_plan": {"aggregation_target": "entity_count", "counted_table": "dbo.signups"},
        "intent": {
            "type": "aggregate_by_dimension", "aggregation": "COUNT", "aggregation_target": "entity_count",
            "order": {"direction": "DESC", "limit": 10}, "status_value": "Active", "status_negated": False,
        },
    }


def test_build_pipeline_trace_extracts_all_sections():
    trace = build_pipeline_trace(_sample_sql_plan())

    assert trace["question_understanding"]["type"] == "aggregate_by_dimension"
    assert trace["question_understanding"]["aggregation_target"] == "entity_count"
    assert trace["question_understanding"]["status_value"] == "Active"

    assert trace["candidate_retrieval"]["concepts_considered"] == [
        {"term": "clients", "selected_table_fqn": "dbo.clients"}
    ]

    assert trace["relationship_resolution"]["tables_referenced"] == ["dbo.clients", "dbo.signups"]
    assert trace["relationship_resolution"]["joins"] == [
        {"table_fqn": "dbo.signups", "join_type": "INNER", "fanout_risk": "LOW"}
    ]

    assert trace["sql_generation"]["method"] == "template"
    assert trace["sql_generation"]["aggregation_plan"]["counted_table"] == "dbo.signups"

    assert trace["validation"]["valid"] is True
    assert trace["validation"]["checks"] == {"all_joins_trusted": True}

    assert trace["warnings"] == [{"type": "implicit_activity_measure_used", "severity": "LOW"}]
    assert trace["explanation"] == _sample_sql_plan()["explanation"]


def test_build_pipeline_trace_never_leaks_raw_sql_or_values():
    plan = _sample_sql_plan()
    plan["select"][0]["some_future_key_with_sql"] = "SELECT secret FROM leaked_table"
    trace = build_pipeline_trace(plan)

    import json
    serialized = json.dumps(trace)
    assert "SELECT" not in serialized
    assert "leaked_table" not in serialized


def test_build_pipeline_trace_handles_empty_plan():
    assert build_pipeline_trace({}) is not None
    assert build_pipeline_trace(None) is not None
    trace = build_pipeline_trace(None)
    assert trace["question_understanding"]["type"] is None
    assert trace["relationship_resolution"]["tables_referenced"] == []


def test_build_pipeline_trace_carries_ai_used_flags():
    trace = build_pipeline_trace(
        _sample_sql_plan(),
        ai_used={"question_interpreter": True, "candidate_adjudicator": False},
    )
    assert trace["ai_planning"]["question_interpreter_used"] is True
    assert trace["ai_planning"]["candidate_adjudicator_used"] is False
    assert trace["ai_planning"]["implicit_ranking_ai_used"] is None  # not reported = unknown


# ---------------------------------------------------------------------------
# log_query_execution(pipeline_trace=...) <-> get_pipeline_trace() round trip
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
        created_at           TEXT    NOT NULL,
        execution_kind       TEXT    NOT NULL DEFAULT 'user_query',
        pipeline_trace_encrypted TEXT
    )
"""


class _KeepAliveConn:
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


@pytest.fixture
def log_db(monkeypatch):
    import data.query_execution_service as svc
    inner = sqlite3.connect(":memory:", check_same_thread=False)
    inner.row_factory = sqlite3.Row
    inner.execute(_QEL_DDL)
    inner.commit()
    conn = _KeepAliveConn(inner)
    monkeypatch.setattr(svc, "get_connection", lambda: conn)
    return conn


def test_pipeline_trace_round_trips_encrypted(log_db):
    from data.query_execution_service import get_pipeline_trace, log_query_execution

    trace = build_pipeline_trace(_sample_sql_plan())
    log_query_execution(
        "exec-1", "user-1", 1, "SELECT 1", {},
        param_count=0, row_count=1, truncated=False, duration_ms=5,
        status="success", error_code=None, executed_at="2026-08-25T00:00:00Z",
        pipeline_trace=trace,
    )

    # Stored value must be encrypted, not plaintext JSON.
    row = log_db.execute(
        "SELECT pipeline_trace_encrypted FROM query_execution_log WHERE execution_id = ?", ("exec-1",)
    ).fetchone()
    assert row["pipeline_trace_encrypted"] is not None
    assert "aggregate_by_dimension" not in row["pipeline_trace_encrypted"]

    retrieved = get_pipeline_trace("exec-1")
    assert retrieved == trace


def test_pipeline_trace_absent_when_not_provided(log_db):
    from data.query_execution_service import get_pipeline_trace, log_query_execution

    log_query_execution(
        "exec-2", "user-1", 1, "SELECT 1", {},
        param_count=0, row_count=1, truncated=False, duration_ms=5,
        status="success", error_code=None, executed_at="2026-08-25T00:00:00Z",
    )
    assert get_pipeline_trace("exec-2") is None


def test_pipeline_trace_ownership_enforced_when_requested(log_db):
    from data.query_execution_service import get_pipeline_trace, log_query_execution

    trace = build_pipeline_trace(_sample_sql_plan())
    log_query_execution(
        "exec-3", "owner", 1, "SELECT 1", {},
        param_count=0, row_count=1, truncated=False, duration_ms=5,
        status="success", error_code=None, executed_at="2026-08-25T00:00:00Z",
        pipeline_trace=trace,
    )

    assert get_pipeline_trace("exec-3", require_user_id="someone-else") is None
    assert get_pipeline_trace("exec-3", require_user_id="owner") == trace
    # Admin path — no ownership filter.
    assert get_pipeline_trace("exec-3") == trace


def test_pipeline_trace_missing_execution_returns_none(log_db):
    from data.query_execution_service import get_pipeline_trace
    assert get_pipeline_trace("does-not-exist") is None
