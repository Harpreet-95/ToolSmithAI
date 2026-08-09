"""
SQL Dialect Abstraction — Program 3 Phase 5.5

One DialectAdapter per supported database engine.  All SQL-string rendering in
sql_generation_service routes through these adapters so no dialect-specific
string (quotes, placeholders, row-limit syntax) is ever hardcoded there.

Supported dialects
------------------
sqlite      — SQLite 3.x       (trailing LIMIT, "double-quoted" identifiers, ? placeholders)
mssql       — SQL Server        (SELECT TOP (n), [bracket] identifiers,       ? placeholders)
postgresql  — PostgreSQL        (trailing LIMIT, "double-quoted" identifiers, %s placeholders)
mysql       — MySQL / MariaDB   (trailing LIMIT, `backtick` identifiers,      %s placeholders)

Adapters are stateless singletons — never store per-request state here.
"""

from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# Source-type → dialect mapping
# ---------------------------------------------------------------------------

_SOURCE_TYPE_TO_DIALECT: dict[str, str] = {
    "mssql":      "mssql",
    "postgresql": "postgresql",
    "mysql":      "mysql",
}


def source_type_to_dialect(source_type: str) -> str:
    """Map a connector source_type string to a SQL dialect name.

    Unknown source types fall back to 'sqlite' so callers never receive an
    invalid dialect from this function.
    """
    return _SOURCE_TYPE_TO_DIALECT.get(source_type, "sqlite")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class DialectAdapter(ABC):
    """Contract for SQL dialect-specific rendering.

    Concrete subclasses implement the four abstract methods; the remaining
    methods have safe defaults that subclasses may override.
    """

    name: str  # dialect key returned in API responses and used for adapter lookup

    # ── Identifiers ──────────────────────────────────────────────────────────

    @abstractmethod
    def quote_identifier(self, identifier: str) -> str:
        """Wrap one identifier (schema, table, or column name) in dialect-correct quotes."""
        ...

    def qualified_name(self, *parts: str) -> str:
        """Return a dot-joined fully-qualified name with each segment independently quoted.

        Empty strings in *parts are silently skipped so callers need not pre-filter.
        Examples (SQLite):  qualified_name("dbo", "orders") → '"dbo"."orders"'
        Examples (MSSQL):   qualified_name("dbo", "orders") → '[dbo].[orders]'
        """
        return ".".join(self.quote_identifier(p) for p in parts if p)

    # ── Parameter placeholders ────────────────────────────────────────────────

    @abstractmethod
    def param_placeholder(self) -> str:
        """Return the parameter binding token for a single value.

        Examples: '?' (pyodbc / sqlite3), '%s' (psycopg2 / mysqlclient).
        Note: positional-indexed styles (e.g. asyncpg's '$1') require index
        tracking in the WHERE-clause builder — a future concern for asyncpg.
        """
        ...

    def placeholder_list(self, count: int) -> str:
        """Return comma-separated placeholders for an IN-clause value list."""
        return ", ".join(self.param_placeholder() for _ in range(count))

    # ── Row limiting ─────────────────────────────────────────────────────────

    @abstractmethod
    def row_limit_prefix(self, n: int) -> str:
        """Return text inserted between SELECT and the column list to cap rows.

        Non-empty only for SQL Server: 'TOP (n) '.
        All other dialects return ''.
        """
        ...

    @abstractmethod
    def row_limit_suffix(self, n: int) -> str:
        """Return a trailing clause appended after the full query body to cap rows.

        Non-empty for SQLite / PostgreSQL / MySQL: 'LIMIT n'.
        Empty for SQL Server (which uses the TOP prefix instead).
        """
        ...

    # ── Scalar literals ───────────────────────────────────────────────────────

    def bool_true(self) -> str:
        """SQL literal for boolean TRUE."""
        return "1"

    def bool_false(self) -> str:
        """SQL literal for boolean FALSE."""
        return "0"

    def null_literal(self) -> str:
        return "NULL"

    def date_literal(self, iso_date: str) -> str:
        """Format an ISO-8601 date string as a SQL date literal."""
        return f"'{iso_date}'"

    # ── Calendar-grain extraction ───────────────────────────────────────────

    def date_part_expr(self, grain: str, col_ref: str) -> str:
        """Wrap an already-quoted column reference to extract a calendar
        grain ('year'|'month'|'quarter'|'week'|'day') for GROUP BY/SELECT
        (e.g. grain='year' -> 'YEAR(col)'). Base default is a no-op — never
        invents a wrapper for a dialect/grain it doesn't explicitly support;
        concrete subclasses override with their own real syntax."""
        return col_ref


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

class _SqliteDialect(DialectAdapter):
    name = "sqlite"

    def quote_identifier(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def param_placeholder(self) -> str:
        return "?"

    def row_limit_prefix(self, n: int) -> str:
        return ""

    def row_limit_suffix(self, n: int) -> str:
        return f"LIMIT {n}"

    def date_part_expr(self, grain: str, col_ref: str) -> str:
        fmt = {"year": "%Y", "month": "%m", "day": "%d"}.get(grain)
        if fmt:
            return f"CAST(strftime('{fmt}', {col_ref}) AS INTEGER)"
        if grain == "quarter":
            return f"((CAST(strftime('%m', {col_ref}) AS INTEGER) - 1) / 3) + 1"
        if grain == "week":
            return f"CAST(strftime('%W', {col_ref}) AS INTEGER)"
        return col_ref


# ---------------------------------------------------------------------------
# SQL Server (MSSQL)
# ---------------------------------------------------------------------------

class _MssqlDialect(DialectAdapter):
    name = "mssql"

    def quote_identifier(self, identifier: str) -> str:
        if "]" in identifier or "\x00" in identifier:
            raise ValueError(
                f"Unsafe SQL Server identifier (contains ] or null byte): {identifier!r}"
            )
        return f"[{identifier}]"

    def param_placeholder(self) -> str:
        return "?"  # pyodbc positional-? style

    def row_limit_prefix(self, n: int) -> str:
        return f"TOP ({n}) "

    def row_limit_suffix(self, n: int) -> str:
        return ""  # MSSQL uses the SELECT TOP prefix, not a trailing clause

    def date_literal(self, iso_date: str) -> str:
        return f"CONVERT(date, '{iso_date}')"

    def date_part_expr(self, grain: str, col_ref: str) -> str:
        func = {"year": "YEAR", "month": "MONTH", "day": "DAY"}.get(grain)
        if func:
            return f"{func}({col_ref})"
        if grain in ("quarter", "week"):
            return f"DATEPART({grain}, {col_ref})"
        return col_ref


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

class _PostgreSQLDialect(DialectAdapter):
    name = "postgresql"

    def quote_identifier(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def param_placeholder(self) -> str:
        return "%s"  # psycopg2 / psycopg3 style

    def row_limit_prefix(self, n: int) -> str:
        return ""

    def row_limit_suffix(self, n: int) -> str:
        return f"LIMIT {n}"

    def date_part_expr(self, grain: str, col_ref: str) -> str:
        if grain in ("year", "month", "day", "quarter", "week"):
            return f"EXTRACT({grain.upper()} FROM {col_ref})::integer"
        return col_ref


# ---------------------------------------------------------------------------
# MySQL / MariaDB
# ---------------------------------------------------------------------------

class _MySQLDialect(DialectAdapter):
    name = "mysql"

    def quote_identifier(self, identifier: str) -> str:
        return "`" + identifier.replace("`", "``") + "`"

    def param_placeholder(self) -> str:
        return "%s"  # mysqlclient / mysql-connector-python style

    def row_limit_prefix(self, n: int) -> str:
        return ""

    def row_limit_suffix(self, n: int) -> str:
        return f"LIMIT {n}"

    def date_part_expr(self, grain: str, col_ref: str) -> str:
        func = {"year": "YEAR", "month": "MONTH", "day": "DAY", "quarter": "QUARTER", "week": "WEEK"}.get(grain)
        if func:
            return f"{func}({col_ref})"
        return col_ref


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, DialectAdapter] = {
    "sqlite":     _SqliteDialect(),
    "mssql":      _MssqlDialect(),
    "postgresql": _PostgreSQLDialect(),
    "mysql":      _MySQLDialect(),
}


def get_adapter(dialect: str) -> DialectAdapter:
    """Return the DialectAdapter for the named dialect.

    Raises ValueError for unknown dialect names so callers detect mis-spellings
    early rather than silently falling back to incorrect behaviour.
    """
    adapter = _ADAPTERS.get(dialect)
    if adapter is None:
        raise ValueError(
            f"Unknown SQL dialect: {dialect!r}. "
            f"Supported: {sorted(_ADAPTERS)}"
        )
    return adapter


def list_supported_dialects() -> list[str]:
    """Return the sorted list of dialect names accepted by get_adapter()."""
    return sorted(_ADAPTERS)
