"""
Executive Report Query Foundation milestone, Part 10 — the structured
executive-report result model.

Every field here is populated ONLY from governed query results (SQL
execution rows, or core.reporting.comparison's deterministic arithmetic) —
never parsed out of AI narrative prose. core.reporting.narrative (Part 12)
receives an already-built ExecutiveReportData and may only ADD a short text
summary field; it never edits the numeric content here.

chart_type/series/labels intentionally reuse the SAME shape
core/answering/result_formatter.py._build_chart_spec and
core/tools/report_generator.py._build_chart_sections already produce
({"chart_type", "labels", "series": [{"name", "data"}]}) — the one
frontend/src/components/ChartSection.jsx already renders — so the frontend
needs no new chart-parsing code for report charts (Part 15).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

ChartType = Literal["line", "bar"]


@dataclass(frozen=True)
class KPICard:
    id: str
    title: str
    value: float | int | None
    unit: str | None = None
    previous_value: float | int | None = None
    absolute_change: float | None = None
    percent_change: float | None = None
    direction: Literal["up", "down", "flat", "unknown"] | None = None
    comparison_label: str | None = None  # e.g. "vs last month"
    warning: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "value": self.value, "unit": self.unit,
            "previous_value": self.previous_value, "absolute_change": self.absolute_change,
            "percent_change": self.percent_change, "direction": self.direction,
            "comparison_label": self.comparison_label, "warning": self.warning,
        }


@dataclass(frozen=True)
class ChartSeries:
    name: str
    data: tuple[float | int | None, ...]


@dataclass(frozen=True)
class ChartDataset:
    id: str
    title: str
    chart_type: ChartType
    labels: tuple[str, ...]
    series: tuple[ChartSeries, ...]
    unit: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "chart_type": self.chart_type,
            "labels": list(self.labels),
            "series": [{"name": s.name, "data": list(s.data)} for s in self.series],
            "unit": self.unit,
        }


@dataclass(frozen=True)
class ReportTable:
    id: str
    title: str
    columns: tuple[str, ...]
    rows: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "columns": list(self.columns), "rows": [dict(r) for r in self.rows]}


@dataclass(frozen=True)
class KPITrace:
    """Part 14/9 traceability — every displayed metric must trace back to
    exactly one KPIQuerySpec and the physical plan that answered it."""
    kpi_id: str
    business_question: str
    selected_tables: tuple[str, ...]
    ai_plan_valid: bool
    legacy_fallback_used: bool
    sql: str | None
    execution_status: Literal["executed", "skipped_unsafe", "failed"]
    skip_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "kpi_id": self.kpi_id, "business_question": self.business_question,
            "selected_tables": list(self.selected_tables), "ai_plan_valid": self.ai_plan_valid,
            "legacy_fallback_used": self.legacy_fallback_used, "sql": self.sql,
            "execution_status": self.execution_status, "skip_reason": self.skip_reason,
        }


@dataclass
class ExecutiveReportData:
    title: str
    source_id: int
    subtitle: str = ""
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    kpi_cards: list[KPICard] = field(default_factory=list)
    charts: list[ChartDataset] = field(default_factory=list)
    tables: list[ReportTable] = field(default_factory=list)
    narrative: str | None = None
    warnings: list[str] = field(default_factory=list)
    trace: list[KPITrace] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    # FINAL CORRECTNESS CLEANUP MILESTONE, Part 9 — reuses core.answering.
    # data_quality_observations verbatim (report_orchestrator._execute_
    # single_spec populates these per-KPI/chart, deduplicated across the
    # whole report); data_quality_notes is executive-facing plain language,
    # technical_diagnostics is the full evidence-bearing detail.
    data_quality_notes: list[str] = field(default_factory=list)
    technical_diagnostics: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title, "source_id": self.source_id, "subtitle": self.subtitle,
            "generated_at": self.generated_at,
            "kpi_cards": [c.to_dict() for c in self.kpi_cards],
            "charts": [c.to_dict() for c in self.charts],
            "tables": [t.to_dict() for t in self.tables],
            "narrative": self.narrative, "warnings": list(self.warnings),
            "trace": [t.to_dict() for t in self.trace],
            "timings_ms": dict(self.timings_ms),
            "data_quality_notes": list(self.data_quality_notes),
            "technical_diagnostics": list(self.technical_diagnostics),
        }
