"""
Translates profiling DB rows for a SQL data source into a DatasetProfile.

Read-only.  No writes to any table, no calls to profiling engines.

Usage::

    from core.profile_view.sql_adapter import build_profile_view_from_source

    profile = build_profile_view_from_source(source_id=3, user_id="u_42")
    if profile is None:
        # source does not belong to user
        ...

Reads:
    data_source_connections   — ownership check, display_name, source_type
    profiling_snapshots       — latest snapshot header + aggregate counters
    profiling_table_profiles  — per-table row/column counts
    profiling_column_profiles — per-column stats (data_type, semantic_type, nulls, …)
    profiling_value_samples   — non-PII sample values joined to column profiles
"""
from __future__ import annotations

import logging
from collections import defaultdict

from core.profile_view.models import ColumnProfileView, DatasetProfile, TableProfileView
from data.db import get_connection

logger = logging.getLogger(__name__)


def build_profile_view_from_source(
    source_id: int,
    user_id: str,
) -> DatasetProfile | None:
    """Build a DatasetProfile from the latest profiling snapshot for a SQL source.

    Returns:
        DatasetProfile on success.
        None if the source does not belong to user_id.

    Raises:
        ValueError if no profiling snapshot exists for the source.
    """
    # ── Ownership check ───────────────────────────────────────────────────────
    conn = get_connection()
    try:
        source_row = conn.execute(
            "SELECT id, display_name, source_type "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if source_row is None:
        return None

    # ── Latest profiling snapshot ─────────────────────────────────────────────
    conn = get_connection()
    try:
        snap_row = conn.execute(
            "SELECT * FROM profiling_snapshots "
            "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()

    if snap_row is None:
        raise ValueError(
            f"No profiling snapshot found for source id={source_id}. "
            "Run structural or full profiling first."
        )

    snap_id = snap_row["id"]
    generated_at: str = snap_row["completed_at"] or snap_row["created_at"] or ""

    # ── Table profiles ────────────────────────────────────────────────────────
    conn = get_connection()
    try:
        table_rows = conn.execute(
            "SELECT table_fqn, table_name, schema_name, "
            "exact_row_count, estimated_row_count, column_count "
            "FROM profiling_table_profiles "
            "WHERE profiling_snapshot_id = ? "
            "ORDER BY schema_name, table_name",
            (snap_id,),
        ).fetchall()
    finally:
        conn.close()

    # ── Column profiles ───────────────────────────────────────────────────────
    conn = get_connection()
    try:
        col_rows = conn.execute(
            "SELECT id, table_fqn, column_name, data_type, semantic_type, "
            "null_count, distinct_count, min_value, max_value "
            "FROM profiling_column_profiles "
            "WHERE profiling_snapshot_id = ? "
            "ORDER BY table_fqn, ordinal_position",
            (snap_id,),
        ).fetchall()
    finally:
        conn.close()

    # ── Value samples — JOIN avoids large IN clause ───────────────────────────
    # col_profile_id → list of sample value strings (max 10, PII cols excluded)
    col_samples: dict[int, list[str]] = defaultdict(list)
    conn = get_connection()
    try:
        sample_rows = conn.execute(
            "SELECT pcp.id AS col_id, pvs.value "
            "FROM profiling_column_profiles pcp "
            "JOIN profiling_value_samples pvs "
            "    ON pvs.profiling_column_profile_id = pcp.id "
            "WHERE pcp.profiling_snapshot_id = ? "
            "  AND pvs.value IS NOT NULL "
            "ORDER BY pcp.table_fqn, pcp.ordinal_position, pvs.sample_type, pvs.rank",
            (snap_id,),
        ).fetchall()
    finally:
        conn.close()

    for sr in sample_rows:
        bucket = col_samples[sr["col_id"]]
        if len(bucket) < 10:
            bucket.append(sr["value"])

    # ── Build column views keyed by table_fqn ────────────────────────────────
    columns_by_table: dict[str, list[ColumnProfileView]] = defaultdict(list)
    for cr in col_rows:
        columns_by_table[cr["table_fqn"]].append(ColumnProfileView(
            column_name=cr["column_name"],
            data_type=cr["data_type"] or "UNKNOWN",
            semantic_type=cr["semantic_type"],
            null_count=cr["null_count"],
            distinct_count=cr["distinct_count"],
            min_value=cr["min_value"],
            max_value=cr["max_value"],
            sample_values=col_samples.get(cr["id"], []),
        ))

    # ── Assemble table views ──────────────────────────────────────────────────
    tables: list[TableProfileView] = []
    for tr in table_rows:
        row_count = (
            tr["exact_row_count"]
            if tr["exact_row_count"] is not None
            else tr["estimated_row_count"]
        )
        tables.append(TableProfileView(
            table_fqn=tr["table_fqn"],
            row_count=row_count,
            column_count=tr["column_count"] or 0,
            columns=columns_by_table.get(tr["table_fqn"], []),
        ))

    # ── Summary metrics ───────────────────────────────────────────────────────
    summary: dict = {
        "snapshot_id":         snap_id,
        "snapshot_version":    snap_row["snapshot_version"],
        "profiling_mode":      snap_row["mode"],
        "profiling_status":    snap_row["status"],
        "total_tables":        snap_row["tables_total"] or len(table_rows),
        "tables_profiled":     snap_row["tables_profiled"] or 0,
        "columns_profiled":    snap_row["columns_profiled"] or 0,
        "pii_columns_found":   snap_row["pii_columns_found"] or 0,
        "total_rows_profiled": snap_row["total_rows_profiled"] or 0,
    }

    return DatasetProfile(
        source_type=source_row["source_type"],
        source_name=source_row["display_name"],
        generated_at=generated_at,
        tables=tables,
        summary_metrics=summary,
    )
