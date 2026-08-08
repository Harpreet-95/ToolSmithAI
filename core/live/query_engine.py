from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.live.connection_resolver import LiveConnectionResolver
from core.live.models import ResolutionStatus
from core.live.query_limits import resolve_limits
from core.live.query_result import QueryResult, QueryStatus
from core.live.query_validator import validate as validate_sql

logger = logging.getLogger(__name__)

# execution_id -> live DBAPI2 connection, while a query is in flight.
# Enables cancel(): closing the connection interrupts the blocked call, the
# same mechanism _execute_with_timeout already uses on natural timeout.
_RUNNING_LOCK = threading.Lock()
_RUNNING: dict[str, object] = {}


class LiveQueryEngine:
    """
    Read-only live SQL execution against enterprise databases.

    Reuses the safety-checked primitives already shipped in
    data.query_execution_service (thread-based timeout, fetchmany row cap,
    result serialization, rate limiting, audit logging) rather than
    reimplementing them. What this adds beyond that pipeline: executing an
    already-known, trusted raw SQL string directly (no business-query
    planning/generation stage), a broader read-only statement allowlist,
    pagination over the bounded result set, a payload-size guard, and
    cancel-by-execution-id.

    Never accepts SQL from end-user chat/NL input — callers must already
    know the exact SQL to run. Intended for trusted internal callers only.
    Does not persist results and does not cache.
    """

    def execute(
        self,
        source_id: Optional[int],
        user_id: Optional[str],
        sql: Optional[str],
        *,
        params: Optional[list] = None,
        row_limit: Optional[int] = None,
        timeout_s: Optional[int] = None,
        page: int = 1,
        page_size: Optional[int] = None,
        max_payload_bytes: Optional[int] = None,
        existing_connection: Optional[object] = None,
        connection_box: Optional[dict] = None,
    ) -> QueryResult:
        from data.query_execution_service import (
            DAILY_LIMIT,
            REPEATED_QUERY_THRESHOLD,
            SOURCE_RATE_PER_MINUTE,
            _build_columns,
            _build_rows,
            _check_daily_limit,
            _check_repeated_query,
            _check_source_rate,
            _check_user_rate_limit,
            _execute_with_timeout,
            _write_audit,
            log_query_execution,
        )
        import data.sql_dialects as sql_dialects

        execution_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        params = params or []
        limits = resolve_limits(row_limit, timeout_s, page, page_size, max_payload_bytes)
        sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None

        def _elapsed_ms() -> int:
            return int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

        def _log(status: str, *, row_count: int = 0, truncated: bool = False,
                  error_code: Optional[str] = None) -> int:
            dur = _elapsed_ms()
            try:
                log_query_execution(
                    execution_id, user_id, source_id, sql, {},
                    param_count=len(params), row_count=row_count, truncated=truncated,
                    duration_ms=dur, status=status, error_code=error_code,
                    executed_at=started_at.isoformat(),
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "LiveQueryEngine: log_query_execution failed [execution_id=%s]",
                    execution_id,
                )
            _write_audit(
                "live_query_execution", status, user_id, source_id,
                execution_id=execution_id, row_count=row_count, truncated=truncated,
            )
            return dur

        # ── Resolve connection (ownership, active state, capability) ──────────
        from core.perf import stage_timer

        with stage_timer.measure("live_connection_resolve"):
            resolution = LiveConnectionResolver().resolve(
                source_id, user_id, required_capability="sql_query"
            )
        if resolution.status != ResolutionStatus.RESOLVED:
            dur = _log("blocked", error_code=resolution.status.value)
            return QueryResult(
                execution_id=execution_id, status=QueryStatus.BLOCKED,
                source_id=source_id, executed_at=started_at.isoformat(),
                duration_ms=dur, error=resolution.message,
            )
        context = resolution.context

        # ── Rate limits (reused as-is) ─────────────────────────────────────────
        with stage_timer.measure("rate_limit_checks"):
            if _check_user_rate_limit(user_id):
                dur = _log("rate_limited", error_code="user_rate_limit")
                return QueryResult(
                    execution_id=execution_id, status=QueryStatus.RATE_LIMITED,
                    source_id=source_id, executed_at=started_at.isoformat(),
                    duration_ms=dur, error="Rate limit exceeded: too many executions in a short window.",
                )
            if _check_daily_limit(user_id) >= DAILY_LIMIT:
                dur = _log("rate_limited", error_code="daily_limit")
                return QueryResult(
                    execution_id=execution_id, status=QueryStatus.RATE_LIMITED,
                    source_id=source_id, executed_at=started_at.isoformat(),
                    duration_ms=dur, error=f"Daily execution limit of {DAILY_LIMIT} reached.",
                )
            if _check_source_rate(source_id) >= SOURCE_RATE_PER_MINUTE:
                dur = _log("rate_limited", error_code="source_rate_limit")
                return QueryResult(
                    execution_id=execution_id, status=QueryStatus.RATE_LIMITED,
                    source_id=source_id, executed_at=started_at.isoformat(),
                    duration_ms=dur,
                    error=f"Source rate limit exceeded: {SOURCE_RATE_PER_MINUTE} executions per minute.",
                )

        # ── Validate SQL (read-only enforcement) ───────────────────────────────
        dialect = sql_dialects.source_type_to_dialect(context.source_type)
        validation = validate_sql(sql, dialect)
        if not validation.is_valid:
            dur = _log("blocked", error_code="validation_failed")
            return QueryResult(
                execution_id=execution_id, status=QueryStatus.BLOCKED,
                source_id=source_id, executed_at=started_at.isoformat(),
                duration_ms=dur, error="; ".join(validation.blocking_reasons),
            )

        # ── Open connection ─────────────────────────────────────────────────────
        # Day 4, Capability 6 (Task 4) — existing_connection lets a caller that
        # already opened+authenticated a live connection for an earlier
        # governed query THIS SAME REQUEST (currently only data.insight_
        # service's period-comparison follow-up) reuse it instead of paying a
        # second live_connection_open round trip. Every safety check above
        # this point (ownership/capability resolution, rate limits, SQL
        # validation) still runs fresh on every call — only the already-
        # authenticated network connection object is reused, nothing governed
        # is skipped. Every existing caller that doesn't pass this parameter
        # gets byte-identical behavior: open a fresh connection, same as
        # before this parameter existed.
        if existing_connection is not None:
            db_conn = existing_connection
        else:
            try:
                with stage_timer.measure("live_connection_open"):
                    db_conn = context.connector_cls().open_connection(context.config)
            except Exception:  # noqa: BLE001 — includes stub connectors' NotImplementedError
                logger.warning(
                    "LiveQueryEngine: open_connection failed [source_id=%s]", source_id
                )
                dur = _log("failed", error_code="connection_failed")
                return QueryResult(
                    execution_id=execution_id, status=QueryStatus.FAILED,
                    source_id=source_id, executed_at=started_at.isoformat(),
                    duration_ms=dur, error="Failed to open database connection.",
                )

        with _RUNNING_LOCK:
            _RUNNING[execution_id] = db_conn

        try:
            with stage_timer.measure("sql_server_execution"):
                description, rows_raw, exec_error, timed_out = _execute_with_timeout(
                    db_conn, sql, params, limits.row_limit, limits.timeout_s
                )

            if timed_out:
                dur = _log("timeout", error_code="timeout")
                return QueryResult(
                    execution_id=execution_id, status=QueryStatus.TIMEOUT,
                    source_id=source_id, executed_at=started_at.isoformat(),
                    duration_ms=dur, row_limit_applied=limits.row_limit,
                    error=f"Query exceeded the {limits.timeout_s}s execution timeout.",
                )

            if exec_error:
                logger.error(
                    "LiveQueryEngine: query error [source_id=%s]: %s", source_id, exec_error
                )
                dur = _log("failed", error_code="query_error")
                return QueryResult(
                    execution_id=execution_id, status=QueryStatus.FAILED,
                    source_id=source_id, executed_at=started_at.isoformat(),
                    duration_ms=dur, error=exec_error,
                )

            rows_raw = rows_raw or []
            truncated = len(rows_raw) > limits.row_limit
            rows_raw = rows_raw[:limits.row_limit]
            columns = _build_columns(description, set())
            rows = _build_rows(rows_raw, columns)

            # ── Payload-size guard ────────────────────────────────────────────
            payload_truncated = False
            while rows and len(json.dumps(rows, default=str).encode("utf-8")) > limits.max_payload_bytes:
                rows = rows[: max(1, len(rows) // 2)]
                payload_truncated = True
            if payload_truncated:
                truncated = True

            # ── Pagination over the bounded result set ────────────────────────
            start = (limits.page - 1) * limits.page_size
            end = start + limits.page_size
            page_rows = rows[start:end]
            has_more = end < len(rows)

        finally:
            with _RUNNING_LOCK:
                _RUNNING.pop(execution_id, None)
            if connection_box is not None:
                # Caller (connection_box passed, non-None) takes ownership of
                # closing db_conn — it may still be reused for one more
                # governed query this same request. Storing it here even when
                # db_conn came in via existing_connection is a harmless
                # idempotent re-store of the same object, not a second open.
                connection_box["conn"] = db_conn
            else:
                with stage_timer.measure("live_connection_close"):
                    try:
                        db_conn.close()
                    except Exception:  # noqa: BLE001
                        pass

        with stage_timer.measure("post_execution_bookkeeping"):
            dur = _log("success", row_count=len(rows), truncated=truncated)

            warnings: list[str] = []
            if payload_truncated:
                warnings.append("Result payload exceeded the maximum size and was truncated.")
            repeat_count = _check_repeated_query(user_id, sql_hash)
            if repeat_count >= REPEATED_QUERY_THRESHOLD:
                warnings.append(f"Repeated query detected: executed {repeat_count} time(s) recently.")

        return QueryResult(
            execution_id=execution_id, status=QueryStatus.SUCCESS, source_id=source_id,
            executed_at=started_at.isoformat(), duration_ms=dur,
            columns=columns, rows=page_rows, row_count=len(rows),
            truncated=truncated, row_limit_applied=limits.row_limit,
            page=limits.page, page_size=limits.page_size, has_more=has_more,
            warnings=warnings,
        )

    def cancel(self, execution_id: str) -> bool:
        """Close the tracked connection for a running execution, if any."""
        with _RUNNING_LOCK:
            conn = _RUNNING.get(execution_id)
        if conn is None:
            return False
        try:
            conn.close()
            return True
        except Exception:  # noqa: BLE001
            return False
