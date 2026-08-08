"""
Targeted, bounded column-level metadata preparation for the autonomous
live-query pipeline (Enterprise Phase 1 — AI-First Progressive Live Query
Pipeline).

Bridges the gap between table-level candidate resolution (which already
works purely from profiling_table_profiles / data_dictionary_tables — see
data.query_planning_service.plan_business_query) and column-level semantic
resolution (which needs profiling_column_profiles rows to select a measure
or dimension). The existing full-source paths — profiling_service.
run_structural_profiling/run_full_profiling and dictionary_service.
generate_and_save_dictionary — all derive column identity from the stored
schema_snapshots.snapshot_json blob; on a source that has never been
profiled, that blob has no column data at all, so re-running those
functions scoped to one table would still discover zero columns. This
module adds the one missing primitive: a single, bounded, per-table live
probe — reusing the exact connector-open + cursor-execute + fetchmany
pattern data.query_execution_service._execute_with_timeout already uses for
LiveQueryEngine — that learns real column names and a best-effort data type
from at most one real row, then persists the minimum profiling_column_profiles
rows the existing planner needs (query_planning_service._is_metric_column /
_is_dimension_column already fall back to schema-only classification —
numeric data_type + a metric-shaped column name — when no dictionary row
exists, so no dictionary write is required for the planner to use a
freshly-prepared column).

Never touches a table outside the caller-supplied list. Never scans more
than one row per table. Idempotent — a table that already carries
profiling_column_profiles rows is skipped, not re-probed.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.live.connection_resolver import LiveConnectionResolver
from core.live.models import ResolutionStatus
from data.db import get_connection
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

logger = logging.getLogger(__name__)

_PROBE_ROW_LIMIT = 1
_PROBE_TIMEOUT_S = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PreparationResult:
    """Outcome of one prepare_selected_tables() call.

    prepared: tables that had at least one profiling_column_profiles row
              newly written.
    skipped:  tables that already had column metadata — untouched.
    failed:   table_fqn -> reason, for tables that could not be prepared
              (not owned by the source, connection/probe failure, or a
              persistence error). Never raises for a per-table failure —
              one table's failure never blocks the others.
    """
    prepared: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.prepared)


def _owned_table_fqns(conn, source_id: int, table_fqns: list[str]) -> set[str]:
    """Real tables only — never invents one outside profiling_table_profiles /
    data_dictionary_tables, the same two stores plan_business_query's
    candidate resolution already trusts for table-level identity."""
    placeholders = ",".join("?" * len(table_fqns))
    rows = conn.execute(
        f"SELECT DISTINCT table_fqn FROM profiling_table_profiles "
        f"WHERE source_id = ? AND table_fqn IN ({placeholders}) "
        f"UNION "
        f"SELECT DISTINCT table_fqn FROM data_dictionary_tables "
        f"WHERE source_id = ? AND table_fqn IN ({placeholders})",
        (source_id, *table_fqns, source_id, *table_fqns),
    ).fetchall()
    return {r["table_fqn"] for r in rows}


def _has_column_metadata(conn, source_id: int, table_fqn: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM profiling_column_profiles WHERE source_id = ? AND table_fqn = ? LIMIT 1",
        (source_id, table_fqn),
    ).fetchone()
    return row is not None


def _infer_data_type(sample_value) -> str:
    """Best-effort, from one real sampled value — never fabricated. Uses the
    same normalized vocabulary as core.connectors.schema.ColumnInfo.data_type
    (TEXT | INTEGER | DECIMAL | DATETIME | BOOLEAN | BINARY) so downstream
    consumers (_is_metric_column's _NUMERIC_DATA_TYPES check, etc.) treat a
    freshly-probed column identically to one discovered through normal
    schema discovery."""
    if isinstance(sample_value, bool):
        return "BOOLEAN"
    if isinstance(sample_value, int):
        return "INTEGER"
    if isinstance(sample_value, (float, decimal.Decimal)):
        return "DECIMAL"
    if isinstance(sample_value, (_dt.date, _dt.datetime)):
        return "DATETIME"
    if isinstance(sample_value, (bytes, bytearray)):
        return "BINARY"
    return "TEXT"


def _probe_columns(context, table_fqn: str) -> list[dict] | None:
    """One bounded read against the live source — at most one row, purely to
    learn column names and a best-effort data type. Returns None on any
    connection or query failure rather than raising, so the caller can mark
    just this table as failed and continue with the others."""
    from data.query_execution_service import _execute_with_timeout

    if context.source_type == "mssql":
        from core.profiling.sql.mssql import _fqn, _nolock
        sql = f"SELECT TOP {_PROBE_ROW_LIMIT} * FROM {_fqn(table_fqn)}{_nolock(True)}"
    else:
        # No other relational connector is registered in this codebase yet;
        # a plain bounded SELECT is the safest generic fallback.
        sql = f"SELECT * FROM {table_fqn} LIMIT {_PROBE_ROW_LIMIT}"

    try:
        db_conn = context.connector_cls().open_connection(context.config)
    except Exception:  # noqa: BLE001
        logger.warning(
            "metadata_preparation_service: open_connection failed for table_fqn=%s",
            table_fqn,
        )
        return None

    try:
        description, rows, error, timed_out = _execute_with_timeout(
            db_conn, sql, [], _PROBE_ROW_LIMIT, _PROBE_TIMEOUT_S,
        )
    finally:
        try:
            db_conn.close()
        except Exception:  # noqa: BLE001
            pass

    if timed_out or error or not description:
        if error:
            logger.warning(
                "metadata_preparation_service: probe failed for table_fqn=%s: %s",
                table_fqn, error,
            )
        return None

    sample = rows[0] if rows else None
    columns = []
    for i, desc in enumerate(description):
        value = sample[i] if sample is not None else None
        columns.append({
            "column_name": desc[0],
            "data_type": _infer_data_type(value),
            "ordinal_position": i + 1,
        })
    return columns


def prepare_selected_tables(
    source_id: int,
    user_id: str,
    table_fqns: list[str],
) -> PreparationResult:
    """
    Prepare column-level metadata for exactly the caller-supplied tables —
    never any other table on the source. Verifies the source is owned by
    user_id and each table_fqn actually belongs to it before touching
    anything. Idempotent: a table already carrying profiling_column_profiles
    rows is skipped, not re-probed.
    """
    result = PreparationResult()
    table_fqns = list(dict.fromkeys(table_fqns))
    if not table_fqns:
        return result

    resolution = LiveConnectionResolver().resolve(
        source_id, user_id, required_capability="sql_query",
    )
    if resolution.status != ResolutionStatus.RESOLVED:
        for fqn in table_fqns:
            result.failed[fqn] = resolution.message
        return result
    context = resolution.context

    conn = get_connection()
    try:
        owned = _owned_table_fqns(conn, source_id, table_fqns)
        to_prepare = []
        for fqn in table_fqns:
            if fqn not in owned:
                result.failed[fqn] = "table does not belong to source"
                continue
            if _has_column_metadata(conn, source_id, fqn):
                result.skipped.append(fqn)
                continue
            to_prepare.append(fqn)

        if not to_prepare:
            return result

        snapshot = get_latest_profiling_snapshot(source_id, conn=conn)
    finally:
        conn.close()

    if snapshot is None:
        for fqn in to_prepare:
            result.failed[fqn] = "no profiling snapshot to attach column metadata to"
        return result

    for fqn in to_prepare:
        columns = _probe_columns(context, fqn)
        if not columns:
            result.failed[fqn] = "live column probe failed"
            continue

        conn = get_connection()
        try:
            now = _now()
            for col in columns:
                conn.execute(
                    "INSERT INTO profiling_column_profiles "
                    "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
                    " is_nullable, is_primary_key, is_identity, ordinal_position, "
                    " profiling_depth, profiling_status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, 0, 0, ?, 'STRUCTURAL_ONLY', 'COMPLETE', ?, ?) "
                    "ON CONFLICT(profiling_snapshot_id, table_fqn, column_name) DO NOTHING",
                    (
                        snapshot.id, source_id, fqn, col["column_name"], col["data_type"],
                        col["ordinal_position"], now, now,
                    ),
                )
            conn.commit()
            result.prepared.append(fqn)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.exception(
                "metadata_preparation_service: failed to persist columns for table_fqn=%s",
                fqn,
            )
            result.failed[fqn] = f"persistence failed: {exc}"
        finally:
            conn.close()

    return result
