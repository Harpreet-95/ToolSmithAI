"""
Phase 11 tests — Safe SQL Generation Engine.

Tests generate_sql() in isolation using hand-built sql_plan dicts (matching
Phase 4's real output shape). generate_sql() accepts a sql_plan dict and
produces a parameterized SQL string — it makes no DB reads, so isolation tests
need no temp-DB fixture. A final test proves that by patching get_connection
to raise and confirming the function still succeeds.
"""
import pathlib
import pytest

import data.db as db_module
from data import models
from data.sql_generation_service import generate_sql


# ---------------------------------------------------------------------------
# DB fixture (for future integration tests only — generation itself is DB-free)
# ---------------------------------------------------------------------------

def _make_db(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    monkeypatch.setattr("data.models.DB_PATH", db_path)
    for svc in (
        "data.query_planning_service",
        "data.sql_planning_service",
        "data.sql_generation_service",
        "data.business_knowledge_service",
        "data.knowledge_graph_service",
        "data.semantic_layer_service",
        "data.relationship_service",
        "data.lineage_service",
        "data.profiling_service",
        "data.domain_service",
        "data.entity_service",
        "data.dictionary_service",
    ):
        try:
            monkeypatch.setattr(f"{svc}.get_connection", db_module.get_connection)
        except AttributeError:
            pass
    models.init_db()
    return db_path


# ---------------------------------------------------------------------------
# Plan dict builders
# ---------------------------------------------------------------------------

def _valid_plan(
    *,
    select=None,
    from_=None,
    joins=None,
    where=None,
    group_by=None,
    limits=None,
    warnings=None,
) -> dict:
    return {
        "select":    select or [],
        "from":      from_,
        "joins":     joins or [],
        "where":     where or [],
        "group_by":  group_by or [],
        "order_by":  [],
        "limits":    limits if limits is not None else {"row_limit": 1000},
        "warnings":  warnings or [],
        "validation": {
            "valid": True, "read_only": True,
            "checks": {}, "blocking_reasons": [],
        },
        "explanation": [],
    }


def _invalid_plan(reason: str = "Plan is invalid.") -> dict:
    return {
        "select": [], "from": None, "joins": [], "where": [],
        "group_by": [], "order_by": [], "limits": {},
        "warnings": [],
        "validation": {
            "valid": False, "read_only": True,
            "checks": {}, "blocking_reasons": [reason],
        },
        "explanation": [],
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ORDERS_SELECT = [
    {"table_fqn": "main.orders", "column_name": "amount",
     "alias": "sum_amount", "aggregation": "SUM"},
]
_ORDERS_FROM = {"table_fqn": "main.orders", "alias": "mai"}

_JOIN_STEP = {
    "join_type": "INNER",
    "left_table":   "main.orders",  "left_column":  "customer_id",
    "right_table":  "main.customers", "right_column": "id",
    "cardinality": "MANY_TO_ONE", "fanout_risk": "LOW", "confidence": 90,
}


# ===========================================================================
# 1 — Single-table SELECT
# ===========================================================================

def test_single_table_select():
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert sql is not None
    assert sql.strip().upper().startswith("SELECT")
    assert '"main"."orders"."amount"' in sql
    assert "SUM(" in sql
    assert "FROM" in sql
    assert '"main"."orders"' in sql
    assert result["dialect"] == "sqlite"
    assert result["safety"]["read_only"] is True
    assert result["safety"]["validated"] is True


# ===========================================================================
# 2 — Aggregated measure + dimension with GROUP BY
# ===========================================================================

def test_aggregate_measure_with_group_by():
    select = [
        {"table_fqn": "main.orders", "column_name": "amount",
         "alias": "sum_amount", "aggregation": "SUM"},
        {"table_fqn": "main.orders", "column_name": "status",
         "alias": "status", "aggregation": None},
    ]
    group_by = [{"table_fqn": "main.orders", "column_name": "status"}]
    plan = _valid_plan(select=select, from_=_ORDERS_FROM, group_by=group_by)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert "SUM(" in sql
    assert "GROUP BY" in sql
    assert '"main"."orders"."status"' in sql
    assert result["safety"]["validated"] is True


# ===========================================================================
# 3 — Multi-table INNER JOIN
# ===========================================================================

def test_multi_table_inner_join():
    select = [
        {"table_fqn": "main.orders",    "column_name": "amount",
         "alias": "sum_amount", "aggregation": "SUM"},
        {"table_fqn": "main.customers", "column_name": "region",
         "alias": "region", "aggregation": None},
    ]
    group_by = [{"table_fqn": "main.customers", "column_name": "region"}]
    plan = _valid_plan(
        select=select, from_=_ORDERS_FROM,
        joins=[_JOIN_STEP], group_by=group_by,
    )
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert "INNER JOIN" in sql
    assert '"main"."customers"' in sql
    assert '"main"."orders"."customer_id"' in sql
    assert '"main"."customers"."id"' in sql


# ===========================================================================
# 4 — Parameterized equality filter
# ===========================================================================

def test_parameterized_eq_filter():
    where = [
        {"table_fqn": "main.orders", "column_name": "status",
         "operator": "=", "value": "active"},
    ]
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM, where=where)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert "WHERE" in sql
    assert "?" in sql
    assert result["parameters"]["values"] == ["active"]
    assert result["parameters"]["count"] == 1
    assert "active" not in sql  # value must never appear literally


# ===========================================================================
# 5 — IN operator parameterized
# ===========================================================================

def test_parameterized_in_filter():
    where = [
        {"table_fqn": "main.orders", "column_name": "status",
         "operator": "IN", "value": ["open", "pending"]},
    ]
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM, where=where)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert "IN (?, ?)" in sql
    assert result["parameters"]["values"] == ["open", "pending"]
    assert "open" not in sql
    assert "pending" not in sql


# ===========================================================================
# 6 — BETWEEN operator parameterized
# ===========================================================================

def test_parameterized_between_filter():
    where = [
        {"table_fqn": "main.orders", "column_name": "amount",
         "operator": "BETWEEN", "value": [100, 500]},
    ]
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM, where=where)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert "BETWEEN ? AND ?" in sql
    assert result["parameters"]["values"] == [100, 500]
    # Values are in parameters, not inlined — the WHERE clause itself has no literals
    assert "WHERE" in sql and "?" in sql


# ===========================================================================
# 7 — Invalid plan (valid=False) is rejected
# ===========================================================================

def test_invalid_plan_rejected():
    plan = _invalid_plan("Unresolved term(s) cannot be planned: revenue.")
    result = generate_sql(1, "user1", plan)

    assert result["sql"] is None
    assert result["safety"]["validated"] is False
    assert any("refused" in e.lower() for e in result["explanation"])
    assert result["parameters"]["count"] == 0


# ===========================================================================
# 8 — Empty plan dict is rejected gracefully
# ===========================================================================

def test_empty_plan_rejected():
    result = generate_sql(1, "user1", {})

    assert result["sql"] is None
    assert result["safety"]["validated"] is False
    assert result["parameters"]["values"] == []


# ===========================================================================
# 9 — Generated SQL always starts with SELECT
# ===========================================================================

def test_sql_always_starts_with_select():
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM)
    result = generate_sql(1, "user1", plan)

    assert result["sql"] is not None
    assert result["sql"].strip().upper().startswith("SELECT")


# ===========================================================================
# 10 — SQL is read-only: no write keywords in generated string
# ===========================================================================

def test_sql_is_read_only():
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"].upper()
    for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE"):
        assert kw not in sql, f"SQL contains write keyword: {kw}"

    assert result["safety"]["read_only"] is True
    assert result["safety"]["select_only"] is True


# ===========================================================================
# 11 — Row limit appears in output when plan is valid
# ===========================================================================

def test_row_limit_applied():
    plan = _valid_plan(
        select=_ORDERS_SELECT, from_=_ORDERS_FROM,
        limits={"row_limit": 1000},
    )
    result = generate_sql(1, "user1", plan)

    assert "LIMIT 1000" in result["sql"]


# ===========================================================================
# 12 — Warnings from sql_plan pass through unchanged
# ===========================================================================

def test_warnings_passed_through():
    w = [{"type": "pii_involved", "severity": "MEDIUM", "message": "col may contain PII."}]
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM, warnings=w)
    result = generate_sql(1, "user1", plan)

    assert result["warnings"] == w


# ===========================================================================
# 13 — Identifier quoting: "schema"."table"."column" form
# ===========================================================================

def test_identifier_quoting():
    select = [
        {"table_fqn": "dbo.orders", "column_name": "total",
         "alias": "total", "aggregation": None},
    ]
    from_ = {"table_fqn": "dbo.orders", "alias": "ord"}
    plan = _valid_plan(select=select, from_=from_)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert '"dbo"."orders"."total"' in sql
    assert '"dbo"."orders"' in sql
    # Unquoted form must not appear
    assert "dbo.orders.total" not in sql


# ===========================================================================
# 14 — generate_sql makes no DB calls (patching get_connection to raise proves it)
# ===========================================================================

def test_no_db_calls(monkeypatch):
    def _raise(*_, **__):
        raise AssertionError("generate_sql must not open a DB connection")

    monkeypatch.setattr(db_module, "get_connection", _raise)

    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM)
    result = generate_sql(1, "user1", plan)  # must not raise
    assert result["sql"] is not None


# ===========================================================================
# 15 — Filter values are parameterized: literal value must not appear in SQL
# ===========================================================================

def test_filter_value_not_inlined_in_sql():
    where = [
        {"table_fqn": "main.orders", "column_name": "amount",
         "operator": ">", "value": 999999},
    ]
    plan = _valid_plan(select=_ORDERS_SELECT, from_=_ORDERS_FROM, where=where)
    result = generate_sql(1, "user1", plan)

    sql = result["sql"]
    assert "999999" not in sql
    assert "?" in sql
    assert 999999 in result["parameters"]["values"]


# ===========================================================================
# 16 — None join_type defaults to INNER JOIN
# ===========================================================================

def test_null_join_type_defaults_to_inner():
    join_step = dict(_JOIN_STEP, join_type=None)
    select = [
        {"table_fqn": "main.orders",    "column_name": "amount",
         "alias": "amount", "aggregation": None},
        {"table_fqn": "main.customers", "column_name": "name",
         "alias": "name",   "aggregation": None},
    ]
    plan = _valid_plan(select=select, from_=_ORDERS_FROM, joins=[join_step])
    result = generate_sql(1, "user1", plan)

    assert "INNER JOIN" in result["sql"]
