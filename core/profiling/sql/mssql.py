"""
SQL Server profiling query builders.

Returns complete, parameterised-safe SQL strings.
No queries are executed here.  No pyodbc import.
Every table/column identifier is bracket-quoted to prevent injection.
"""

import re

# Reject identifiers that would escape the bracket-quote or corrupt the query.
_UNSAFE_RE = re.compile(r'[\]\x00]')


def _validate(identifier: str) -> str:
    """Raise ValueError if the identifier is unsafe to bracket-quote."""
    if not identifier:
        raise ValueError("SQL Server identifier cannot be empty.")
    if _UNSAFE_RE.search(identifier):
        raise ValueError(
            f"Unsafe SQL Server identifier (contains ] or null byte): {identifier!r}"
        )
    return identifier


def _q(identifier: str) -> str:
    """Wrap one SQL Server identifier in [brackets]."""
    return f"[{_validate(identifier)}]"


def _fqn(table_fqn: str) -> str:
    """Quote a 'schema.table' FQN as [schema].[table].

    Splits on the first dot only so table names containing dots are handled
    correctly (though unusual in SQL Server).
    """
    parts = table_fqn.split('.', 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"table_fqn must be 'schema.table', got: {table_fqn!r}"
        )
    return f"{_q(parts[0])}.{_q(parts[1])}"


def _nolock(use_nolock: bool) -> str:
    return ' WITH (NOLOCK)' if use_nolock else ''


def _validate_limit(limit: int, name: str = 'limit') -> int:
    if not isinstance(limit, int) or limit < 1 or limit > 10_000:
        raise ValueError(f"{name} must be an integer between 1 and 10 000, got: {limit!r}")
    return limit


# ── Public query builders ──────────────────────────────────────────────────────

def build_row_count_query(table_fqn: str, use_nolock: bool = True) -> str:
    """Return a query that counts all rows in a table.

    Uses COUNT_BIG to avoid INT overflow on tables with > 2 billion rows.
    """
    return (
        f"SELECT COUNT_BIG(*) AS row_count\n"
        f"FROM {_fqn(table_fqn)}{_nolock(use_nolock)}"
    )


def build_date_range_query(
    table_fqn: str,
    column_name: str,
    use_nolock: bool = True,
) -> str:
    """Return a query that finds the earliest and latest value in a date column."""
    t = _fqn(table_fqn)
    c = _q(column_name)
    return (
        f"SELECT\n"
        f"    MIN({c}) AS earliest_record,\n"
        f"    MAX({c}) AS latest_record\n"
        f"FROM {t}{_nolock(use_nolock)}"
    )


def build_column_stats_query(
    table_fqn: str,
    column_name: str,
    data_type: str,
    use_nolock: bool = True,
) -> str:
    """Return a column-level statistics query appropriate for the normalized data_type.

    data_type must be one of the normalized types from core/connectors/schema.py:
    TEXT | INTEGER | DECIMAL | DATETIME | BOOLEAN | BINARY | JSON | OTHER

    The returned SQL always includes:
      total_rows, populated_count, null_count, distinct_count, min_value, max_value

    Additional columns depend on type:
      TEXT    → min_length, max_length_observed, avg_length, empty_string_count
      INTEGER/DECIMAL → mean_value, std_deviation, zero_count
      BOOLEAN → zero_count (count of FALSE/0 values)
    """
    t   = _fqn(table_fqn)
    c   = _q(column_name)
    nl  = _nolock(use_nolock)
    dt  = data_type.upper()

    common = (
        f"    COUNT_BIG(*)                              AS total_rows,\n"
        f"    COUNT_BIG({c})                            AS populated_count,\n"
        f"    COUNT_BIG(*) - COUNT_BIG({c})             AS null_count,\n"
        f"    COUNT_BIG(DISTINCT {c})                   AS distinct_count,\n"
    )

    if dt == 'TEXT':
        extra = (
            f"    MIN({c})                                   AS min_value,\n"
            f"    MAX({c})                                   AS max_value,\n"
            f"    MIN(LEN({c}))                              AS min_length,\n"
            f"    MAX(LEN({c}))                              AS max_length_observed,\n"
            f"    AVG(CAST(LEN({c}) AS FLOAT))               AS avg_length,\n"
            f"    SUM(CASE WHEN LEN(LTRIM(RTRIM({c}))) = 0\n"
            f"             THEN 1 ELSE 0 END)               AS empty_string_count\n"
        )
    elif dt in ('INTEGER', 'DECIMAL'):
        extra = (
            f"    CAST(MIN({c}) AS NVARCHAR(100))            AS min_value,\n"
            f"    CAST(MAX({c}) AS NVARCHAR(100))            AS max_value,\n"
            f"    AVG(CAST({c} AS FLOAT))                    AS mean_value,\n"
            f"    STDEV(CAST({c} AS FLOAT))                  AS std_deviation,\n"
            f"    SUM(CASE WHEN {c} = 0 THEN 1 ELSE 0 END)  AS zero_count\n"
        )
    elif dt == 'DATETIME':
        extra = (
            f"    CAST(MIN({c}) AS NVARCHAR(50))             AS min_value,\n"
            f"    CAST(MAX({c}) AS NVARCHAR(50))             AS max_value\n"
        )
    elif dt == 'BOOLEAN':
        extra = (
            f"    CAST(MIN({c}) AS NVARCHAR(10))             AS min_value,\n"
            f"    CAST(MAX({c}) AS NVARCHAR(10))             AS max_value,\n"
            f"    SUM(CASE WHEN {c} = 0 THEN 1 ELSE 0 END)  AS zero_count\n"
        )
    else:
        # BINARY, JSON, OTHER — min/max cast to string; no type-specific metrics
        extra = (
            f"    CAST(MIN(CAST({c} AS NVARCHAR(100))) AS NVARCHAR(100))\n"
            f"                                               AS min_value,\n"
            f"    CAST(MAX(CAST({c} AS NVARCHAR(100))) AS NVARCHAR(100))\n"
            f"                                               AS max_value\n"
        )

    return f"SELECT\n{common}{extra}FROM {t}{nl}"


def build_top_values_query(
    table_fqn: str,
    column_name: str,
    limit: int = 20,
    use_nolock: bool = True,
) -> str:
    """Return a query for the top-N most frequent non-null values in a column.

    Returns: value (NVARCHAR), row_count.
    The caller computes percentage = row_count / total_rows (from column stats).
    Capped at 10 000 to prevent runaway result sets.
    """
    _validate_limit(limit, 'limit')
    t  = _fqn(table_fqn)
    c  = _q(column_name)
    nl = _nolock(use_nolock)
    return (
        f"SELECT TOP ({limit})\n"
        f"    CAST({c} AS NVARCHAR(500)) AS value,\n"
        f"    COUNT_BIG(*)               AS row_count\n"
        f"FROM {t}{nl}\n"
        f"WHERE {c} IS NOT NULL\n"
        f"GROUP BY CAST({c} AS NVARCHAR(500))\n"
        f"ORDER BY COUNT_BIG(*) DESC"
    )


def build_sample_values_query(
    table_fqn: str,
    column_name: str,
    limit: int = 10,
    sample_percent: int = 5,
    use_nolock: bool = True,
) -> str:
    """Return a query for a random sample of non-null values.

    Uses TABLESAMPLE for efficiency on large tables — samples at the page level,
    not the row level, so result count is approximate.  The caller should retry
    without TABLESAMPLE if the result is empty (common on very small tables).

    sample_percent: 1–100 (percent of pages to sample)
    """
    _validate_limit(limit, 'limit')
    if not isinstance(sample_percent, int) or not (1 <= sample_percent <= 100):
        raise ValueError(
            f"sample_percent must be an integer between 1 and 100, got: {sample_percent!r}"
        )
    t  = _fqn(table_fqn)
    c  = _q(column_name)
    nl = _nolock(use_nolock)
    return (
        f"SELECT TOP ({limit})\n"
        f"    CAST({c} AS NVARCHAR(500)) AS value\n"
        f"FROM {t} TABLESAMPLE ({sample_percent} PERCENT){nl}\n"
        f"WHERE {c} IS NOT NULL\n"
        f"ORDER BY NEWID()"
    )


def build_percentile_query(
    table_fqn: str,
    column_name: str,
    use_nolock: bool = True,
) -> str:
    """Return a query computing P5/P25/P50/P75/P95 for a numeric column.

    Uses PERCENTILE_CONT window functions (SQL Server 2012+), which are
    deterministic ordered-set aggregates.  Returns a single row via TOP (1)
    because the window function repeats the same value on every row.
    NULLs are excluded from the ordered set by the WHERE clause.
    All percentile values are NVARCHAR-cast for uniform string handling.
    """
    t       = _fqn(table_fqn)
    c       = _q(column_name)
    nl      = _nolock(use_nolock)
    cast_c  = f"CAST({c} AS FLOAT)"
    return (
        f"SELECT TOP (1)\n"
        f"    CAST(PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY {cast_c}) OVER () AS NVARCHAR(100)) AS p5_value,\n"
        f"    CAST(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {cast_c}) OVER () AS NVARCHAR(100)) AS p25_value,\n"
        f"    CAST(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY {cast_c}) OVER () AS NVARCHAR(100)) AS p50_value,\n"
        f"    CAST(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {cast_c}) OVER () AS NVARCHAR(100)) AS p75_value,\n"
        f"    CAST(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {cast_c}) OVER () AS NVARCHAR(100)) AS p95_value\n"
        f"FROM {t}{nl}\n"
        f"WHERE {c} IS NOT NULL"
    )
