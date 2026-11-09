"""
Executive Report Query Foundation milestone, Part 6 — KPIQuerySpec.

A KPIQuerySpec describes what business information a report needs; it is
NOT SQL and never becomes SQL directly. core.reporting.report_orchestrator
converts each KPIQuerySpec into a call to
core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan — the SAME
AI physical planning/validation architecture used by ordinary SQL Q&A — and
only ever executes what that call validates. A KPI whose spec cannot be
safely planned is skipped/refused (Part 8), never silently answered by a
different, unrelated metric.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Aggregation = Literal["COUNT", "DISTINCT_COUNT", "SUM", "AVG", "MIN", "MAX"]
CalendarGrain = Literal["year", "month", "quarter", "week", "day"]
ComparisonType = Literal["month_over_month", "year_over_year", "quarter_over_quarter", "custom"]
VisualizationHint = Literal["kpi_card", "line_chart", "bar_chart", "table"]


@dataclass(frozen=True)
class KPIMeasure:
    """One business quantity to aggregate. `term` is a business phrase (e.g.
    "course fee"), never a physical column name — resolved the same way
    ordinary SQL Q&A resolves an AI-named measure term."""
    term: str
    aggregation: Aggregation


@dataclass(frozen=True)
class ComparisonRequest:
    """Part 5 — a request for period-over-period comparison, attached to a
    KPIQuerySpec rather than expressed as a second, independent KPI. Exactly
    one of (comparison_type, (current_period & previous_period)) drives
    core.reporting.comparison.build_comparison_plan: a named comparison_type
    ("month_over_month", ...) is resolved to concrete date ranges
    deterministically; "custom" requires both periods to be supplied
    explicitly."""
    comparison_type: ComparisonType
    current_period: dict | None = None  # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    previous_period: dict | None = None


@dataclass(frozen=True)
class KPIQuerySpec:
    id: str
    title: str
    business_question: str
    entity_name: str
    measures: tuple[KPIMeasure, ...]
    calendar_grain: CalendarGrain | None = None
    dimension_terms: tuple[str, ...] = ()
    date_range: dict | None = None  # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    comparison: ComparisonRequest | None = None
    visualization_hint: VisualizationHint = "kpi_card"
    importance: str = "medium"  # "high" | "medium" | "low"
    reason: str = ""
    evidence_table_fqns: tuple[str, ...] = field(default_factory=tuple)

    def as_plan_kwargs(self) -> dict:
        """Adapts this spec into core.orchestrator.ai_physical_planning.
        attempt_structured_kpi_plan's own keyword-argument shape — the one
        and only place a KPIQuerySpec's fields are translated toward the
        physical planner, so every KPI (report-planned or hand-authored)
        goes through identical translation."""
        return {
            "entity_name": self.entity_name,
            "business_question": self.business_question,
            "measures": [{"term": m.term, "aggregation": m.aggregation} for m in self.measures],
            "calendar_grain": self.calendar_grain,
            "dimension_terms": list(self.dimension_terms),
            "date_range": self.date_range,
        }
