"""
SQL Server profiling query builders.

Returns complete, parameterised-safe SQL strings.
No queries are executed here.  No pyodbc import.
Every table/column identifier is bracket-quoted to prevent injection.
"""

import math
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


def build_referential_overlap_query(
    from_table_fqn: str,
    from_column: str,
    to_table_fqn: str,
    to_column: str,
    use_nolock: bool = True,
) -> str:
    """Return a single-scan query measuring referential coverage: how many
    distinct non-null from_column values also appear in to_column.

    Uses one LEFT JOIN (not a per-row correlated subquery) so cost is one
    pass over from_table joined to to_table — cheap when to_column is a
    primary/unique key (the expected case; callers only call this for
    candidates where to_column already looks key-like), since the join can
    use to_column's own index.

    Returns a single row: source_nonnull_count, source_distinct_count,
    matched_distinct_count. matched_distinct_count / source_distinct_count
    is the referential coverage ratio the caller scores as evidence.
    """
    ft = _fqn(from_table_fqn)
    fc = _q(from_column)
    tt = _fqn(to_table_fqn)
    tc = _q(to_column)
    nl = _nolock(use_nolock)
    return (
        f"SELECT\n"
        f"    COUNT_BIG(*)                                   AS source_nonnull_count,\n"
        f"    COUNT_BIG(DISTINCT s.{fc})                     AS source_distinct_count,\n"
        f"    COUNT_BIG(DISTINCT CASE WHEN t.{tc} IS NOT NULL\n"
        f"                            THEN s.{fc} END)        AS matched_distinct_count\n"
        f"FROM {ft} AS s{nl}\n"
        f"LEFT JOIN {tt} AS t{nl} ON t.{tc} = s.{fc}\n"
        f"WHERE s.{fc} IS NOT NULL"
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


def build_histogram_query(
    table_fqn: str,
    column_name: str,
    min_val: float,
    max_val: float,
    n_buckets: int = 10,
    use_nolock: bool = True,
) -> str:
    """Return a single-scan histogram query for a numeric column.

    Injects pre-validated min_val and max_val as safe float literals (never
    user-supplied strings).  Returns (bucket_idx, row_count) for each
    non-empty bucket; the caller reconstructs empty buckets and percentages.

    The inner derived table assigns each value to a bucket in one table scan.
    The max value is clamped to the last bucket to handle float precision edge
    cases where FLOOR would otherwise produce index n_buckets.
    """
    if not isinstance(n_buckets, int) or n_buckets < 1 or n_buckets > 1_000:
        raise ValueError(f"n_buckets must be an integer between 1 and 1000, got: {n_buckets!r}")
    if not math.isfinite(min_val) or not math.isfinite(max_val):
        raise ValueError("min_val and max_val must be finite floats")
    if min_val > max_val:
        raise ValueError("min_val must be <= max_val")

    t  = _fqn(table_fqn)
    c  = _q(column_name)
    nl = _nolock(use_nolock)

    # Constant column: return one bucket with all rows in a single query.
    if min_val == max_val:
        return (
            f"SELECT 0 AS bucket_idx, COUNT_BIG(*) AS row_count\n"
            f"FROM {t}{nl}\n"
            f"WHERE {c} IS NOT NULL"
        )

    bucket_width = (max_val - min_val) / n_buckets
    n_minus_1    = n_buckets - 1

    # Format as full-precision float literals — numeric, never user-controlled.
    min_s = repr(float(min_val))
    max_s = repr(float(max_val))
    bw_s  = repr(float(bucket_width))

    return (
        f"SELECT bucket_idx, COUNT_BIG(*) AS row_count\n"
        f"FROM (\n"
        f"    SELECT\n"
        f"        CASE\n"
        f"            WHEN CAST({c} AS FLOAT) >= {max_s} THEN {n_minus_1}\n"
        f"            WHEN CAST({c} AS FLOAT) <  {min_s} THEN 0\n"
        f"            ELSE CAST(FLOOR((CAST({c} AS FLOAT) - {min_s}) / {bw_s}) AS INT)\n"
        f"        END AS bucket_idx\n"
        f"    FROM {t}{nl}\n"
        f"    WHERE {c} IS NOT NULL\n"
        f") AS _hist\n"
        f"GROUP BY bucket_idx\n"
        f"ORDER BY bucket_idx"
    )


def build_view_dependency_query() -> str:
    """
    SQL rebuild Phase 5, Step 6 — authoritative, catalog-only, same-database
    view -> table/view dependency query. Never parses view SQL text and
    never guesses: every row is resolved by SQL Server's own compiled
    dependency catalog (sys.sql_expression_dependencies) — the identical
    mechanism SQL Server Management Studio's own "View Dependencies"
    feature relies on.

    Every exclusion happens in this WHERE clause — never as a second,
    Python-side filtering pass:
      - unresolved/dynamic references:      d.referenced_id IS NOT NULL
      - ambiguous references SQL Server itself could not uniquely resolve:
                                             d.is_ambiguous = 0
      - cross-database / linked-server refs: d.referenced_server_name and
        d.referenced_database_name must both be blank/absent or name this
        exact connection's own database
      - system objects:                     v.is_ms_shipped = 0 AND
                                             o.is_ms_shipped = 0 AND the
                                             target schema is never 'sys'
      - anything that is not a user table or a view (functions/procedures/
        triggers excluded per this step's own scope): o.type IN ('U','V')
      - self-references:                    d.referenced_id <> v.object_id

    SELECT DISTINCT collapses SQL Server's own multiple dependency rows for
    the same (view, target) pair (one row per referenced column, or one per
    repeated reference in the view body) into exactly one edge per pair —
    duplicate suppression happens here, in SQL, not as a second pass.

    No table/column/schema name from any specific customer's database is
    referenced anywhere in this function — every identifier here is a
    SQL Server SYSTEM catalog object name, present identically on every
    SQL Server instance.
    """
    return (
        "SELECT DISTINCT\n"
        "    sv.name AS view_schema,\n"
        "    v.name  AS view_name,\n"
        "    st.name AS ref_schema,\n"
        "    o.name  AS ref_name,\n"
        "    o.type  AS ref_type\n"
        "FROM sys.views AS v\n"
        "JOIN sys.schemas AS sv ON sv.schema_id = v.schema_id\n"
        "JOIN sys.sql_expression_dependencies AS d ON d.referencing_id = v.object_id\n"
        "JOIN sys.objects AS o ON o.object_id = d.referenced_id\n"
        "JOIN sys.schemas AS st ON st.schema_id = o.schema_id\n"
        "WHERE v.is_ms_shipped = 0\n"
        "  AND d.referenced_id IS NOT NULL\n"
        "  AND d.referenced_id <> v.object_id\n"
        "  AND d.is_ambiguous = 0\n"
        "  AND (d.referenced_server_name IS NULL OR d.referenced_server_name = '')\n"
        "  AND (d.referenced_database_name IS NULL OR d.referenced_database_name = DB_NAME())\n"
        "  AND o.is_ms_shipped = 0\n"
        "  AND st.name <> 'sys'\n"
        "  AND o.type IN ('U', 'V')"
    )
