"""
Bounded execution of an already-validated read-only SQL Server query — SQL
rebuild Phase 3, Step 3.

Thin orchestration over existing building blocks — it does not create a
parallel connection or credential system:

  1. core.sqlservice.sql_validation.SqlValidationResult
     — the ALREADY-validated candidate (this module never re-parses,
       re-derives, or repairs the SQL; STATUS_OK is a hard precondition).
  2. core.sqlservice.retrieval_store.get_retrieval_build()
     — the SAME ownership + build-belongs-to-source check
       core.sqlservice.schema_context_retrieval already relies on; reused
       here to verify source_id owns the build the validated SQL was
       checked against, and that its schema_fingerprint has not drifted.
  3. data.datasource_service.get_connection_config()
     — the project's one source of decrypted connection parameters
       (ownership-scoped, never returns encrypted_config_json).
  4. core.connectors.registry.get("mssql") /
     core.connectors.relational.mssql.SQLServerConnector.open_connection()
     — the project's one SQL Server connection-string-building and
       pyodbc.connect() call. Never reimplemented here.

This module's own job is exactly what's between those: refuse anything
that is not a successful, source-owned, SQL-Server-typed, currently-active
validation result; open exactly one live connection through the existing
connector; run the validated SQL completely unmodified under a real pyodbc
query timeout; bound memory via fetchmany() batching and a hard row cap;
normalize the returned values into safe, immutable, JSON-friendly forms;
and sanitize any failure into a high-level category with a fixed, generic
message that can never echo driver text (which could contain a connection
string or credential fragment).

Scope: this module never rewrites or repairs SQL, never retries a failed
or timed-out query, never commits, never wires into a route or the
connection-time coordinator, and never generates an answer from the
result — it returns typed, immutable data only.

pyodbc query timeout (see execute_validated_sql's docstring, requirement
15): the `timeout` keyword passed to `pyodbc.connect()` — which is what
core.connectors.relational.mssql.SQLServerConnector.open_connection()
accepts as `timeout_s` — bounds only the LOGIN/connect phase
(SQL_ATTR_LOGIN_TIMEOUT). It has no effect on how long a query is allowed
to run once connected. The installed pyodbc (5.3.0) exposes a separate,
settable `Connection.timeout` property that bounds subsequent statement
execution on cursors created from that connection
(SQL_ATTR_QUERY_TIMEOUT) — this module sets that property, once, right
after connecting and before creating a cursor, using
`query_timeout_seconds` (a distinct, independently configurable value
from the connect timeout).
"""

from __future__ import annotations

import datetime
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from core.config import (
    SQL_EXECUTION_CONNECT_TIMEOUT_SECONDS,
    SQL_EXECUTION_FETCH_BATCH_SIZE,
    SQL_EXECUTION_MAX_CELL_CHARACTERS,
    SQL_EXECUTION_MAX_COLUMNS,
    SQL_EXECUTION_MAX_ROWS,
    SQL_EXECUTION_MAX_SQL_CHARACTERS,
    SQL_EXECUTION_MAX_TOTAL_CHARACTERS,
    SQL_EXECUTION_QUERY_TIMEOUT_SECONDS,
)
from core.connectors.base import DataSourceConfig
from core.sqlservice.retrieval_store import get_retrieval_build
from core.sqlservice.sql_validation import STATUS_OK as _SQL_VALIDATION_STATUS_OK
from core.sqlservice.sql_validation import SqlValidationResult
from data.datasource_service import get_connection_config

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_OK",
    "STATUS_REJECTED",
    "STATUS_ERROR",
    "CATEGORY_TIMEOUT",
    "CATEGORY_CONNECTIVITY",
    "CATEGORY_PERMISSION",
    "CATEGORY_SCHEMA_CHANGED",
    "CATEGORY_EXECUTION_ERROR",
    "TRUNCATION_REASON_ROW_LIMIT",
    "TRUNCATION_REASON_CELL_CHARACTERS",
    "TRUNCATION_REASON_TOTAL_CHARACTERS",
    "SOURCE_TYPE",
    "ConnectionFactory",
    "SqlExecutionResult",
    "execute_validated_sql",
]

STATUS_OK = "OK"
STATUS_REJECTED = "REJECTED"  # refused before/without a completed live execution — no DB error occurred
STATUS_ERROR = "ERROR"        # a live execution was attempted and the database/driver failed

CATEGORY_TIMEOUT = "timeout"
CATEGORY_CONNECTIVITY = "connectivity"
CATEGORY_PERMISSION = "permission"
CATEGORY_SCHEMA_CHANGED = "schema_changed"
CATEGORY_EXECUTION_ERROR = "execution_error"

TRUNCATION_REASON_ROW_LIMIT = "row_limit"
TRUNCATION_REASON_CELL_CHARACTERS = "cell_characters"
TRUNCATION_REASON_TOTAL_CHARACTERS = "total_character_limit"

# The only source_type this module ever executes against. A source of any
# other type is always rejected, regardless of its capabilities.
SOURCE_TYPE = "mssql"

ConnectionFactory = Callable[[DataSourceConfig, int], object]


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SqlExecutionResult:
    """
    Result of one execute_validated_sql() call.

    status: STATUS_OK | STATUS_REJECTED | STATUS_ERROR.
        STATUS_REJECTED means no query was ever sent to a live connection
        (or a resultset was cut off before any row was returned, in the
        max_columns case) — see `error` for why. STATUS_ERROR means a live
        execution was attempted and the driver/database failed — see
        `error` (fixed, sanitized, category-derived text — never raw
        driver text) and `error_category` (one of the CATEGORY_* constants).
    build_id / schema_fingerprint: echoed unchanged from the
        SqlValidationResult this call was given — this result's own
        source/build/schema identity, not re-derived.
    canonical_referenced_object_fqns: echoed unchanged from the
        SqlValidationResult this call was given.
    columns: column names in cursor.description order. Duplicate names are
        preserved by position — never deduplicated or used as dict keys.
    rows: tuple of tuples, one per returned row, values in column order
        (see _normalize_value for the exact per-value normalization).
        Positional, matching `columns` — never a list of dicts, so a
        duplicate column name never silently overwrites another.
    row_count: len(rows) — the number actually returned after every bound
        (row/cell/total-character) was applied, never the number visited
        internally before truncation.
    truncated / truncation_reason: truncated is True when the live result
        was cut short by ANY bound (row cap, per-cell character cap, or
        total-character budget). truncation_reason lists every bound that
        actually fired, joined with "; " (never just the first) — None
        when truncated is False.
    """

    status: str
    source_id: int
    build_id: Optional[int]
    schema_fingerprint: Optional[str]
    canonical_referenced_object_fqns: tuple
    columns: tuple
    rows: tuple
    row_count: int
    truncated: bool
    truncation_reason: Optional[str]
    duration_ms: int
    warnings: tuple
    error: Optional[str]
    error_category: Optional[str]


# ── Error categorization / sanitization ────────────────────────────────────
#
# Never returns any part of the original exception text — only a fixed,
# generic message per category (see _CATEGORY_MESSAGES). This is what
# guarantees a password, connection string fragment, decrypted credential,
# or API key embedded in a driver message can never reach a caller,
# regardless of what the driver actually said.

_SQLSTATE_CATEGORY: dict = {
    "HYT00": CATEGORY_TIMEOUT,
    "HYT01": CATEGORY_TIMEOUT,
    "HY008": CATEGORY_TIMEOUT,
    "08001": CATEGORY_CONNECTIVITY,
    "08003": CATEGORY_CONNECTIVITY,
    "08004": CATEGORY_CONNECTIVITY,
    "08006": CATEGORY_CONNECTIVITY,
    "08007": CATEGORY_CONNECTIVITY,
    "08S01": CATEGORY_CONNECTIVITY,
    "28000": CATEGORY_PERMISSION,
    "42000": CATEGORY_PERMISSION,
    "42S02": CATEGORY_SCHEMA_CHANGED,
    "42S22": CATEGORY_SCHEMA_CHANGED,
    "42S12": CATEGORY_SCHEMA_CHANGED,
}

_KEYWORD_CATEGORY: tuple = (
    ("timeout", CATEGORY_TIMEOUT),
    ("timed out", CATEGORY_TIMEOUT),
    ("query timeout", CATEGORY_TIMEOUT),
    ("login failed", CATEGORY_PERMISSION),
    ("invalid authorization", CATEGORY_PERMISSION),
    ("permission", CATEGORY_PERMISSION),
    ("access is denied", CATEGORY_PERMISSION),
    ("denied", CATEGORY_PERMISSION),
    ("invalid object name", CATEGORY_SCHEMA_CHANGED),
    ("invalid column name", CATEGORY_SCHEMA_CHANGED),
    ("network-related", CATEGORY_CONNECTIVITY),
    ("server was not found", CATEGORY_CONNECTIVITY),
    ("could not open a connection", CATEGORY_CONNECTIVITY),
    ("connection", CATEGORY_CONNECTIVITY),
)

_CATEGORY_MESSAGES: dict = {
    CATEGORY_TIMEOUT: "The query exceeded the configured execution timeout.",
    CATEGORY_CONNECTIVITY: "Failed to connect to the SQL Server data source.",
    CATEGORY_PERMISSION: "The database rejected the query due to insufficient permissions.",
    CATEGORY_SCHEMA_CHANGED: "The query referenced schema objects that could not be resolved; the schema may have changed.",
    CATEGORY_EXECUTION_ERROR: "The query failed during execution.",
}


def _categorize_error(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], str) and len(args[0]) == 5:
        sqlstate = args[0].upper()
        if sqlstate in _SQLSTATE_CATEGORY:
            return _SQLSTATE_CATEGORY[sqlstate]
        if sqlstate.startswith("08"):
            return CATEGORY_CONNECTIVITY

    text = str(exc).lower()
    for keyword, category in _KEYWORD_CATEGORY:
        if keyword in text:
            return category
    return CATEGORY_EXECUTION_ERROR


# ── Value normalization (pure, no I/O) ──────────────────────────────────────


def _normalize_value(value: object, max_cell_characters: int) -> tuple:
    """
    Returns (normalized_value, was_truncated, warning).

    None -> None, unchanged.
    bool -> bool, unchanged (checked before int: bool is an int subclass).
    int / float / decimal.Decimal -> unchanged, exact value/precision kept.
    datetime.datetime / datetime.date / datetime.time -> str via
        .isoformat() (never a raw datetime object, which is not
        JSON-serializable and unsafe to hand to later answer generation
        unnormalized).
    uuid.UUID -> str via str(value) (canonical hyphenated form).
    bytes / bytearray / memoryview -> str, a lowercase hex encoding of the
        raw bytes, subject to the same max_cell_characters cap as text.
    str -> unchanged, unless longer than max_cell_characters, in which
        case truncated to exactly max_cell_characters characters
        (was_truncated=True).
    anything else (a type this module does not recognize) -> the fixed
        placeholder string "<unsupported:{type name}>" — NEVER str(value)
        or repr(value), which could leak an arbitrary object's internal
        state (a raw driver/connection object, for example). A warning
        describing the type is returned for the caller to attach a column
        name to.
    """
    if value is None:
        return None, False, None
    if isinstance(value, bool):
        return value, False, None
    if isinstance(value, int):
        return value, False, None
    if isinstance(value, float):
        return value, False, None
    if isinstance(value, Decimal):
        return value, False, None
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(), False, None
    if isinstance(value, uuid.UUID):
        return str(value), False, None
    if isinstance(value, (bytes, bytearray, memoryview)):
        text = bytes(value).hex()
        if len(text) > max_cell_characters:
            return text[:max_cell_characters], True, None
        return text, False, None
    if isinstance(value, str):
        if len(value) > max_cell_characters:
            return value[:max_cell_characters], True, None
        return value, False, None
    return (
        f"<unsupported:{type(value).__name__}>",
        False,
        f"unsupported value type {type(value).__name__!r} was replaced with a placeholder",
    )


def _character_contribution(value: object) -> int:
    """
    Character cost of one already-normalized (and, where applicable,
    already per-cell-truncated) value against the max_total_characters
    budget.

    Counting rule (this is the exact representation later answer
    generation will be handed, via SqlExecutionResult.rows — nothing else
    is measured): None contributes 0; every other value contributes
    len(str(value)), i.e. the length of the same textual form a caller
    would get from str() on that normalized value (an int, a float, a
    Decimal, or a string already produced by _normalize_value — including
    an isoformat() date/time/datetime, a UUID's str(), a hex-encoded
    binary value, or an already max_cell_characters-truncated string).
    Column headers are metadata, not row content, and are never counted
    against this budget.
    """
    if value is None:
        return 0
    return len(str(value))


def _normalize_rows(
    rows_raw: list,
    columns: tuple,
    max_cell_characters: int,
    max_total_characters: int,
) -> tuple:
    """
    Returns (normalized_rows: list[tuple], cell_truncated: bool,
    total_truncated: bool, warnings: list[str]).

    max_total_characters is a HARD ceiling that the returned rows' total
    character cost (see _character_contribution) never exceeds — not even
    for the first row. A row is appended only when doing so keeps the
    running total at or under max_total_characters (an exact match at the
    boundary is accepted; one character over is rejected); a row that
    does not fit is NEVER partially included, and packing stops there —
    later rows are never considered even if a smaller one would have fit,
    mirroring core.sqlservice.schema_context_retrieval's own
    character-budget packing. If the very first normalized row alone
    exceeds the budget, the result is zero rows with total_truncated=True
    (the caller still returns STATUS_OK with column metadata intact —
    this is a bound being enforced, never a database execution error).

    Per-cell truncation (see _normalize_value) always runs BEFORE this
    budget check, so a cell shortened by max_cell_characters is counted at
    its shortened length — per-cell truncation can be exactly what makes
    an otherwise-too-wide row fit.
    """
    normalized_rows: list = []
    warnings: list = []
    cell_truncated = False
    total_truncated = False
    used_characters = 0

    for raw_row in rows_raw:
        normalized_row: list = []
        row_characters = 0
        for col_index, cell in enumerate(raw_row):
            value, was_cell_truncated, warning = _normalize_value(cell, max_cell_characters)
            if was_cell_truncated:
                cell_truncated = True
            if warning:
                column_name = columns[col_index] if col_index < len(columns) else f"#{col_index}"
                warnings.append(f"column {column_name!r}: {warning}")
            normalized_row.append(value)
            row_characters += _character_contribution(value)

        if used_characters + row_characters > max_total_characters:
            total_truncated = True
            break

        normalized_rows.append(tuple(normalized_row))
        used_characters += row_characters

    return normalized_rows, cell_truncated, total_truncated, warnings


# ── Default connection factory (reuses the existing connector — never a
#    parallel connection system) ────────────────────────────────────────────


def _default_connection_factory(config: DataSourceConfig, connect_timeout_seconds: int) -> object:
    import core.connectors.registry as registry

    connector_cls = registry.get(SOURCE_TYPE)
    if connector_cls is None:
        raise RuntimeError(f"No connector registered for source_type {SOURCE_TYPE!r}.")
    return connector_cls().open_connection(config, timeout_s=connect_timeout_seconds)


# ── Result builders ─────────────────────────────────────────────────────────


def _rejected(
    validation_result: SqlValidationResult, source_id: int, reason: str, duration_ms: int,
) -> SqlExecutionResult:
    return SqlExecutionResult(
        status=STATUS_REJECTED,
        source_id=source_id,
        build_id=validation_result.build_id,
        schema_fingerprint=validation_result.schema_fingerprint,
        canonical_referenced_object_fqns=validation_result.canonical_referenced_object_fqns,
        columns=(), rows=(), row_count=0,
        truncated=False, truncation_reason=None,
        duration_ms=duration_ms, warnings=(),
        error=reason, error_category=None,
    )


def _errored(
    validation_result: SqlValidationResult, source_id: int, category: str, duration_ms: int,
) -> SqlExecutionResult:
    return SqlExecutionResult(
        status=STATUS_ERROR,
        source_id=source_id,
        build_id=validation_result.build_id,
        schema_fingerprint=validation_result.schema_fingerprint,
        canonical_referenced_object_fqns=validation_result.canonical_referenced_object_fqns,
        columns=(), rows=(), row_count=0,
        truncated=False, truncation_reason=None,
        duration_ms=duration_ms, warnings=(),
        error=_CATEGORY_MESSAGES[category], error_category=category,
    )


# ── Public entry point ────────────────────────────────────────────────────


def execute_validated_sql(
    validation_result: SqlValidationResult,
    source_id: int,
    user_id: str,
    *,
    max_rows: int = SQL_EXECUTION_MAX_ROWS,
    max_columns: int = SQL_EXECUTION_MAX_COLUMNS,
    max_cell_characters: int = SQL_EXECUTION_MAX_CELL_CHARACTERS,
    max_total_characters: int = SQL_EXECUTION_MAX_TOTAL_CHARACTERS,
    max_sql_characters: int = SQL_EXECUTION_MAX_SQL_CHARACTERS,
    fetch_batch_size: int = SQL_EXECUTION_FETCH_BATCH_SIZE,
    query_timeout_seconds: int = SQL_EXECUTION_QUERY_TIMEOUT_SECONDS,
    connect_timeout_seconds: int = SQL_EXECUTION_CONNECT_TIMEOUT_SECONDS,
    connection_factory: Optional[ConnectionFactory] = None,
    clock: Callable[[], float] = time.monotonic,
) -> SqlExecutionResult:
    """
    Execute exactly one already-validated, read-only SQL Server SELECT
    against its owned live source, under strict bounds. Never rewrites the
    SQL, never retries, never commits.

    Order of operations (cheapest / no-I/O checks first; a live connection
    is opened only after every check before it has passed):
      1. Validate every limit parameter is a positive int — raises
         ValueError (a caller/programmer contract violation), no I/O yet.
      2. Require validation_result.status == sql_validation.STATUS_OK,
         and non-blank SQL within max_sql_characters — anything else is
         STATUS_REJECTED with zero database access of any kind.
      3. source_id/user_id must be given, and
         core.sqlservice.retrieval_store.get_retrieval_build() must return
         a row: this is simultaneously the ownership check (requirement 3)
         and the source/build identity check (requirement 4) — it returns
         None for an unowned source, a build_id that doesn't exist, or one
         that belongs to a different source. Its schema_fingerprint must
         then equal validation_result.schema_fingerprint exactly, or the
         validated build may have been superseded by a newer schema.
      4. data.datasource_service.get_connection_config() must return a
         row: source must exist (again ownership-scoped), be active
         (is_active and source_status != 'ERROR'), and be source_type
         'mssql' — anything else is STATUS_REJECTED, still with no live
         connection opened.
      5. Open exactly one live connection via connection_factory (default:
         the registered 'mssql' connector's own open_connection(), i.e.
         the project's one pyodbc.connect() call — never a parallel
         connection system). A failure here is STATUS_ERROR, categorized
         and sanitized (see _categorize_error / _CATEGORY_MESSAGES).
      6. Set connection.timeout = query_timeout_seconds (the real pyodbc
         QUERY timeout — see this module's docstring for why this is NOT
         the same as connect_timeout_seconds), open one cursor, and
         execute the validated SQL completely unmodified.
      7. Reject (STATUS_REJECTED) if the resultset has more than
         max_columns columns — before fetching a single row.
      8. fetchmany() in fetch_batch_size batches, never more than
         max_rows + 1 rows total (detects truncation without loading an
         unlimited result set into memory).
      9. Normalize every value (see _normalize_value) and pack rows into
         the max_total_characters budget (see _normalize_rows) — a hard
         ceiling that is never exceeded, not even by the first row (a
         first row that alone is too large yields zero rows, not a
         partially-written or oversized one) — then report
         truncated/truncation_reason for every bound that fired.
      10. In `finally`: close the cursor, roll back the connection (always
          — including after a successful SELECT; commit() is never
          called), and close the connection. This runs on every exit path,
          success or failure.
    """
    for name, value in (
        ("max_rows", max_rows), ("max_columns", max_columns),
        ("max_cell_characters", max_cell_characters), ("max_total_characters", max_total_characters),
        ("max_sql_characters", max_sql_characters), ("fetch_batch_size", fetch_batch_size),
        ("query_timeout_seconds", query_timeout_seconds), ("connect_timeout_seconds", connect_timeout_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value!r}")

    start = clock()

    def _duration_ms() -> int:
        return int((clock() - start) * 1000)

    if validation_result.status != _SQL_VALIDATION_STATUS_OK:
        return _rejected(
            validation_result, source_id,
            f"validation_result.status={validation_result.status!r} is not {_SQL_VALIDATION_STATUS_OK!r}",
            _duration_ms(),
        )

    sql = validation_result.sql
    if not sql or not sql.strip():
        return _rejected(validation_result, source_id, "validated SQL is blank", _duration_ms())
    if len(sql) > max_sql_characters:
        return _rejected(
            validation_result, source_id,
            f"validated SQL length {len(sql)} exceeds the maximum of {max_sql_characters} characters",
            _duration_ms(),
        )

    if source_id is None or user_id is None:
        return _rejected(validation_result, source_id, "source_id and user_id are required", _duration_ms())

    if validation_result.build_id is None:
        return _rejected(
            validation_result, source_id, "validation_result.build_id is missing", _duration_ms()
        )

    build_row = get_retrieval_build(validation_result.build_id, source_id, user_id)
    if build_row is None:
        return _rejected(
            validation_result, source_id,
            f"source_id={source_id} does not own build_id={validation_result.build_id}, "
            "or the source does not exist",
            _duration_ms(),
        )
    if build_row.schema_fingerprint != validation_result.schema_fingerprint:
        return _rejected(
            validation_result, source_id,
            "schema fingerprint mismatch: the validated build may have been superseded by a newer schema",
            _duration_ms(),
        )

    connection_record = get_connection_config(source_id, user_id)
    if connection_record is None:
        return _rejected(
            validation_result, source_id, "data source not found or not owned by this user", _duration_ms()
        )
    if not connection_record["is_active"] or connection_record["source_status"] == "ERROR":
        return _rejected(validation_result, source_id, "data source is disabled", _duration_ms())
    if connection_record["source_type"] != SOURCE_TYPE:
        return _rejected(
            validation_result, source_id,
            f"data source type {connection_record['source_type']!r} is not {SOURCE_TYPE!r}",
            _duration_ms(),
        )

    config = DataSourceConfig(
        source_type=SOURCE_TYPE,
        params={**connection_record["params"], "_source_id": source_id},
    )
    factory = connection_factory or _default_connection_factory

    try:
        connection = factory(config, connect_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — sanitized before it ever leaves this module
        logger.warning("execute_validated_sql: connection open failed [source_id=%s]", source_id)
        return _errored(validation_result, source_id, _categorize_error(exc), _duration_ms())

    cursor = None
    try:
        connection.timeout = query_timeout_seconds
        cursor = connection.cursor()
        cursor.execute(sql)

        description = cursor.description or ()
        columns = tuple(d[0] for d in description)
        if len(columns) > max_columns:
            return _rejected(
                validation_result, source_id,
                f"result has {len(columns)} columns, exceeds the maximum of {max_columns}",
                _duration_ms(),
            )

        fetch_limit = max_rows + 1
        rows_raw: list = []
        while len(rows_raw) < fetch_limit:
            batch = cursor.fetchmany(min(fetch_batch_size, fetch_limit - len(rows_raw)))
            if not batch:
                break
            rows_raw.extend(batch)

        row_limit_truncated = len(rows_raw) > max_rows
        if row_limit_truncated:
            rows_raw = rows_raw[:max_rows]

        normalized_rows, cell_truncated, total_truncated, warnings = _normalize_rows(
            rows_raw, columns, max_cell_characters, max_total_characters,
        )

        reasons = []
        if row_limit_truncated:
            reasons.append(TRUNCATION_REASON_ROW_LIMIT)
        if cell_truncated:
            reasons.append(TRUNCATION_REASON_CELL_CHARACTERS)
        if total_truncated:
            reasons.append(TRUNCATION_REASON_TOTAL_CHARACTERS)

        return SqlExecutionResult(
            status=STATUS_OK,
            source_id=source_id,
            build_id=validation_result.build_id,
            schema_fingerprint=validation_result.schema_fingerprint,
            canonical_referenced_object_fqns=validation_result.canonical_referenced_object_fqns,
            columns=columns,
            rows=tuple(normalized_rows),
            row_count=len(normalized_rows),
            truncated=bool(reasons),
            truncation_reason="; ".join(reasons) if reasons else None,
            duration_ms=_duration_ms(),
            warnings=tuple(warnings),
            error=None,
            error_category=None,
        )
    except Exception as exc:  # noqa: BLE001 — sanitized before it ever leaves this module
        logger.warning("execute_validated_sql: query execution failed [source_id=%s]", source_id)
        return _errored(validation_result, source_id, _categorize_error(exc), _duration_ms())
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
