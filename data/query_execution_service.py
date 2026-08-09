"""
Safe Read-Only Query Execution Service — Program 3 Phase 6.1.

Executes SQL produced by generate_sql() against connected data sources.
Every execution path enforces:
  1. Pre-execution safety gate  — validates sql_result flags and SQL structure.
  2. Governance re-check        — blocks unconfirmed PII; marks confirmed PII for masking.
  3. Row cap via fetchmany      — never fetchall; default 1 000, max 5 000.
  4. Thread timeout             — closes connection if query exceeds DEFAULT_QUERY_TIMEOUT_S.
  5. PII masking                — confirmed PII column values replaced with "***".
  6. Audit logging              — records execution event; never logs raw SQL or values.

This module does NOT:
  - Accept raw SQL from callers.
  - Execute write statements.
  - Return unmasked confirmed PII values.
  - Create the query_execution_log table (Phase 6.2).
"""

import decimal
import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone

import core.connectors.registry as _registry
from core.connectors.base import DataSourceConfig
from core.secrets.manager import get_secret_manager
from data.audit import log_audit_event
from data.business_knowledge_service import get_column_business_contexts_batch
from data.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_QUERY_TIMEOUT_S: int = 30
DEFAULT_ROW_LIMIT: int = 1_000
MAX_ROW_LIMIT: int = 5_000

# ---------------------------------------------------------------------------
# Operational safeguard constants — Phase 6.3
# ---------------------------------------------------------------------------

RATE_LIMIT_WINDOW_S: int      = 2     # minimum seconds between per-user executions
DAILY_LIMIT: int              = 500   # max execution attempts per user per UTC day
SOURCE_RATE_PER_MINUTE: int   = 60    # max executions per source per 60-second window
REPEATED_QUERY_WINDOW_S: int  = 300   # look-back window (seconds) for repeated-query detection
REPEATED_QUERY_THRESHOLD: int = 3     # inclusive count at which the warning fires

# Write / DDL keywords blocked anywhere in the SQL string (after stripping
# quoted identifiers so bracketed column names don't cause false positives).
_WRITE_ANYWHERE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC(?:UTE)?|MERGE)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# SQL safety helpers
# ---------------------------------------------------------------------------

def _strip_quoted_identifiers(sql: str) -> str:
    """Remove quoted identifier tokens from SQL to enable safe keyword scanning.

    Strips [bracket quoted], "double quoted", and `backtick quoted` tokens.
    Single-quoted string literals are left in place — they never reach here
    because all filter values are parameterized (? / %s), not inlined.
    """
    s = re.sub(r"\[[^\]]*\]", " ", sql)
    s = re.sub(r'"[^"]*"', " ", s)
    s = re.sub(r"`[^`]*`", " ", s)
    return s


def _has_write_keywords(sql: str) -> bool:
    """Return True if write/DDL keywords appear outside quoted identifiers."""
    return bool(_WRITE_ANYWHERE_RE.search(_strip_quoted_identifiers(sql)))


def _safety_gate(
    sql: str | None,
    generated_sql_result: dict,
    sql_plan: dict,
) -> list[str]:
    """Return a list of blocking reasons.  Empty list means the query is safe to run.

    All checks happen BEFORE the database connection is opened.
    """
    blocks: list[str] = []

    if not sql:
        blocks.append("SQL string is missing — generate_sql() refused this plan.")
        return blocks  # no point continuing

    safety = generated_sql_result.get("safety") or {}
    if not safety.get("validated"):
        blocks.append(
            "generated_sql_result.safety.validated is not True — "
            "only SQL that passed full validation may be executed."
        )
    if not safety.get("select_only"):
        blocks.append(
            "generated_sql_result.safety.select_only is not True — "
            "only SELECT statements may be executed."
        )

    validation = (sql_plan.get("validation") or {})
    if not validation.get("valid"):
        blocking = validation.get("blocking_reasons") or ["sql_plan.validation.valid is False."]
        blocks.extend(blocking)

    if not sql.strip().upper().startswith("SELECT"):
        blocks.append("SQL does not begin with SELECT.")

    if ";" in sql:
        blocks.append(
            "SQL contains a semicolon — multi-statement queries are not permitted."
        )

    if _has_write_keywords(sql):
        blocks.append(
            "SQL contains a write or DDL keyword outside a quoted identifier — blocked."
        )

    return blocks


# ---------------------------------------------------------------------------
# Governance re-check
# ---------------------------------------------------------------------------

def _governance_recheck(
    source_id: int,
    user_id: str,
    sql_plan: dict,
) -> tuple[list[str], set[str], list[dict]]:
    """Re-check PII and approval state for every selected column.

    Returns:
      blocked_cols   — list of "table_fqn.column_name" strings for unconfirmed PII
      pii_aliases    — set of alias names (lowercased) whose values must be masked
      warnings       — warning dicts to surface to the caller
    """
    blocked_cols: list[str] = []
    pii_aliases:  set[str]  = set()
    warnings:     list[dict] = []

    selections = sql_plan.get("select") or []
    # Day 4, Capability 6 (Task 3) — one batched lookup for every selected
    # column instead of one get_column_business_context() call (its own
    # fresh SQLite connection + source-verify + snapshot-lookup) PER COLUMN.
    # Same fallback contract as before: a failure here must never block
    # execution — every column just falls through as if no context were
    # found (ctx is None), same as a per-column failure used to.
    requested_columns = [
        (sel.get("table_fqn"), sel.get("column_name"))
        for sel in selections
        if sel.get("table_fqn") and sel.get("column_name")
    ]
    try:
        ctx_by_column = get_column_business_contexts_batch(source_id, user_id, requested_columns)
    except Exception:
        logger.warning("_governance_recheck: get_column_business_contexts_batch failed for source_id=%s", source_id)
        ctx_by_column = {}

    for sel in selections:
        table_fqn   = sel.get("table_fqn")
        column_name = sel.get("column_name")
        alias       = (sel.get("alias") or column_name or "").lower()

        if not table_fqn or not column_name:
            continue

        ctx = ctx_by_column.get((table_fqn, column_name))
        if ctx is None:
            continue

        dic  = ctx.get("dictionary")
        prof = ctx.get("profiling")

        pii_flagged = (
            (prof and prof.get("pii_name_heuristic"))
            or (dic and dic.get("pii_risk"))
        )

        if pii_flagged:
            confirmed   = bool(prof and prof.get("pii_confirmed"))
            aggregation = sel.get("aggregation")
            if aggregation:
                # COUNT/SUM/AVG etc. emit a derived number — not the raw cell value.
                # PII on the source column does not apply to the aggregate result.
                warnings.append({
                    "type":     "pii_aggregated",
                    "severity": "LOW",
                    "message":  (
                        f"{table_fqn}.{column_name} has a PII flag but is "
                        f"aggregated ({aggregation}) — no raw values are returned."
                    ),
                })
            elif confirmed:
                pii_aliases.add(alias)
                warnings.append({
                    "type":     "pii_masked",
                    "severity": "MEDIUM",
                    "message":  (
                        f"{table_fqn}.{column_name} is confirmed PII — "
                        "returned values are masked."
                    ),
                })
            else:
                blocked_cols.append(f"{table_fqn}.{column_name}")
                warnings.append({
                    "type":     "pii_blocked",
                    "severity": "HIGH",
                    "message":  (
                        f"{table_fqn}.{column_name} has unconfirmed PII — "
                        "column blocked from execution."
                    ),
                })

        if not (dic and dic.get("is_approved")):
            warnings.append({
                "type":     "metadata_not_approved",
                "severity": "LOW",
                "message":  f"{table_fqn}.{column_name} has no approved dictionary entry.",
            })

    return blocked_cols, pii_aliases, warnings


# ---------------------------------------------------------------------------
# Governed execution — the one entry point every live-SQL caller must use
# ---------------------------------------------------------------------------

def execute_governed_query(
    source_id: int,
    user_id: str,
    sql: str,
    sql_plan: dict,
    *,
    params: "list | None" = None,
    row_limit: "int | None" = None,
    timeout_s: "int | None" = None,
    page: int = 1,
    page_size: "int | None" = None,
    max_payload_bytes: "int | None" = None,
    execution_kind: str = "user_query",
    existing_connection: object = None,
    connection_box: "dict | None" = None,
):
    """The one governed entry point for executing SQL derived from a
    business question (chat, execute-query REST route, and every
    context_builder._live_query branch).

    existing_connection/connection_box (Day 4, Capability 6, Task 4) —
    passed straight through to LiveQueryEngine.execute(); see its own
    docstring. Every existing caller that omits them gets byte-identical
    behavior to before these parameters existed.

    Runs _governance_recheck and enforces both halves of its result before
    any row is returned: unconfirmed PII (blocked_cols) blocks execution
    outright; confirmed PII (pii_aliases) is passed through to
    LiveQueryEngine.execute() for masking. No caller should call
    LiveQueryEngine.execute() directly with SQL derived from an NL question
    — doing so would apply masking but silently skip the unconfirmed-PII
    block.

    Returns (query_result, governance_warnings) — governance_warnings is the
    structured {"type", "severity", "message"} list from _governance_recheck.
    It is not merged into query_result automatically (query_result may be a
    bare QueryResult with no dependency on this module); callers that surface
    warnings to the user are responsible for merging it into their own
    response shape.

    execution_kind — passed straight through to LiveQueryEngine.execute().
    Defaults to "user_query" (identical behavior to before this parameter
    existed) for every existing caller. See its own docstring for the one
    other value in use ("insight_comparison", data.insight_service).
    """
    from core.live.query_engine import LiveQueryEngine
    from core.live.query_result import QueryResult, QueryStatus
    from core.perf import stage_timer

    with stage_timer.measure("governance_pii_recheck"):
        blocked_cols, pii_aliases, gov_warnings = _governance_recheck(
            source_id, user_id, sql_plan
        )

    if blocked_cols:
        execution_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        _write_audit(
            "query_execution", "governance_block", user_id, source_id,
            execution_id=execution_id,
        )
        blocked_result = QueryResult(
            execution_id=execution_id,
            status=QueryStatus.BLOCKED,
            source_id=source_id,
            executed_at=started_at.isoformat(),
            duration_ms=0,
            warnings=[w["message"] for w in gov_warnings],
            error=f"Blocked: unconfirmed PII column(s): {', '.join(blocked_cols)}",
        )
        return blocked_result, gov_warnings

    result = LiveQueryEngine().execute(
        source_id, user_id, sql,
        params=params, row_limit=row_limit, timeout_s=timeout_s,
        page=page, page_size=page_size, max_payload_bytes=max_payload_bytes,
        pii_aliases=pii_aliases, execution_kind=execution_kind,
        existing_connection=existing_connection, connection_box=connection_box,
    )
    return result, gov_warnings


# ---------------------------------------------------------------------------
# Connection loading
# ---------------------------------------------------------------------------

def _load_source_connection(source_id: int, user_id: str):
    """Load and decrypt the data source config, then open a DBAPI2 connection.

    Returns (db_conn, source_type).

    Raises:
      PermissionError  — source_id not found or not owned by user_id
      RuntimeError     — credential decryption failure
      ValueError       — no connector registered for the source_type
    """
    meta_conn = get_connection()
    try:
        row = meta_conn.execute(
            "SELECT source_type, encrypted_config_json "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        meta_conn.close()

    if row is None:
        raise PermissionError(
            f"Data source {source_id} not found or not owned by this user."
        )

    source_type = row["source_type"]

    try:
        params = json.loads(
            get_secret_manager().decrypt_secret(row["encrypted_config_json"])
        )
    except Exception as exc:
        raise RuntimeError("Failed to decrypt connection credentials.") from exc

    connector_cls = _registry.get(source_type)
    if connector_cls is None:
        raise ValueError(
            f"No connector registered for source_type '{source_type}'."
        )

    config  = DataSourceConfig(source_type=source_type, params=params)
    db_conn = connector_cls().open_connection(config)
    return db_conn, source_type


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def _serialize_value(v):
    """Convert a DBAPI2 cell value to a JSON-serializable Python type."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return "<binary>"
    return str(v)


def _execute_with_timeout(
    db_conn,
    sql: str,
    params: list,
    row_limit: int,
    timeout_s: int,
) -> tuple:
    """Run cursor.execute + cursor.fetchmany(row_limit+1) in a daemon thread.

    Returns (description, rows, error_str, timed_out).

    On timeout, closes db_conn to interrupt the in-progress ODBC/DB call,
    then returns (None, None, None, True).

    Never calls cursor.fetchall().
    """
    result: dict = {"description": None, "rows": None, "error": None}

    def _run() -> None:
        try:
            cursor = db_conn.cursor()
            cursor.execute(sql, params)
            result["description"] = cursor.description
            result["rows"]        = cursor.fetchmany(row_limit + 1)
        except Exception as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)

    if thread.is_alive():
        try:
            db_conn.close()
        except Exception:
            pass
        return None, None, None, True

    return result["description"], result["rows"], result["error"], False


def _build_columns(description, pii_aliases: set[str]) -> list[dict]:
    """Build the columns metadata list from cursor.description."""
    if not description:
        return []
    return [
        {
            "name": desc[0],
            "pii":  desc[0].lower() in pii_aliases,
        }
        for desc in description
    ]


def _build_rows(rows_raw, columns: list[dict]) -> list[dict]:
    """Serialize raw DBAPI2 rows, masking any confirmed PII columns."""
    result: list[dict] = []
    for raw_row in rows_raw:
        row: dict = {}
        for col_meta, cell_value in zip(columns, raw_row):
            if col_meta["pii"]:
                row[col_meta["name"]] = "***"
            else:
                row[col_meta["name"]] = _serialize_value(cell_value)
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def _write_audit(
    task_type: str,
    status: str,
    user_id: str,
    source_id: int,
    *,
    execution_id: str = "",
    row_count: int = 0,
    truncated: bool = False,
) -> None:
    """Write one audit event.  Never logs raw SQL or parameter values."""
    try:
        payload = json.dumps({
            "execution_id": execution_id,
            "source_id":    source_id,
            "row_count":    row_count,
            "truncated":    truncated,
        })
        log_audit_event(
            {
                "task_type":      task_type,
                "original_input": payload,
                "status":         status,
            },
            user_id=user_id,
        )
    except Exception:
        logger.warning(
            "_write_audit: log_audit_event failed [source_id=%s user_id=%s]",
            source_id, user_id,
        )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed_ms(started_at: datetime) -> int:
    return int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)


def _block_result(
    execution_id: str,
    source_id: int,
    started_at: datetime,
    reasons: list[str],
    warnings: list[dict],
) -> dict:
    return {
        "execution_id":     execution_id,
        "status":           "governance_block",
        "source_id":        source_id,
        "executed_at":      started_at.isoformat(),
        "duration_ms":      _elapsed_ms(started_at),
        "columns":          [],
        "rows":             [],
        "row_count":        0,
        "truncated":        False,
        "row_limit_applied": 0,
        "warnings":         warnings,
        "error":            "; ".join(reasons),
    }


def _error_result(
    execution_id: str,
    source_id: int,
    started_at: datetime,
    error: str,
    warnings: list[dict],
) -> dict:
    return {
        "execution_id":     execution_id,
        "status":           "failed",
        "source_id":        source_id,
        "executed_at":      started_at.isoformat(),
        "duration_ms":      _elapsed_ms(started_at),
        "columns":          [],
        "rows":             [],
        "row_count":        0,
        "truncated":        False,
        "row_limit_applied": 0,
        "warnings":         warnings,
        "error":            error,
    }


def _rate_limit_result(
    execution_id: str,
    source_id: int,
    started_at: datetime,
    error: str,
    warnings: list[dict],
) -> dict:
    return {
        "execution_id":      execution_id,
        "status":            "rate_limited",
        "source_id":         source_id,
        "executed_at":       started_at.isoformat(),
        "duration_ms":       _elapsed_ms(started_at),
        "columns":           [],
        "rows":              [],
        "row_count":         0,
        "truncated":         False,
        "row_limit_applied": 0,
        "warnings":          warnings,
        "error":             error,
    }


# ---------------------------------------------------------------------------
# Query execution audit log — Phase 6.2
# ---------------------------------------------------------------------------

def _extract_tables(sql_plan: dict) -> list[str]:
    """Return a sorted list of unique table FQNs referenced in sql_plan."""
    tables: set[str] = set()
    for sel in (sql_plan.get("select") or []):
        if sel.get("table_fqn"):
            tables.add(sel["table_fqn"])
    from_entry = sql_plan.get("from")
    if from_entry and from_entry.get("table_fqn"):
        tables.add(from_entry["table_fqn"])
    for j in (sql_plan.get("joins") or []):
        for key in ("left_table", "right_table"):
            if j.get(key):
                tables.add(j[key])
    return sorted(tables)


def _log_row_to_dict(row) -> dict:
    """Convert a sqlite3.Row from query_execution_log to a plain dict."""
    return {
        "id":              row["id"],
        "execution_id":    row["execution_id"],
        "user_id":         row["user_id"],
        "source_id":       row["source_id"],
        "sql_hash":        row["sql_hash"],
        "tables_accessed": json.loads(row["tables_accessed_json"] or "[]"),
        "param_count":     row["param_count"],
        "row_count":       row["row_count"],
        "truncated":       bool(row["truncated"]),
        "duration_ms":     row["duration_ms"],
        "status":          row["status"],
        "error_code":      row["error_code"],
        "executed_at":     row["executed_at"],
        "created_at":      row["created_at"],
    }


def log_query_execution(
    execution_id: str,
    user_id: str,
    source_id: int,
    sql: "str | None",
    sql_plan: dict,
    *,
    param_count: int,
    row_count: int,
    truncated: bool,
    duration_ms: int,
    status: str,
    error_code: "str | None",
    executed_at: str,
    execution_kind: str = "user_query",
) -> None:
    """Persist one row to query_execution_log.

    Security invariants:
      - Raw SQL is NEVER stored — only its SHA-256 hex digest.
      - Parameter values are NEVER stored — only the count.
      - Returned row values are NEVER stored.
      - PII values are NEVER stored.

    execution_kind distinguishes a real user-visible execution
    ("user_query", the default — every pre-existing call site keeps
    counting toward the per-user rate limit unchanged) from the agent's own
    internal investigation probes ("investigation" —
    data.investigation_service passes this explicitly), which are still
    persisted here for audit/analytics but must not themselves trigger
    _check_user_rate_limit for the real execution that follows them in the
    same turn. Daily/source-rate/repeated-query safeguards are unaffected —
    they intentionally keep counting every row regardless of kind.
    """
    try:
        sql_hash       = hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None
        tables_json    = json.dumps(_extract_tables(sql_plan))
        created_at_str = _now_iso()

        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO query_execution_log
                    (execution_id, user_id, source_id, sql_hash,
                     tables_accessed_json, param_count, row_count, truncated,
                     duration_ms, status, error_code, executed_at, created_at,
                     execution_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id, user_id, source_id, sql_hash,
                    tables_json, param_count, row_count, 1 if truncated else 0,
                    duration_ms, status, error_code, executed_at, created_at_str,
                    execution_kind,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "log_query_execution: DB write failed [execution_id=%s]", execution_id
        )


def get_query_execution_log(execution_id: str, user_id: str) -> "dict | None":
    """Return one query_execution_log row, enforcing user_id ownership."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM query_execution_log WHERE execution_id = ? AND user_id = ?",
            (execution_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return _log_row_to_dict(row) if row is not None else None


def list_query_executions(
    user_id: str,
    *,
    source_id: "int | None" = None,
    status: "str | None" = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """Return paginated query_execution_log rows for user_id, newest first."""
    parts:  list[str] = ["SELECT * FROM query_execution_log WHERE user_id = ?"]
    params: list      = [user_id]

    if source_id is not None:
        parts.append("AND source_id = ?")
        params.append(source_id)
    if status:
        parts.append("AND status = ?")
        params.append(status)

    parts.extend(["ORDER BY executed_at DESC", "LIMIT ? OFFSET ?"])
    params.extend([limit, offset])

    conn = get_connection()
    try:
        rows = conn.execute(" ".join(parts), params).fetchall()
    finally:
        conn.close()
    return [_log_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Operational safeguards — Phase 6.3
# All reads come from query_execution_log — no new table required.
# No raw SQL or parameter values are read or stored here.
# ---------------------------------------------------------------------------

def _check_user_rate_limit(user_id: str) -> bool:
    """Return True (blocked) if user has executed within the last RATE_LIMIT_WINDOW_S seconds.

    Only counts execution_kind='user_query' rows — the agent's own bounded
    internal investigation probes (data.investigation_service) write to this
    same table for audit/analytics but must never cause the real,
    user-visible execution that follows them in the same turn to
    self-rate-limit. Every other safeguard (daily limit, per-source rate,
    repeated-query protection) deliberately keeps counting all rows
    regardless of kind — this narrowing is specific to the 2-second
    per-user window.

    Returns False (fail-open) on any DB error so that infrastructure failures
    never block legitimate executions.
    """
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT executed_at FROM query_execution_log "
                "WHERE user_id = ? AND execution_kind = 'user_query' "
                "ORDER BY executed_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        last_dt = datetime.fromisoformat(row["executed_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() < RATE_LIMIT_WINDOW_S
    except Exception:
        return False  # fail-open: never block due to a safeguard's own DB error


def _check_daily_limit(user_id: str) -> int:
    """Return the number of executions recorded for user_id on today's UTC date.

    Returns 0 (fail-open) on any DB error.
    """
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM query_execution_log "
                "WHERE user_id = ? AND executed_at >= ?",
                (user_id, f"{today}T00:00:00"),
            ).fetchone()
        finally:
            conn.close()
        return int(row["cnt"]) if row else 0
    except Exception:
        return 0


def _check_source_rate(source_id: int) -> int:
    """Return the number of executions for source_id in the last 60 seconds.

    Returns 0 (fail-open) on any DB error.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM query_execution_log "
                "WHERE source_id = ? AND executed_at >= ?",
                (source_id, cutoff),
            ).fetchone()
        finally:
            conn.close()
        return int(row["cnt"]) if row else 0
    except Exception:
        return 0


def _check_repeated_query(user_id: str, sql_hash: "str | None") -> int:
    """Return how many times user_id has run sql_hash in the last REPEATED_QUERY_WINDOW_S seconds.

    Returns 0 if sql_hash is None or on any DB error.  Reads only hashes,
    never raw SQL or parameter values.
    """
    if not sql_hash:
        return 0
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=REPEATED_QUERY_WINDOW_S)
        ).isoformat()
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM query_execution_log "
                "WHERE user_id = ? AND sql_hash = ? AND executed_at >= ?",
                (user_id, sql_hash, cutoff),
            ).fetchone()
        finally:
            conn.close()
        return int(row["cnt"]) if row else 0
    except Exception:
        return 0


def get_execution_readiness(
    user_id: str,
    source_id: "int | None" = None,
) -> dict:
    """Return operational readiness metrics for a user's execution context.

    All values are derived from query_execution_log timestamps and hashes.
    No raw SQL or parameter values are read.
    """
    now         = datetime.now(timezone.utc)
    today_str   = now.date().isoformat()
    one_min_ago = (now - timedelta(seconds=60)).isoformat()
    five_min_ago = (now - timedelta(seconds=300)).isoformat()
    day_start   = f"{today_str}T00:00:00"

    _empty = {
        "executions_today": 0, "user_rate_limited": False,
        "daily_remaining": DAILY_LIMIT, "source_recent_count": 0,
        "recent_failures": 0, "recent_timeouts": 0,
        "repeated_query_warnings": 0,
    }
    try:
        conn = get_connection()
    except Exception:
        return _empty
    try:
        # executions today (all statuses)
        today_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM query_execution_log "
            "WHERE user_id = ? AND executed_at >= ?",
            (user_id, day_start),
        ).fetchone()
        executions_today = int(today_row["cnt"]) if today_row else 0

        # most recent execution timestamp (for rate-limit state)
        last_row = conn.execute(
            "SELECT executed_at FROM query_execution_log "
            "WHERE user_id = ? ORDER BY executed_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        user_rate_limited = False
        if last_row:
            try:
                last_dt = datetime.fromisoformat(last_row["executed_at"])
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                user_rate_limited = (now - last_dt).total_seconds() < RATE_LIMIT_WINDOW_S
            except Exception:
                pass

        # source executions in the last 60 seconds
        source_recent_count = 0
        if source_id is not None:
            src_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM query_execution_log "
                "WHERE source_id = ? AND executed_at >= ?",
                (source_id, one_min_ago),
            ).fetchone()
            source_recent_count = int(src_row["cnt"]) if src_row else 0

        # failed executions in the last 5 minutes
        fail_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM query_execution_log "
            "WHERE user_id = ? AND status = 'failed' AND executed_at >= ?",
            (user_id, five_min_ago),
        ).fetchone()
        recent_failures = int(fail_row["cnt"]) if fail_row else 0

        # timeout executions in the last 5 minutes
        timeout_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM query_execution_log "
            "WHERE user_id = ? AND status = 'timeout' AND executed_at >= ?",
            (user_id, five_min_ago),
        ).fetchone()
        recent_timeouts = int(timeout_row["cnt"]) if timeout_row else 0

        # sql_hash values run >= REPEATED_QUERY_THRESHOLD times in last 5 minutes
        rq_rows = conn.execute(
            "SELECT COUNT(*) AS hash_count FROM ("
            "  SELECT sql_hash FROM query_execution_log "
            "  WHERE user_id = ? AND sql_hash IS NOT NULL AND executed_at >= ? "
            "  GROUP BY sql_hash HAVING COUNT(*) >= ?"
            ")",
            (user_id, five_min_ago, REPEATED_QUERY_THRESHOLD),
        ).fetchone()
        repeated_query_warnings = int(rq_rows["hash_count"]) if rq_rows else 0

    except Exception:
        return _empty
    finally:
        conn.close()

    return {
        "executions_today":        executions_today,
        "user_rate_limited":       user_rate_limited,
        "daily_remaining":         max(0, DAILY_LIMIT - executions_today),
        "source_recent_count":     source_recent_count,
        "recent_failures":         recent_failures,
        "recent_timeouts":         recent_timeouts,
        "repeated_query_warnings": repeated_query_warnings,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def execute_generated_query(
    source_id: int,
    user_id: str,
    generated_sql_result: dict,
    sql_plan: dict,
) -> dict:
    """Execute a validated, generated SELECT statement against a connected data source.

    Inputs:
      source_id            — ID of the data_source_connections row
      user_id              — authenticated user; ownership is verified
      generated_sql_result — direct output of generate_sql()
      sql_plan             — direct output of build_sql_plan()

    Returns a result dict with keys:
      execution_id, status, source_id, executed_at, duration_ms,
      columns, rows, row_count, truncated, row_limit_applied, warnings, error

    status values:
      "success"          — query completed and results are returned
      "failed"           — connection failure, credential error, or query error
      "timeout"          — query exceeded DEFAULT_QUERY_TIMEOUT_S seconds
      "governance_block" — pre-execution safety or governance check failed
    """
    execution_id = str(uuid.uuid4())
    started_at   = datetime.now(timezone.utc)
    warnings: list[dict] = list(generated_sql_result.get("warnings") or [])
    param_count  = len(
        (generated_sql_result.get("parameters") or {}).get("values") or []
    )
    sql      = generated_sql_result.get("sql")
    sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None

    # ── STEP 0a: Per-user rate limit (RATE_LIMIT_WINDOW_S between executions) ─
    if _check_user_rate_limit(user_id):
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="rate_limited",
            error_code="user_rate_limit", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "rate_limited", user_id, source_id,
                     execution_id=execution_id)
        return _rate_limit_result(
            execution_id, source_id, started_at,
            f"Rate limit exceeded: 1 execution per {RATE_LIMIT_WINDOW_S} seconds per user.",
            warnings,
        )

    # ── STEP 0b: Daily execution limit ───────────────────────────────────────
    executions_today = _check_daily_limit(user_id)
    if executions_today >= DAILY_LIMIT:
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="rate_limited",
            error_code="daily_limit", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "rate_limited", user_id, source_id,
                     execution_id=execution_id)
        return _rate_limit_result(
            execution_id, source_id, started_at,
            f"Daily execution limit of {DAILY_LIMIT} reached for today.",
            warnings,
        )

    # ── STEP 0c: Per-source rate limit (SOURCE_RATE_PER_MINUTE / 60 seconds) ─
    if _check_source_rate(source_id) >= SOURCE_RATE_PER_MINUTE:
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="rate_limited",
            error_code="source_rate_limit", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "rate_limited", user_id, source_id,
                     execution_id=execution_id)
        return _rate_limit_result(
            execution_id, source_id, started_at,
            f"Source rate limit exceeded: {SOURCE_RATE_PER_MINUTE} executions per minute.",
            warnings,
        )

    # ── STEP 1: Safety gate (all checks before opening any connection) ────────
    # sql already extracted above (before rate limit checks)
    blocks = _safety_gate(sql, generated_sql_result, sql_plan)
    if blocks:
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="governance_block",
            error_code="safety_gate", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "governance_block", user_id, source_id,
                     execution_id=execution_id)
        return _block_result(execution_id, source_id, started_at, blocks, warnings)

    # ── STEP 2: Governance re-check per column ────────────────────────────────
    blocked_cols, pii_aliases, gov_warnings = _governance_recheck(
        source_id, user_id, sql_plan
    )
    warnings.extend(gov_warnings)
    if blocked_cols:
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="governance_block",
            error_code="pii_blocked", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "governance_block", user_id, source_id,
                     execution_id=execution_id)
        return _block_result(
            execution_id, source_id, started_at,
            [f"Blocked: unconfirmed PII column(s): {', '.join(blocked_cols)}"],
            warnings,
        )

    # ── STEP 3: Resolve row limit ─────────────────────────────────────────────
    plan_limit = (sql_plan.get("limits") or {}).get("row_limit") or DEFAULT_ROW_LIMIT
    row_limit  = min(int(plan_limit), MAX_ROW_LIMIT)
    params     = (generated_sql_result.get("parameters") or {}).get("values") or []

    # ── STEP 4: Open connection ───────────────────────────────────────────────
    db_conn = None
    try:
        db_conn, _source_type = _load_source_connection(source_id, user_id)
    except PermissionError as exc:
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="failed",
            error_code="permission_denied", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "failed", user_id, source_id,
                     execution_id=execution_id)
        return _error_result(execution_id, source_id, started_at, str(exc), warnings)
    except Exception:
        logger.exception(
            "execute_generated_query: connection failed [source_id=%s user_id=%s]",
            source_id, user_id,
        )
        _dur = _elapsed_ms(started_at)
        log_query_execution(
            execution_id, user_id, source_id, sql, sql_plan,
            param_count=param_count, row_count=0, truncated=False,
            duration_ms=_dur, status="failed",
            error_code="connection_failed", executed_at=started_at.isoformat(),
        )
        _write_audit("query_execution", "failed", user_id, source_id,
                     execution_id=execution_id)
        return _error_result(
            execution_id, source_id, started_at,
            "Failed to open database connection.",
            warnings,
        )

    try:
        # ── STEP 5: Execute with thread timeout ───────────────────────────────
        description, rows_raw, exec_error, timed_out = _execute_with_timeout(
            db_conn, sql, params, row_limit, DEFAULT_QUERY_TIMEOUT_S
        )

        if timed_out:
            _dur = _elapsed_ms(started_at)
            log_query_execution(
                execution_id, user_id, source_id, sql, sql_plan,
                param_count=param_count, row_count=0, truncated=False,
                duration_ms=_dur, status="timeout",
                error_code="timeout", executed_at=started_at.isoformat(),
            )
            _write_audit("query_execution", "timeout", user_id, source_id,
                         execution_id=execution_id)
            return {
                "execution_id":     execution_id,
                "status":           "timeout",
                "source_id":        source_id,
                "executed_at":      started_at.isoformat(),
                "duration_ms":      _dur,
                "columns":          [],
                "rows":             [],
                "row_count":        0,
                "truncated":        False,
                "row_limit_applied": row_limit,
                "warnings":         warnings,
                "error":            (
                    f"Query exceeded the {DEFAULT_QUERY_TIMEOUT_S}s execution timeout."
                ),
            }

        if exec_error:
            logger.error(
                "execute_generated_query: query error [source_id=%s]: %s",
                source_id, exec_error,
            )
            _dur = _elapsed_ms(started_at)
            log_query_execution(
                execution_id, user_id, source_id, sql, sql_plan,
                param_count=param_count, row_count=0, truncated=False,
                duration_ms=_dur, status="failed",
                error_code="query_error", executed_at=started_at.isoformat(),
            )
            _write_audit("query_execution", "failed", user_id, source_id,
                         execution_id=execution_id)
            return _error_result(
                execution_id, source_id, started_at, exec_error, warnings
            )

        # ── STEP 6: Row cap — never fetchall ──────────────────────────────────
        rows_raw  = rows_raw or []
        truncated = len(rows_raw) > row_limit
        rows_raw  = rows_raw[:row_limit]

        # ── STEP 7: Build column metadata ─────────────────────────────────────
        columns = _build_columns(description, pii_aliases)

        # ── STEP 8: Serialize and mask rows ───────────────────────────────────
        rows = _build_rows(rows_raw, columns)

    finally:
        # Always attempt to close; timeout handler may have already done so.
        if db_conn is not None:
            try:
                db_conn.close()
            except Exception:
                pass

    # ── STEP 9: Audit + execution log ────────────────────────────────────────
    _dur = _elapsed_ms(started_at)
    log_query_execution(
        execution_id, user_id, source_id, sql, sql_plan,
        param_count=param_count, row_count=len(rows), truncated=truncated,
        duration_ms=_dur, status="success",
        error_code=None, executed_at=started_at.isoformat(),
    )

    # ── STEP 10: Repeated-query detection (warn only, never block) ───────────
    repeat_count = _check_repeated_query(user_id, sql_hash)
    if repeat_count >= REPEATED_QUERY_THRESHOLD:
        window_min = REPEATED_QUERY_WINDOW_S // 60
        warnings.append({
            "type":     "repeated_query",
            "severity": "LOW",
            "message":  (
                f"Repeated query detected: this SQL has been executed "
                f"{repeat_count} time(s) in the last {window_min} minutes."
            ),
        })

    _write_audit("query_execution", "success", user_id, source_id,
                 execution_id=execution_id, row_count=len(rows), truncated=truncated)

    return {
        "execution_id":     execution_id,
        "status":           "success",
        "source_id":        source_id,
        "executed_at":      started_at.isoformat(),
        "duration_ms":      _dur,
        "columns":          columns,
        "rows":             rows,
        "row_count":        len(rows),
        "truncated":        truncated,
        "row_limit_applied": row_limit,
        "warnings":         warnings,
        "error":            None,
    }
