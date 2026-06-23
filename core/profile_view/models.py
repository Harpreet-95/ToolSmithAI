"""
Unified read-model for profiling results from any data source.

This layer is a pure adapter: it translates existing profiling outputs
from SQL sources (profiling_* DB tables) and file-based datasets
(datasets.numeric_profile_json, categorical_profile_json, semantic_profile_json)
into a single, source-agnostic structure.

Future consumers that should migrate to DatasetProfile instead of
reading source-specific structures directly:

  - Report Generator   (core/tools/report_generator.py)
  - AI Copilot         (routes: /ask-report, /ask-dataset)
  - Business Glossary  (data/dictionary_service.py)
  - Quality Rules      (future quality gate engine)
  - Metadata APIs      (GET /sources/{id}/profile)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ColumnProfileView:
    """Normalised per-column statistics — source-agnostic.

    Supports SQL Server, PostgreSQL, MySQL, CSV, and Excel sources.
    Fields are None when the profiling depth did not reach that metric
    (e.g. STRUCTURAL_ONLY snapshots have no null_count).
    """
    column_name: str
    data_type: str                      # normalised type label; 'UNKNOWN' when unavailable
    semantic_type: str | None = None    # e.g. 'EMAIL', 'AMOUNT', 'DATE', 'unknown'
    null_count: int | None = None
    distinct_count: int | None = None
    min_value: str | None = None        # always str; consumers parse as needed
    max_value: str | None = None
    sample_values: list[str] = field(default_factory=list)  # max 10; empty for PII cols


@dataclass
class TableProfileView:
    """Normalised per-table (or per-file) statistics.

    For file-based sources (CSV, Excel) table_fqn is the uploaded filename.
    row_count is None when the profiling snapshot is STRUCTURAL_ONLY.
    """
    table_fqn: str
    row_count: int | None
    column_count: int
    columns: list[ColumnProfileView] = field(default_factory=list)


@dataclass
class DatasetProfile:
    """Top-level unified profile for a data source or uploaded dataset.

    Built exclusively by sql_adapter.build_profile_view_from_source() or
    dataset_adapter.build_profile_view_from_dataset().  Never constructed
    directly by profiling engines, routes, or report generators.

    source_type values:
        SQL   — 'mssql' | 'postgresql' | 'mysql'
        File  — 'csv'   | 'excel'

    summary_metrics holds source-specific aggregate counts (snapshot_id,
    tables_profiled, pii_columns_found, etc.) and is intentionally untyped
    so each adapter can surface the metrics most relevant to its source.
    """
    source_type: str
    source_name: str            # display_name (SQL) or filename (CSV/Excel)
    generated_at: str           # ISO-8601 timestamp of the profiling snapshot
    tables: list[TableProfileView] = field(default_factory=list)
    summary_metrics: dict[str, Any] = field(default_factory=dict)
