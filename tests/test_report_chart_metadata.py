"""
Tests for core.reporting.chart_metadata — Executive Report Query Foundation
milestone, Part 11.

Run:
    python -m pytest tests/test_report_chart_metadata.py -v
"""
from __future__ import annotations

from core.reporting.chart_metadata import build_visual_from_result, recommend_visualization
from core.reporting.report_models import ChartDataset, KPICard, ReportTable


def test_no_dimension_recommends_kpi_card():
    assert recommend_visualization(has_dimension=False, has_calendar_dimension=False, num_measures=1) == "kpi_card"


def test_calendar_dimension_recommends_line_chart():
    assert recommend_visualization(has_dimension=True, has_calendar_dimension=True, num_measures=1) == "line_chart"


def test_non_calendar_dimension_recommends_bar_chart():
    assert recommend_visualization(has_dimension=True, has_calendar_dimension=False, num_measures=1) == "bar_chart"


def test_multi_measure_with_dimension_recommends_table():
    assert recommend_visualization(has_dimension=True, has_calendar_dimension=True, num_measures=2) == "table"


def test_incompatible_ai_hint_is_overridden_by_derived_shape():
    # AI suggested a bar_chart, but the actual shape is a bare scalar (no
    # dimension) — the derived shape wins, never the hint.
    assert recommend_visualization(
        has_dimension=False, has_calendar_dimension=False, num_measures=1, ai_hint="bar_chart",
    ) == "kpi_card"


def test_compatible_ai_hint_is_honored():
    assert recommend_visualization(
        has_dimension=True, has_calendar_dimension=True, num_measures=1, ai_hint="line_chart",
    ) == "line_chart"


def test_build_visual_scalar_produces_kpi_card():
    select_rows = [{"table_fqn": "dbo.clients", "column_name": "ClientID", "alias": "count_clientid", "aggregation": "COUNT"}]
    result_rows = [{"count_clientid": 42}]
    visual = build_visual_from_result("k1", "Client Count", select_rows, result_rows)
    assert isinstance(visual, KPICard)
    assert visual.value == 42


def test_build_visual_calendar_grouped_produces_line_chart():
    select_rows = [
        {"table_fqn": "dbo.orders", "column_name": "OrderDate", "alias": "orderdate_month", "aggregation": None, "time_grain": "month"},
        {"table_fqn": "dbo.orders", "column_name": "Revenue", "alias": "sum_revenue", "aggregation": "SUM"},
    ]
    result_rows = [
        {"orderdate_month": "2026-01", "sum_revenue": 1000},
        {"orderdate_month": "2026-02", "sum_revenue": 1500},
    ]
    visual = build_visual_from_result("k2", "Revenue by Month", select_rows, result_rows)
    assert isinstance(visual, ChartDataset)
    assert visual.chart_type == "line"
    assert visual.labels == ("2026-01", "2026-02")
    assert visual.series[0].data == (1000, 1500)


def test_build_visual_multi_measure_with_dimension_produces_table():
    select_rows = [
        {"table_fqn": "dbo.programs", "column_name": "ProgramName", "alias": "ProgramName", "aggregation": None},
        {"table_fqn": "dbo.clients", "column_name": "ClientID", "alias": "count_clientid", "aggregation": "COUNT"},
        {"table_fqn": "dbo.clients", "column_name": "CourseFee", "alias": "avg_coursefee", "aggregation": "AVG"},
    ]
    result_rows = [{"ProgramName": "STEM", "count_clientid": 12, "avg_coursefee": 450.0}]
    visual = build_visual_from_result("k3", "Program Detail", select_rows, result_rows)
    assert isinstance(visual, ReportTable)
    assert visual.columns == ("ProgramName", "count_clientid", "avg_coursefee")
