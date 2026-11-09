"""
Investigation Agent Hardening milestone, Part 11 — MSSQL DISTINCT
SQL-generation defect.

Live-confirmed root cause (a real production database, "Distinct students"/"Unique
recruiters"): data.sql_generation_service.generate_sql assembled
"SELECT TOP (n) DISTINCT col ..." — real T-SQL requires
"SELECT DISTINCT TOP (n) col ...", so SQL Server rejected the generated
statement with "Incorrect syntax near the keyword 'DISTINCT'" (156).

Every other dialect this module targets (sqlite/postgresql/mysql) caps rows
via a trailing LIMIT clause (row_limit_prefix() == "" always), so this
defect was invisible everywhere except MSSQL — these tests specifically
target dialect="mssql" to catch it, plus a sqlite pass to prove the fix is
a no-op there (never regresses the far more heavily-tested default dialect).
"""
from __future__ import annotations

from data.sql_generation_service import generate_sql

_VALID = {"valid": True, "blocking_reasons": [], "checks": {}}


def _plan(**overrides) -> dict:
    base = {
        "select": [{"table_fqn": "dbo.Students", "column_name": "ProgramID", "aggregation": None}],
        "from": {"table_fqn": "dbo.Students"},
        "joins": [], "where": [], "group_by": [], "order_by": [],
        "distinct": True,
        "limits": {"row_limit": 1000},
        "validation": _VALID, "warnings": [],
    }
    base.update(overrides)
    return base


def test_distinct_single_column_valid_mssql_syntax():
    result = generate_sql(1, "u1", _plan(), dialect="mssql")
    assert result["sql"].startswith("SELECT DISTINCT TOP (1000)"), result["sql"]
    assert "TOP (1000) DISTINCT" not in result["sql"]


def test_distinct_business_label_column_valid_mssql_syntax():
    plan = _plan(select=[{"table_fqn": "dbo.Recruiters", "column_name": "RecruiterName", "aggregation": None}])
    result = generate_sql(1, "u1", plan, dialect="mssql")
    assert result["sql"].startswith("SELECT DISTINCT TOP (1000)"), result["sql"]


def test_distinct_with_filter_valid_mssql_syntax():
    plan = _plan(where=[{"table_fqn": "dbo.Students", "column_name": "Status", "operator": "=", "value": "Active"}])
    result = generate_sql(1, "u1", plan, dialect="mssql")
    assert result["sql"].startswith("SELECT DISTINCT TOP (1000)")
    assert "WHERE" in result["sql"]
    assert result["parameters"]["values"] == ["Active"]


def test_distinct_across_join_valid_mssql_syntax():
    plan = _plan(
        select=[{"table_fqn": "dbo.Recruiters", "column_name": "RecruiterName", "aggregation": None}],
        joins=[{
            "left_table": "dbo.Students", "left_column": "RecruiterID",
            "right_table": "dbo.Recruiters", "right_column": "RecruiterID", "join_type": "INNER",
        }],
    )
    result = generate_sql(1, "u1", plan, dialect="mssql")
    assert result["sql"].startswith("SELECT DISTINCT TOP (1000)"), result["sql"]
    assert "JOIN" in result["sql"]


def test_count_distinct_aggregate_unaffected_by_plan_level_fix():
    # A NAMED aggregate DISTINCT (COUNT(DISTINCT col)) is a completely
    # separate code path (_build_select_clause's own per-row aggregation
    # handling) from the plan-level `distinct` flag this fix touches —
    # confirms the two never interact incorrectly.
    plan = _plan(
        select=[{"table_fqn": "dbo.Students", "column_name": "ProgramID", "aggregation": "COUNT", "distinct": True}],
        distinct=False,
    )
    result = generate_sql(1, "u1", plan, dialect="mssql")
    assert "COUNT(DISTINCT" in result["sql"]
    assert result["sql"].startswith("SELECT TOP (1000)")  # no plan-level DISTINCT prefix here


def test_distinct_with_order_by_valid_mssql_syntax():
    plan = _plan(order_by=[{"table_fqn": "dbo.Students", "column_name": "ProgramID", "direction": "ASC"}])
    result = generate_sql(1, "u1", plan, dialect="mssql")
    assert result["sql"].startswith("SELECT DISTINCT TOP (1000)")
    assert "ORDER BY" in result["sql"]


def test_distinct_sqlite_dialect_unaffected_no_op():
    # sqlite's row_limit_prefix() is always "" — proves the reorder is a
    # true no-op for the default/most heavily-tested dialect.
    result = generate_sql(1, "u1", _plan(), dialect="sqlite")
    assert result["sql"].startswith("SELECT DISTINCT "), result["sql"]
    assert "LIMIT 1000" in result["sql"]
