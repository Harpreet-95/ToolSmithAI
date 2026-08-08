"""
Targeted Investigation Tool — Enterprise AI Analyst Agent, Milestone M-29.

A tightly bounded, read-only capability the agent (core.orchestrator.agent)
may use when existing metadata cannot answer a narrow, specific uncertainty
about real column values — what distinct values a status/category column
actually holds, what date range a column actually covers, whether a
null-rate is high enough to matter, or whether a table/column genuinely
exists. This is deliberately NOT profiling (data.profiling_service — full,
multi-column, persisted statistics) and NOT schema discovery (data.schema_service
— structural metadata harvesting): it is one bounded live probe per call,
never persisted, never touching more than one table/column, always capped at
MAX_INVESTIGATION_ROWS rows.

Only four fixed SQL shapes exist (distinct_values / min_max / null_rate /
column_exists) — the SQL text is always built from a template via the same
DialectAdapter + sql_generation_service._qcol/_qfqn quoting helpers real
business-question SQL already uses. There is no code path that accepts or
assembles free-form SQL text from any caller, LLM-authored or otherwise.

Reuses, never reimplements:
  - core.live.connection_resolver.LiveConnectionResolver.resolve() — the same
    ownership + live_query_enabled gate data.metadata_preparation_service's
    probe already uses.
  - data.business_knowledge_service.get_column_business_context() /
    get_table_business_context() — the same structured PII/approval read
    data.sql_planning_service._check_pii_and_approval already uses for a
    real business query's selected columns. An unconfirmed-PII column is
    always refused here, with no allow_unconfirmed_pii override — unlike a
    planned business query, an investigation has no user-visible business
    justification recorded for why that specific column was necessary.
  - core.live.query_validator.validate() — the exact same read-only SQL
    validator core.live.query_engine.LiveQueryEngine.execute() already runs,
    as defense-in-depth on top of the fixed templates below.
  - data.query_execution_service._execute_with_timeout() / _write_audit() /
    log_query_execution() — the same bounded thread-timeout execution and
    audit-log writer (SHA-256 SQL hash only, no raw SQL/values/PII ever
    persisted) every other governed query already uses.

Never allowed, structurally: SELECT *, an unbounded row count, DDL/DML (the
templates below contain none, and validate_sql() would refuse them anyway),
or any table/column the caller does not explicitly name.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MAX_INVESTIGATION_ROWS = 20
_INVESTIGATION_TIMEOUT_S = 15

_SUPPORTED_TYPES = frozenset({"distinct_values", "min_max", "null_rate", "column_exists"})


@dataclass(frozen=True)
class InvestigationResult:
    """Typed, bounded return contract. sample_values is capped at
    MAX_INVESTIGATION_ROWS (or 2 for min_max/null_rate's fixed two-value
    shape) and contains only already-serialized scalar values — never a raw
    row dict, never a full table sample, never a secret/connection detail."""
    investigation_type: str
    table: str
    column: Optional[str]
    reason: str
    summary: str
    row_count: int
    sample_values: list
    duration_ms: float
    warnings: list[str] = field(default_factory=list)
    valid: bool = False


def _refuse(
    investigation_type: str, table_fqn: str, column_name: Optional[str],
    reason_code: str, message: str, *, duration_ms: float = 0.0,
) -> InvestigationResult:
    logger.info(
        "investigation_service: refused investigation_type=%s table=%s column=%s reason=%s",
        investigation_type, table_fqn, column_name, reason_code,
    )
    return InvestigationResult(
        investigation_type=investigation_type, table=table_fqn, column=column_name,
        reason=reason_code, summary=message, row_count=0, sample_values=[],
        duration_ms=duration_ms, warnings=[message], valid=False,
    )


def _verify_investigatable_column(source_id: int, user_id: str, table_fqn: str, column_name: str) -> Optional[str]:
    """Returns an error message if the column cannot be safely investigated,
    None if it's clear to proceed. Never trusts the caller's table_fqn/
    column_name blindly — re-verifies ownership, existence, and PII/approval
    state independently, the same way every other governed SQL layer in this
    codebase re-verifies rather than trusting an upstream caller."""
    from data.business_knowledge_service import get_column_business_context

    ctx = get_column_business_context(source_id, user_id, table_fqn, column_name)
    if ctx is None:
        return "Data source not found or not owned by this user."

    dic = ctx.get("dictionary")
    prof = ctx.get("profiling")
    if dic is None and prof is None:
        return f"{table_fqn}.{column_name} was not found in this source's metadata."

    pii_flagged = bool((prof and prof.get("pii_name_heuristic")) or (dic and dic.get("pii_risk")))
    confirmed = bool(prof and prof.get("pii_confirmed"))
    if pii_flagged and not confirmed:
        return f"{table_fqn}.{column_name} is flagged as unconfirmed PII — investigation is blocked."

    return None


def _build_sql(investigation_type: str, table_fqn: str, column_name: str, adapter) -> str:
    from data.sql_generation_service import _qcol, _qfqn

    col = _qcol(table_fqn, column_name, adapter)
    fqn = _qfqn(table_fqn, adapter)

    if investigation_type == "distinct_values":
        prefix = adapter.row_limit_prefix(MAX_INVESTIGATION_ROWS)
        suffix = adapter.row_limit_suffix(MAX_INVESTIGATION_ROWS)
        sql = f"SELECT DISTINCT {prefix}{col} FROM {fqn}"
        return f"{sql} {suffix}".rstrip() if suffix else sql
    if investigation_type == "min_max":
        return f"SELECT MIN({col}) AS min_value, MAX({col}) AS max_value FROM {fqn}"
    if investigation_type == "null_rate":
        return (
            f"SELECT COUNT(*) AS total_count, "
            f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) AS null_count FROM {fqn}"
        )
    raise ValueError(f"Unsupported investigation_type: {investigation_type!r}")  # unreachable — caller pre-checked


def inspect_targeted_values(
    source_id: int,
    user_id: str,
    table_fqn: str,
    column_name: Optional[str],
    *,
    investigation_type: str,
    reason: str,
) -> InvestigationResult:
    """
    One bounded, read-only, single-column investigation. Callers (the agent)
    are responsible for restricting table_fqn/column_name to a table and
    column already part of the current plan — this module independently
    re-verifies ownership, existence, and PII/approval state regardless, and
    never touches more than the one named table/column.

    investigation_type:
      - "distinct_values": SELECT DISTINCT {column} FROM {table}, capped at
        MAX_INVESTIGATION_ROWS rows. Never SELECT * — one named column only.
      - "min_max": SELECT MIN({column}), MAX({column}) FROM {table} — one row.
      - "null_rate": SELECT COUNT(*), COUNT of NULLs FROM {table} — one row.
      - "column_exists": metadata-only (no live connection is opened, no
        live_query_enabled check applies, nothing is executed or audited) —
        pass column_name=None to check table existence instead.

    reason is a short, caller-supplied justification recorded on the result
    and (by the caller) on the AgentTrace — never returned data, never a
    secret.
    """
    started = time.monotonic()

    if investigation_type not in _SUPPORTED_TYPES:
        return _refuse(
            investigation_type, table_fqn, column_name, "unsupported_investigation_type",
            f"Unsupported investigation_type: {investigation_type!r}.",
        )

    if investigation_type != "column_exists" and not column_name:
        return _refuse(
            investigation_type, table_fqn, column_name, "missing_column",
            "column_name is required for this investigation_type.",
        )

    if investigation_type == "column_exists":
        if column_name:
            error = _verify_investigatable_column(source_id, user_id, table_fqn, column_name)
        else:
            from data.business_knowledge_service import get_table_business_context
            ctx = get_table_business_context(source_id, user_id, table_fqn)
            if ctx is None:
                error = "Data source not found or not owned by this user."
            elif ctx.get("dictionary") is None and ctx.get("profiling") is None:
                error = f"{table_fqn} was not found in this source's metadata."
            else:
                error = None
        duration_ms = (time.monotonic() - started) * 1000
        if error:
            return _refuse(investigation_type, table_fqn, column_name, "not_found", error, duration_ms=duration_ms)
        target = f"{table_fqn}.{column_name}" if column_name else table_fqn
        return InvestigationResult(
            investigation_type=investigation_type, table=table_fqn, column=column_name,
            reason=reason, summary=f"{target} exists.", row_count=0, sample_values=[],
            duration_ms=duration_ms, warnings=[], valid=True,
        )

    # All remaining investigation types require a live, governed read.
    error = _verify_investigatable_column(source_id, user_id, table_fqn, column_name)
    if error:
        return _refuse(
            investigation_type, table_fqn, column_name, "column_not_investigatable", error,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    from core.live.connection_resolver import LiveConnectionResolver
    from core.live.models import ResolutionStatus

    resolution = LiveConnectionResolver().resolve(source_id, user_id, required_capability="sql_query")
    if resolution.status != ResolutionStatus.RESOLVED:
        return _refuse(
            investigation_type, table_fqn, column_name, "not_authorized", resolution.message,
            duration_ms=(time.monotonic() - started) * 1000,
        )
    context = resolution.context

    from data.sql_dialects import get_adapter, source_type_to_dialect
    from core.live.query_validator import validate as validate_sql

    dialect = source_type_to_dialect(context.source_type)
    adapter = get_adapter(dialect)
    sql = _build_sql(investigation_type, table_fqn, column_name, adapter)

    validation = validate_sql(sql, dialect)
    if not validation.is_valid:
        # Defense-in-depth only — every template above is read-only by
        # construction; this should never actually trip.
        return _refuse(
            investigation_type, table_fqn, column_name, "sql_validation_failed",
            "; ".join(validation.blocking_reasons),
            duration_ms=(time.monotonic() - started) * 1000,
        )

    try:
        db_conn = context.connector_cls().open_connection(context.config)
    except Exception:  # noqa: BLE001
        logger.warning("investigation_service: open_connection failed for table_fqn=%s", table_fqn)
        return _refuse(
            investigation_type, table_fqn, column_name, "connection_failed",
            "Could not open a connection to the data source.",
            duration_ms=(time.monotonic() - started) * 1000,
        )

    from data.query_execution_service import _execute_with_timeout, _serialize_value, _write_audit, log_query_execution

    row_cap = MAX_INVESTIGATION_ROWS if investigation_type == "distinct_values" else 1
    try:
        description, rows, exec_error, timed_out = _execute_with_timeout(
            db_conn, sql, [], row_cap, _INVESTIGATION_TIMEOUT_S,
        )
    finally:
        try:
            db_conn.close()
        except Exception:  # noqa: BLE001
            pass

    duration_ms = (time.monotonic() - started) * 1000
    execution_id = str(uuid.uuid4())
    # Minimal synthetic sql_plan — only ever read by log_query_execution's
    # own _extract_tables() to populate the audit log's tables_accessed_json;
    # never a real sql_plan and never used for anything else.
    synthetic_sql_plan = {"select": [{"table_fqn": table_fqn}], "from": {"table_fqn": table_fqn}, "joins": []}
    executed_at = datetime.now(timezone.utc).isoformat()

    def _audit(status: str, *, row_count: int = 0, error_code: Optional[str] = None) -> None:
        _write_audit("investigation", status, user_id, source_id, execution_id=execution_id, row_count=row_count)
        log_query_execution(
            execution_id, user_id, source_id, sql, synthetic_sql_plan,
            param_count=0, row_count=row_count, truncated=False,
            duration_ms=int(duration_ms), status=status, error_code=error_code, executed_at=executed_at,
            execution_kind="investigation",
        )

    if timed_out:
        _audit("timeout", error_code="investigation_timeout")
        return _refuse(
            investigation_type, table_fqn, column_name, "timeout",
            f"Investigation query exceeded the {_INVESTIGATION_TIMEOUT_S}s timeout.",
            duration_ms=duration_ms,
        )

    if exec_error or description is None:
        _audit("failed", error_code="investigation_query_failed")
        return _refuse(
            investigation_type, table_fqn, column_name, "query_failed",
            exec_error or "Investigation query failed.", duration_ms=duration_ms,
        )

    rows = rows[:row_cap]
    row_count = len(rows)
    _audit("success", row_count=row_count)

    # Defensive: a well-formed DB always returns exactly as many cells as the
    # fixed template selects, but never index into a row that came back
    # short (a misbehaving driver/connector) — pad with None rather than
    # raising, so a malformed result degrades to "no value observed" instead
    # of crashing the caller.
    def _cell(row, index):
        return row[index] if row is not None and len(row) > index else None

    if investigation_type == "distinct_values":
        sample_values = [_serialize_value(_cell(row, 0)) for row in rows]
        summary = f"{row_count} distinct value(s) sampled (capped at {MAX_INVESTIGATION_ROWS})."
    elif investigation_type == "min_max":
        first_row = rows[0] if rows else None
        min_v, max_v = _cell(first_row, 0), _cell(first_row, 1)
        sample_values = [_serialize_value(min_v), _serialize_value(max_v)]
        summary = f"min={sample_values[0]!r}, max={sample_values[1]!r}."
    else:  # null_rate
        first_row = rows[0] if rows else None
        total, nulls = _cell(first_row, 0) or 0, _cell(first_row, 1) or 0
        rate = (nulls / total) if total else 0.0
        sample_values = [_serialize_value(total), _serialize_value(nulls)]
        summary = f"{nulls} null(s) out of {total} row(s) ({rate:.1%})."

    return InvestigationResult(
        investigation_type=investigation_type, table=table_fqn, column=column_name,
        reason=reason, summary=summary, row_count=row_count, sample_values=sample_values,
        duration_ms=duration_ms, warnings=[], valid=True,
    )
