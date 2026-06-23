"""
Translates JSON profile blobs stored in the `datasets` table into a DatasetProfile.

Read-only.  No writes to any table, no calls to profiling engines.

Usage::

    from core.profile_view.dataset_adapter import build_profile_view_from_dataset

    profile = build_profile_view_from_dataset(dataset_id=7, user_id="u_42")
    if profile is None:
        # dataset does not belong to user
        ...

Reads from the `datasets` row:
    columns_json             — ordered list of column names
    numeric_profile_json     — {col: {min, max, mean, std, median, …}}
    missing_values_json      — {col: null_count}
    categorical_profile_json — {col: [{value, count}, …]}
    semantic_profile_json    — [{column, semantic_type, confidence, …}]
    categorical_meta_json    — {col: {unique_count, …}}  (may be absent)
"""
from __future__ import annotations

import json
import logging

from core.profile_view.models import ColumnProfileView, DatasetProfile, TableProfileView
from data.db import get_connection

logger = logging.getLogger(__name__)


def _parse_json(raw: str | None) -> dict | list | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.debug("dataset_adapter: failed to parse JSON blob")
        return None


def _source_type_from_filename(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return "excel" if ext in {"xlsx", "xls", "xlsm"} else "csv"


def build_profile_view_from_dataset(
    dataset_id: int,
    user_id: str,
) -> DatasetProfile | None:
    """Build a DatasetProfile from a CSV/Excel dataset record.

    Returns:
        DatasetProfile on success.
        None if the dataset does not exist or does not belong to user_id.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?",
            (dataset_id, user_id),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    row = dict(row)

    # ── Parse stored JSON blobs ───────────────────────────────────────────────
    columns: list[str]          = _parse_json(row.get("columns_json")) or []
    numeric_profile: dict       = _parse_json(row.get("numeric_profile_json")) or {}
    missing_values: dict        = _parse_json(row.get("missing_values_json")) or {}
    categorical_profile: dict   = _parse_json(row.get("categorical_profile_json")) or {}
    semantic_items: list        = _parse_json(row.get("semantic_profile_json")) or []
    categorical_meta: dict      = _parse_json(row.get("categorical_meta_json")) or {}

    # ── Semantic lookup: col_name → descriptor dict ───────────────────────────
    sem_by_col: dict[str, dict] = {
        item["column"]: item
        for item in semantic_items
        if isinstance(item, dict) and "column" in item
    }

    # ── Build ColumnProfileView for every column ──────────────────────────────
    col_views: list[ColumnProfileView] = []
    for col in columns:
        is_numeric     = col in numeric_profile
        is_categorical = col in categorical_profile

        if is_numeric:
            data_type = "NUMERIC"
        elif is_categorical:
            data_type = "CATEGORICAL"
        else:
            data_type = "UNKNOWN"

        sem_item      = sem_by_col.get(col, {})
        semantic_type = sem_item.get("semantic_type") or None

        raw_null   = missing_values.get(col)
        null_count = int(raw_null) if raw_null is not None else None

        meta             = categorical_meta.get(col) or {}
        raw_distinct     = meta.get("unique_count")
        distinct_count   = int(raw_distinct) if raw_distinct is not None else None

        np_stats  = numeric_profile.get(col) or {}
        min_value = str(np_stats["min"]) if np_stats.get("min") is not None else None
        max_value = str(np_stats["max"]) if np_stats.get("max") is not None else None

        # Top categorical values become sample_values; numeric cols have none here
        cat_top = categorical_profile.get(col) or []
        sample_values = [
            str(entry["value"])
            for entry in cat_top
            if isinstance(entry, dict) and entry.get("value") is not None
        ][:5]

        col_views.append(ColumnProfileView(
            column_name=col,
            data_type=data_type,
            semantic_type=semantic_type,
            null_count=null_count,
            distinct_count=distinct_count,
            min_value=min_value,
            max_value=max_value,
            sample_values=sample_values,
        ))

    # ── Single table: the file itself ─────────────────────────────────────────
    filename: str = row.get("filename") or ""
    table_view = TableProfileView(
        table_fqn=filename,
        row_count=row.get("row_count"),
        column_count=row.get("column_count") or len(col_views),
        columns=col_views,
    )

    # ── Summary metrics ───────────────────────────────────────────────────────
    summary: dict = {
        "dataset_id":           dataset_id,
        "total_columns":        len(col_views),
        "numeric_columns":      sum(1 for c in col_views if c.data_type == "NUMERIC"),
        "categorical_columns":  sum(1 for c in col_views if c.data_type == "CATEGORICAL"),
        "columns_with_nulls":   sum(1 for c in col_views if (c.null_count or 0) > 0),
        "total_rows":           row.get("row_count"),
    }

    return DatasetProfile(
        source_type=_source_type_from_filename(filename),
        source_name=filename,
        generated_at=row.get("uploaded_at") or "",
        tables=[table_view],
        summary_metrics=summary,
    )
