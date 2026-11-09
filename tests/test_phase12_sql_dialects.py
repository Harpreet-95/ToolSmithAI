"""
Phase 12 tests — SQL Dialect Abstraction (Phase 5.5).

Covers:
  - DialectAdapter public API via get_adapter() for all four dialects
  - generate_sql() with explicit dialect= keyword argument
  - MSSQL: TOP (n) prefix, bracket quoting, ? placeholder
  - SQLite: LIMIT n suffix, double-quote quoting, ? placeholder  (regression)
  - PostgreSQL: LIMIT n suffix, double-quote quoting, %s placeholder
  - MySQL: LIMIT n suffix, backtick quoting, %s placeholder
  - source_type_to_dialect() mapping
  - Dialect auto-detection fallback (monkeypatched DB)
  - Unknown dialect raises ValueError from get_adapter()

All generate_sql calls pass explicit dialect= to avoid real-DB lookups.
"""
import pytest

import data.db as db_module
from data.sql_dialects import (
    get_adapter,
    list_supported_dialects,
    source_type_to_dialect,
)
from data.sql_generation_service import detect_dialect, generate_sql


# ---------------------------------------------------------------------------
# Plan dict helpers — mirror test_phase11 helpers exactly
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


# Shared column / table fixtures that match DBO-schema style (common in MSSQL)
_DBO_SELECT = [
    {"table_fqn": "dbo.orders", "column_name": "amount",
     "alias": "total_amount", "aggregation": "SUM"},
]
_DBO_FROM = {"table_fqn": "dbo.orders", "alias": "ord"}

_DBO_JOIN = {
    "join_type":    "INNER",
    "left_table":   "dbo.orders",    "left_column":  "customer_id",
    "right_table":  "dbo.customers", "right_column": "id",
    "cardinality": "MANY_TO_ONE", "fanout_risk": "LOW", "confidence": 95,
}


# ===========================================================================
# ── DialectAdapter direct API ──────────────────────────────────────────────
# ===========================================================================

class TestGetAdapter:
    def test_known_dialects_return_adapter(self):
        for name in ("sqlite", "mssql", "postgresql", "mysql"):
            a = get_adapter(name)
            assert a.name == name

    def test_unknown_dialect_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown SQL dialect"):
            get_adapter("oracle")

    def test_list_supported_dialects(self):
        dialects = list_supported_dialects()
        assert set(dialects) == {"sqlite", "mssql", "postgresql", "mysql"}


class TestSqliteAdapter:
    def setup_method(self):
        self.a = get_adapter("sqlite")

    def test_quote_identifier_double_quotes(self):
        assert self.a.quote_identifier("orders") == '"orders"'
        assert self.a.quote_identifier("dbo") == '"dbo"'

    def test_quote_identifier_escapes_embedded_double_quotes(self):
        assert self.a.quote_identifier('col"name') == '"col""name"'

    def test_qualified_name(self):
        assert self.a.qualified_name("dbo", "orders") == '"dbo"."orders"'

    def test_qualified_name_single_part(self):
        assert self.a.qualified_name("orders") == '"orders"'

    def test_param_placeholder(self):
        assert self.a.param_placeholder() == "?"

    def test_placeholder_list(self):
        assert self.a.placeholder_list(3) == "?, ?, ?"

    def test_row_limit_suffix(self):
        assert self.a.row_limit_suffix(500) == "LIMIT 500"

    def test_row_limit_prefix_is_empty(self):
        assert self.a.row_limit_prefix(500) == ""


class TestMssqlAdapter:
    def setup_method(self):
        self.a = get_adapter("mssql")

    def test_quote_identifier_brackets(self):
        assert self.a.quote_identifier("orders") == "[orders]"
        assert self.a.quote_identifier("dbo") == "[dbo]"

    def test_quote_identifier_rejects_closing_bracket(self):
        with pytest.raises(ValueError, match="Unsafe SQL Server identifier"):
            self.a.quote_identifier("bad]col")

    def test_quote_identifier_rejects_null_byte(self):
        with pytest.raises(ValueError, match="Unsafe SQL Server identifier"):
            self.a.quote_identifier("col\x00name")

    def test_qualified_name(self):
        assert self.a.qualified_name("dbo", "orders") == "[dbo].[orders]"

    def test_param_placeholder(self):
        assert self.a.param_placeholder() == "?"

    def test_placeholder_list(self):
        assert self.a.placeholder_list(2) == "?, ?"

    def test_row_limit_prefix(self):
        assert self.a.row_limit_prefix(100) == "TOP (100) "

    def test_row_limit_suffix_is_empty(self):
        assert self.a.row_limit_suffix(100) == ""

    def test_date_literal_uses_convert(self):
        assert self.a.date_literal("2024-01-15") == "CONVERT(date, '2024-01-15')"


class TestPostgresqlAdapter:
    def setup_method(self):
        self.a = get_adapter("postgresql")

    def test_quote_identifier_double_quotes(self):
        assert self.a.quote_identifier("orders") == '"orders"'

    def test_param_placeholder(self):
        assert self.a.param_placeholder() == "%s"

    def test_placeholder_list(self):
        assert self.a.placeholder_list(2) == "%s, %s"

    def test_row_limit_suffix(self):
        assert self.a.row_limit_suffix(250) == "LIMIT 250"

    def test_row_limit_prefix_is_empty(self):
        assert self.a.row_limit_prefix(250) == ""


class TestMysqlAdapter:
    def setup_method(self):
        self.a = get_adapter("mysql")

    def test_quote_identifier_backticks(self):
        assert self.a.quote_identifier("orders") == "`orders`"

    def test_quote_identifier_escapes_backtick(self):
        assert self.a.quote_identifier("col`name") == "`col``name`"

    def test_qualified_name(self):
        assert self.a.qualified_name("mydb", "orders") == "`mydb`.`orders`"

    def test_param_placeholder(self):
        assert self.a.param_placeholder() == "%s"

    def test_row_limit_suffix(self):
        assert self.a.row_limit_suffix(100) == "LIMIT 100"

    def test_row_limit_prefix_is_empty(self):
        assert self.a.row_limit_prefix(100) == ""


# ===========================================================================
# ── source_type_to_dialect ─────────────────────────────────────────────────
# ===========================================================================

class TestSourceTypeToDialect:
    def test_mssql(self):
        assert source_type_to_dialect("mssql") == "mssql"

    def test_postgresql(self):
        assert source_type_to_dialect("postgresql") == "postgresql"

    def test_mysql(self):
        assert source_type_to_dialect("mysql") == "mysql"

    def test_unknown_falls_back_to_sqlite(self):
        assert source_type_to_dialect("oracle") == "sqlite"
        assert source_type_to_dialect("") == "sqlite"
        assert source_type_to_dialect("csv") == "sqlite"


# ===========================================================================
# ── generate_sql — MSSQL dialect ──────────────────────────────────────────
# ===========================================================================

class TestGenerateSqlMssql:
    def test_dialect_reported_as_mssql(self):
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM)
        result = generate_sql(1, "u1", plan, dialect="mssql")
        assert result["dialect"] == "mssql"

    def test_identifiers_use_bracket_quoting(self):
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM)
        sql = generate_sql(1, "u1", plan, dialect="mssql")["sql"]
        assert "[dbo].[orders].[amount]" in sql
        assert "[dbo].[orders]" in sql
        assert '"' not in sql

    def test_row_limit_uses_top_prefix(self):
        plan = _valid_plan(
            select=_DBO_SELECT, from_=_DBO_FROM,
            limits={"row_limit": 500},
        )
        sql = generate_sql(1, "u1", plan, dialect="mssql")["sql"]
        assert "TOP (500)" in sql
        assert "LIMIT" not in sql

    def test_row_limit_top_appears_after_select(self):
        plan = _valid_plan(
            select=_DBO_SELECT, from_=_DBO_FROM,
            limits={"row_limit": 200},
        )
        sql = generate_sql(1, "u1", plan, dialect="mssql")["sql"]
        # SELECT TOP (200) [dbo]...
        idx_select = sql.upper().index("SELECT")
        idx_top    = sql.upper().index("TOP")
        assert idx_top > idx_select  # TOP follows SELECT on same line

    def test_eq_filter_uses_question_mark_placeholder(self):
        where = [
            {"table_fqn": "dbo.orders", "column_name": "status",
             "operator": "=", "value": "shipped"},
        ]
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM, where=where)
        result = generate_sql(1, "u1", plan, dialect="mssql")
        assert "?" in result["sql"]
        assert result["parameters"]["placeholder"] == "?"
        assert result["parameters"]["values"] == ["shipped"]
        assert "shipped" not in result["sql"]

    def test_in_filter_uses_question_mark_placeholders(self):
        where = [
            {"table_fqn": "dbo.orders", "column_name": "region",
             "operator": "IN", "value": ["north", "south"]},
        ]
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM, where=where)
        result = generate_sql(1, "u1", plan, dialect="mssql")
        assert "IN (?, ?)" in result["sql"]
        assert result["parameters"]["values"] == ["north", "south"]

    def test_between_filter_uses_question_marks(self):
        where = [
            {"table_fqn": "dbo.orders", "column_name": "amount",
             "operator": "BETWEEN", "value": [100, 9999]},
        ]
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM, where=where)
        result = generate_sql(1, "u1", plan, dialect="mssql")
        assert "BETWEEN ? AND ?" in result["sql"]
        assert result["parameters"]["values"] == [100, 9999]

    def test_inner_join_with_bracket_quoting(self):
        select = [
            {"table_fqn": "dbo.orders",    "column_name": "amount",
             "alias": "amount", "aggregation": "SUM"},
            {"table_fqn": "dbo.customers", "column_name": "region",
             "alias": "region", "aggregation": None},
        ]
        group_by = [{"table_fqn": "dbo.customers", "column_name": "region"}]
        plan = _valid_plan(
            select=select, from_=_DBO_FROM,
            joins=[_DBO_JOIN], group_by=group_by,
        )
        result = generate_sql(1, "u1", plan, dialect="mssql")
        sql = result["sql"]
        assert "INNER JOIN [dbo].[customers]" in sql
        assert "[dbo].[orders].[customer_id]" in sql
        assert "[dbo].[customers].[id]" in sql

    def test_group_by_uses_bracket_quoting(self):
        select = [
            {"table_fqn": "dbo.orders", "column_name": "amount",
             "alias": "sum_amount", "aggregation": "SUM"},
            {"table_fqn": "dbo.orders", "column_name": "status",
             "alias": "status", "aggregation": None},
        ]
        group_by = [{"table_fqn": "dbo.orders", "column_name": "status"}]
        plan = _valid_plan(select=select, from_=_DBO_FROM, group_by=group_by)
        result = generate_sql(1, "u1", plan, dialect="mssql")
        sql = result["sql"]
        assert "GROUP BY" in sql
        assert "[dbo].[orders].[status]" in sql

    def test_sql_starts_with_select(self):
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM)
        sql = generate_sql(1, "u1", plan, dialect="mssql")["sql"]
        assert sql.strip().upper().startswith("SELECT")

    def test_safety_flags_are_set(self):
        plan = _valid_plan(select=_DBO_SELECT, from_=_DBO_FROM)
        result = generate_sql(1, "u1", plan, dialect="mssql")
        assert result["safety"]["read_only"] is True
        assert result["safety"]["validated"] is True
        assert result["safety"]["select_only"] is True
        assert result["safety"]["parameterized"] is True


# ===========================================================================
# ── generate_sql — SQLite dialect (regression with explicit dialect arg) ───
# ===========================================================================

class TestGenerateSqlSqliteExplicit:
    """Pass dialect='sqlite' explicitly — verifies the adapter path works for
    SQLite and that the explicit-dialect override is honoured."""

    _SELECT = [
        {"table_fqn": "main.orders", "column_name": "amount",
         "alias": "sum_amount", "aggregation": "SUM"},
    ]
    _FROM = {"table_fqn": "main.orders", "alias": "mai"}

    def test_dialect_reported_as_sqlite(self):
        plan = _valid_plan(select=self._SELECT, from_=self._FROM)
        result = generate_sql(1, "u1", plan, dialect="sqlite")
        assert result["dialect"] == "sqlite"

    def test_identifiers_use_double_quote_quoting(self):
        plan = _valid_plan(select=self._SELECT, from_=self._FROM)
        sql = generate_sql(1, "u1", plan, dialect="sqlite")["sql"]
        assert '"main"."orders"."amount"' in sql

    def test_row_limit_uses_limit_suffix(self):
        plan = _valid_plan(
            select=self._SELECT, from_=self._FROM,
            limits={"row_limit": 1000},
        )
        sql = generate_sql(1, "u1", plan, dialect="sqlite")["sql"]
        assert "LIMIT 1000" in sql
        assert "TOP" not in sql

    def test_placeholder_is_question_mark(self):
        where = [
            {"table_fqn": "main.orders", "column_name": "status",
             "operator": "=", "value": "active"},
        ]
        plan = _valid_plan(select=self._SELECT, from_=self._FROM, where=where)
        result = generate_sql(1, "u1", plan, dialect="sqlite")
        assert result["parameters"]["placeholder"] == "?"
        assert "?" in result["sql"]


# ===========================================================================
# ── generate_sql — PostgreSQL dialect ──────────────────────────────────────
# ===========================================================================

class TestGenerateSqlPostgresql:
    _SELECT = [
        {"table_fqn": "public.orders", "column_name": "amount",
         "alias": "total", "aggregation": "SUM"},
    ]
    _FROM = {"table_fqn": "public.orders", "alias": "o"}

    def test_dialect_reported_as_postgresql(self):
        plan = _valid_plan(select=self._SELECT, from_=self._FROM)
        result = generate_sql(1, "u1", plan, dialect="postgresql")
        assert result["dialect"] == "postgresql"

    def test_identifiers_use_double_quote_quoting(self):
        plan = _valid_plan(select=self._SELECT, from_=self._FROM)
        sql = generate_sql(1, "u1", plan, dialect="postgresql")["sql"]
        assert '"public"."orders"."amount"' in sql

    def test_row_limit_uses_limit_suffix(self):
        plan = _valid_plan(
            select=self._SELECT, from_=self._FROM,
            limits={"row_limit": 250},
        )
        sql = generate_sql(1, "u1", plan, dialect="postgresql")["sql"]
        assert "LIMIT 250" in sql
        assert "TOP" not in sql

    def test_placeholder_is_percent_s(self):
        where = [
            {"table_fqn": "public.orders", "column_name": "status",
             "operator": "=", "value": "active"},
        ]
        plan = _valid_plan(select=self._SELECT, from_=self._FROM, where=where)
        result = generate_sql(1, "u1", plan, dialect="postgresql")
        assert result["parameters"]["placeholder"] == "%s"
        assert "%s" in result["sql"]
        assert "active" not in result["sql"]

    def test_in_filter_uses_percent_s_placeholders(self):
        where = [
            {"table_fqn": "public.orders", "column_name": "status",
             "operator": "IN", "value": ["a", "b"]},
        ]
        plan = _valid_plan(select=self._SELECT, from_=self._FROM, where=where)
        sql = generate_sql(1, "u1", plan, dialect="postgresql")["sql"]
        assert "IN (%s, %s)" in sql


# ===========================================================================
# ── generate_sql — MySQL dialect ───────────────────────────────────────────
# ===========================================================================

class TestGenerateSqlMysql:
    _SELECT = [
        {"table_fqn": "shop.orders", "column_name": "amount",
         "alias": "total", "aggregation": "SUM"},
    ]
    _FROM = {"table_fqn": "shop.orders", "alias": "o"}

    def test_dialect_reported_as_mysql(self):
        plan = _valid_plan(select=self._SELECT, from_=self._FROM)
        result = generate_sql(1, "u1", plan, dialect="mysql")
        assert result["dialect"] == "mysql"

    def test_identifiers_use_backtick_quoting(self):
        plan = _valid_plan(select=self._SELECT, from_=self._FROM)
        sql = generate_sql(1, "u1", plan, dialect="mysql")["sql"]
        assert "`shop`.`orders`.`amount`" in sql
        assert '"' not in sql

    def test_row_limit_uses_limit_suffix(self):
        plan = _valid_plan(
            select=self._SELECT, from_=self._FROM,
            limits={"row_limit": 100},
        )
        sql = generate_sql(1, "u1", plan, dialect="mysql")["sql"]
        assert "LIMIT 100" in sql
        assert "TOP" not in sql

    def test_placeholder_is_percent_s(self):
        where = [
            {"table_fqn": "shop.orders", "column_name": "status",
             "operator": "=", "value": "pending"},
        ]
        plan = _valid_plan(select=self._SELECT, from_=self._FROM, where=where)
        result = generate_sql(1, "u1", plan, dialect="mysql")
        assert result["parameters"]["placeholder"] == "%s"


# ===========================================================================
# ── Dialect auto-detection via _detect_dialect fallback ────────────────────
# ===========================================================================

class TestDialectAutoDetection:
    """Verify detect_dialect() returns the right dialect and falls back safely.

    generate_sql defaults to 'sqlite' to keep unit tests stable.
    detect_dialect() is what Phase 6's execution service calls before invoking
    generate_sql so that the correct dialect reaches the SQL builder.
    """

    def test_detect_dialect_fallback_to_sqlite_when_db_unavailable(self, monkeypatch):
        def _raise(*_, **__):
            raise RuntimeError("DB unavailable in test")
        monkeypatch.setattr(db_module, "get_connection", _raise)

        assert detect_dialect(99) == "sqlite"

    def test_detect_dialect_falls_back_when_source_not_found(self, monkeypatch, tmp_path):
        import sqlite3
        db_path = tmp_path / "empty.db"
        test_conn = sqlite3.connect(str(db_path))
        test_conn.execute(
            "CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, source_type TEXT)"
        )
        test_conn.commit()
        test_conn.close()
        monkeypatch.setattr(db_module, "DB_PATH", db_path)

        assert detect_dialect(999) == "sqlite"  # no such id

    def test_detect_dialect_returns_mssql_for_mssql_source(self, monkeypatch, tmp_path):
        import sqlite3
        db_path = tmp_path / "detect_test.db"
        test_conn = sqlite3.connect(str(db_path))
        test_conn.execute(
            "CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, source_type TEXT)"
        )
        test_conn.execute("INSERT INTO data_source_connections VALUES (42, 'mssql')")
        test_conn.commit()
        test_conn.close()
        monkeypatch.setattr(db_module, "DB_PATH", db_path)

        assert detect_dialect(42) == "mssql"

    def test_generate_sql_with_detected_dialect_produces_mssql_sql(self, monkeypatch, tmp_path):
        """End-to-end: detect_dialect → generate_sql(dialect=...) → MSSQL SQL."""
        import sqlite3
        db_path = tmp_path / "e2e.db"
        test_conn = sqlite3.connect(str(db_path))
        test_conn.execute(
            "CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, source_type TEXT)"
        )
        test_conn.execute("INSERT INTO data_source_connections VALUES (7, 'mssql')")
        test_conn.commit()
        test_conn.close()
        monkeypatch.setattr(db_module, "DB_PATH", db_path)

        select = [
            {"table_fqn": "dbo.sales", "column_name": "revenue",
             "alias": "total", "aggregation": "SUM"},
        ]
        from_ = {"table_fqn": "dbo.sales", "alias": "s"}
        plan = _valid_plan(select=select, from_=from_, limits={"row_limit": 100})

        d = detect_dialect(7)
        result = generate_sql(7, "u1", plan, dialect=d)

        assert result["dialect"] == "mssql"
        assert "[dbo].[sales].[revenue]" in result["sql"]
        assert "TOP (100)" in result["sql"]
        assert "LIMIT" not in result["sql"]
