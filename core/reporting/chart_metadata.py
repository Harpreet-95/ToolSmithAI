"""
Executive Report Query Foundation milestone, Part 11 — bounded, deterministic
chart-type recommendation and result-to-visual conversion.

Only four output shapes are ever produced: kpi_card | line_chart | bar_chart
| table (core.reporting.kpi_spec.VisualizationHint). A KPIQuerySpec's own
visualization_hint is a suggestion only — recommend_visualization always
re-derives the shape from the ACTUAL validated sql_plan/result shape and
only honors the hint when it is compatible with that shape, exactly mirroring
frontend/src/components/ChartSection.jsx's own "explicit chart_type still
gets schema-checked" discipline, just enforced server-side, before the
frontend ever sees the data. Chart data is built ONLY from governed SQL
result rows — never from AI-generated numbers.
"""
from __future__ import annotations

from core.reporting.report_models import ChartDataset, ChartSeries, KPICard, ReportTable

_VISUAL_KINDS = frozenset({"kpi_card", "line_chart", "bar_chart", "table"})


def recommend_visualization(
    *, has_dimension: bool, has_calendar_dimension: bool, num_measures: int,
    ai_hint: str | None = None,
) -> str:
    """Deterministic mapping from a validated plan's own shape to a bounded
    visualization kind:
      - no dimension at all              -> kpi_card   (single scalar per measure)
      - calendar-grain dimension present -> line_chart  (time series)
      - non-calendar dimension present   -> bar_chart   (categorical comparison/ranking)
      - more than one measure AND a dimension -> table  (multi-column detail;
        a bar/line chart is single-series per this milestone's bounded scope)
    `ai_hint`, when supplied, is honored only when it agrees with what the
    shape independently implies — never trusted blindly."""
    if not has_dimension:
        derived = "kpi_card"
    elif num_measures > 1:
        derived = "table"
    elif has_calendar_dimension:
        derived = "line_chart"
    else:
        derived = "bar_chart"

    if ai_hint in _VISUAL_KINDS and ai_hint == derived:
        return ai_hint
    return derived


def build_visual_from_result(
    kpi_id: str, title: str, select_rows: list[dict], result_rows: list[dict],
    *, ai_hint: str | None = None,
) -> KPICard | ChartDataset | ReportTable:
    """Converts one KPI's validated sql_plan["select"] shape + governed
    execution rows into the visual its own shape recommends. `select_rows`
    is data.sql_planning_service.build_sql_plan's own sql_plan["select"]
    list (each row: table_fqn/column_name/alias/aggregation/time_grain);
    `result_rows` is the governed execution result (list of dicts keyed by
    alias) — the same contract core/answering/result_formatter.py already
    relies on."""
    dimension_rows = [r for r in select_rows if not r.get("aggregation")]
    measure_rows = [r for r in select_rows if r.get("aggregation")]
    has_calendar = any(r.get("time_grain") for r in dimension_rows)

    kind = recommend_visualization(
        has_dimension=bool(dimension_rows), has_calendar_dimension=has_calendar,
        num_measures=len(measure_rows), ai_hint=ai_hint,
    )

    if kind == "kpi_card":
        alias = (measure_rows[0]["alias"] if measure_rows else None)
        value = result_rows[0].get(alias) if result_rows and alias else None
        return KPICard(id=kpi_id, title=title, value=value)

    dim_alias = dimension_rows[0]["alias"] if dimension_rows else None
    labels = tuple(str(r.get(dim_alias, "")) for r in result_rows) if dim_alias else ()

    if kind == "table":
        columns = tuple(r["alias"] for r in select_rows)
        rows = tuple(dict(r) for r in result_rows)
        return ReportTable(id=kpi_id, title=title, columns=columns, rows=rows)

    chart_type = "line" if kind == "line_chart" else "bar"
    series = tuple(
        ChartSeries(
            name=m["alias"],
            data=tuple(r.get(m["alias"]) for r in result_rows),
        )
        for m in measure_rows
    )
    return ChartDataset(id=kpi_id, title=title, chart_type=chart_type, labels=labels, series=series)
