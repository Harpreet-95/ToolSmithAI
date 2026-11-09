from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AnswerType(str, Enum):
    DICTIONARY = "dictionary"
    METADATA = "metadata"
    DOMAIN = "domain"
    ENTITY = "entity"
    RELATIONSHIP = "relationship"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    PROFILING = "profiling"
    GOVERNANCE = "governance"
    REVIEW = "review"
    LIVE_METADATA = "live_metadata"
    LIVE_QUERY = "live_query"
    REPORT = "report"
    SEARCH = "search"
    CROSS_SOURCE_SUMMARY = "cross_source_summary"
    RESTRICTED = "restricted"
    CLARIFICATION_NEEDED = "clarification_needed"
    UNKNOWN = "unknown"


class CitationType(str, Enum):
    METADATA_SOURCE = "metadata_source"
    TABLE = "table"
    COLUMN = "column"
    DICTIONARY_ENTRY = "dictionary_entry"
    DOMAIN = "domain"
    ENTITY = "entity"
    RELATIONSHIP = "relationship"
    REPORT = "report"
    LIVE_QUERY = "live_query"
    PROFILING = "profiling"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    REMEMBERED_TERMINOLOGY = "remembered_terminology"
    GENERATED_VOCABULARY = "generated_vocabulary"


@dataclass(frozen=True)
class Citation:
    source_type: CitationType
    reference: str
    label: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Recommendation:
    type: str
    message: str
    priority: str = "MEDIUM"


@dataclass
class EnterpriseAnswer:
    answer: str
    summary: str
    answer_type: AnswerType
    confidence: int
    citations: list[Citation] = field(default_factory=list)
    governance_warnings: list[str] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    follow_up_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    related_objects: list[str] = field(default_factory=list)
    execution_summary: dict = field(default_factory=dict)
    clarification: dict | None = None
    # Milestone M-25 — Enterprise Answer Value Rendering. Optional business-
    # value fields, populated only for successful live-query answers; every
    # other answer type leaves these at their defaults so to_dict() stays
    # backward compatible for existing consumers.
    actual_value: Any = None
    result_preview: list = field(default_factory=list)
    business_entity: str | None = None
    measure: str | None = None
    aggregation: str | None = None
    applied_filters: list = field(default_factory=list)
    date_context: dict | None = None
    source_tables: list = field(default_factory=list)
    source_columns: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    truncation_notice: str | None = None
    # Day 4, Capability 2 — Business Insights. Populated only for a
    # time-bound single-scalar aggregate answer where a period-comparison
    # was actually computed (data.insight_service); None for every other
    # answer, same backward-compatible-default convention as the M-25
    # fields above.
    insight: dict | None = None
    # Day 4, Capability 3 — Automatic Charts. {chart_type, labels, series} —
    # the exact shape frontend/src/components/ChartSection.jsx already
    # accepts — or None whenever the result shape doesn't support a chart
    # (core.answering.result_formatter._build_chart_spec).
    chart: dict | None = None
    # Phase 7, Step 6G — AI-first fast-path result insights/visualization.
    # insights (plural): grounded, multi-item findings computed directly
    # from the executed rows (core.sqlservice.result_insight_builder /
    # answer_generation_service.AnswerInsight) -- additive alongside the
    # legacy singular `insight` above, never replacing it; populated only
    # by api/v1/ai_first_composer_adapter.py, [] for every other pipeline.
    # kpi: {"label", "value"} for a genuine single-scalar result (never a
    # one-row-multi-column detail record) -- core.sqlservice.
    # result_chart_builder.build_deterministic_kpi; None whenever the
    # result isn't a clean single scalar.
    insights: list = field(default_factory=list)
    kpi: dict | None = None
    # NEXT MILESTONE, Part 9 — Source Data Quality Observation wiring.
    # data_quality_notes is executive/business-facing plain-language text
    # (core.answering.data_quality_observations.executive_data_notes);
    # technical_diagnostics is the full evidence-bearing detail for an
    # admin/developer view (core.answering.data_quality_observations.
    # technical_diagnostics). Both default to empty — populated only when
    # core.orchestrator.agent actually ran the bounded, question-scoped
    # detection pass (see that module's own wiring comment).
    data_quality_notes: list[str] = field(default_factory=list)
    technical_diagnostics: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["answer_type"] = AnswerType(d["answer_type"]).value
        for c in d["citations"]:
            c["source_type"] = CitationType(c["source_type"]).value
        return d
