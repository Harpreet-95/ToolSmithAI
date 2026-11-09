"""
Tests for Day 4, Capability 2 — Business Insights:
data.insight_service.compute_period_comparison_insight.

Unit-level: exercises eligibility rules, previous-period date math, and
percent-change computation directly, monkeypatching
generate_sql/execute_governed_query rather than standing up a full DB
fixture (those two functions are already covered by their own test
suites; this module only orchestrates a second call to them).

Run from the project root:
    python -m pytest tests/test_insight_service.py -v
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-insight-service-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-insight-service-salt-long-enough-12345")

from data.insight_service import _previous_period, compute_period_comparison_insight

_BUSINESS_PLAN = {
    "aggregation": "COUNT",
    "group_by": [],
    "date_context": {"label": "last quarter", "start": "2026-04-01", "end": "2026-06-30"},
}

_SQL_PLAN = {
    "where": [
        {"table_fqn": "dbo.vw_Clients", "column_name": "InsertDate", "operator": "BETWEEN",
         "value": ["2026-04-01", "2026-06-30"]},
    ],
}


def _fake_generated(sql="SELECT COUNT(*) FROM t WHERE d BETWEEN ? AND ?"):
    return {"sql": sql, "parameters": {"values": ["2026-01-01", "2026-03-31"]}}


class _FakeQueryResult:
    def __init__(self, status="success", rows=None):
        self._status = status
        self._rows = rows if rows is not None else [{"count_x": 62000}]

    def to_dict(self):
        return {"status": self._status, "rows": self._rows}


def test_previous_period_shifts_by_equal_length():
    # A 91-day window (Apr 1 - Jun 30) shifted back by its own length lands
    # on Dec 31 - Mar 31 — it does not snap to calendar-quarter boundaries
    # (Q1 is only 90 days), which is exactly why the insight label stays a
    # generic "vs. the previous period" rather than naming a specific quarter.
    assert _previous_period("2026-04-01", "2026-06-30") == ("2025-12-31", "2026-03-31")


def test_previous_period_single_day():
    assert _previous_period("2026-06-30", "2026-06-30") == ("2026-06-29", "2026-06-29")


def test_previous_period_returns_none_for_inverted_range():
    assert _previous_period("2026-06-30", "2026-01-01") is None


def test_previous_period_returns_none_for_unparseable_date():
    assert _previous_period("not-a-date", "2026-06-30") is None


def test_skips_when_aggregation_not_eligible(monkeypatch):
    plan = {**_BUSINESS_PLAN, "aggregation": "MAX"}
    assert compute_period_comparison_insight(1, "u1", plan, _SQL_PLAN, 100) is None


def test_skips_when_grouped(monkeypatch):
    plan = {**_BUSINESS_PLAN, "group_by": [{"column_name": "region"}]}
    assert compute_period_comparison_insight(1, "u1", plan, _SQL_PLAN, 100) is None


def test_skips_when_no_date_context(monkeypatch):
    plan = {**_BUSINESS_PLAN, "date_context": None}
    assert compute_period_comparison_insight(1, "u1", plan, _SQL_PLAN, 100) is None


def test_skips_when_current_value_is_none(monkeypatch):
    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, None) is None


def test_skips_when_no_matching_date_where_row(monkeypatch):
    sql_plan = {"where": []}
    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, sql_plan, 100) is None


def test_computes_percent_change_up(monkeypatch):
    import data.query_execution_service as qes
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: _fake_generated())
    monkeypatch.setattr(qes, "execute_governed_query", lambda *a, **k: (_FakeQueryResult(rows=[{"c": 50000}]), []))

    insight = compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 70772)
    assert insight is not None
    assert insight["type"] == "period_comparison"
    assert insight["current_value"] == 70772
    assert insight["previous_value"] == 50000
    assert insight["direction"] == "up"
    assert insight["percent_change"] == round((70772 - 50000) / 50000 * 100, 1)


def test_computes_percent_change_down(monkeypatch):
    import data.query_execution_service as qes
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: _fake_generated())
    monkeypatch.setattr(qes, "execute_governed_query", lambda *a, **k: (_FakeQueryResult(rows=[{"c": 100}]), []))

    insight = compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 60)
    assert insight["direction"] == "down"
    assert insight["percent_change"] == -40.0


def test_returns_none_when_comparison_generation_refused(monkeypatch):
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: {"sql": None, "parameters": {"values": []}})

    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 100) is None


def test_returns_none_when_comparison_execution_fails(monkeypatch):
    import data.query_execution_service as qes
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: _fake_generated())
    monkeypatch.setattr(qes, "execute_governed_query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 100) is None


def test_returns_none_when_comparison_status_not_success(monkeypatch):
    import data.query_execution_service as qes
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: _fake_generated())
    monkeypatch.setattr(qes, "execute_governed_query", lambda *a, **k: (_FakeQueryResult(status="failed"), []))

    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 100) is None


def test_returns_none_when_comparison_has_no_rows(monkeypatch):
    import data.query_execution_service as qes
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: _fake_generated())
    monkeypatch.setattr(qes, "execute_governed_query", lambda *a, **k: (_FakeQueryResult(rows=[]), []))

    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 100) is None


def test_returns_none_when_previous_value_is_zero(monkeypatch):
    import data.query_execution_service as qes
    import data.sql_generation_service as sgs

    monkeypatch.setattr(sgs, "detect_dialect", lambda source_id: "mssql")
    monkeypatch.setattr(sgs, "generate_sql", lambda *a, **k: _fake_generated())
    monkeypatch.setattr(qes, "execute_governed_query", lambda *a, **k: (_FakeQueryResult(rows=[{"c": 0}]), []))

    assert compute_period_comparison_insight(1, "u1", _BUSINESS_PLAN, _SQL_PLAN, 100) is None
