"""
core.profile_view
=================
Unified read-model for profiling results from any data source.

Public types::

    DatasetProfile    — top-level profile container
    TableProfileView  — per-table (or per-file) statistics
    ColumnProfileView — per-column statistics

Adapters::

    sql_adapter.build_profile_view_from_source(source_id, user_id)
        → DatasetProfile | None   (Microsoft SQL Server)
"""
from core.profile_view.models import ColumnProfileView, DatasetProfile, TableProfileView

__all__ = ["DatasetProfile", "TableProfileView", "ColumnProfileView"]
