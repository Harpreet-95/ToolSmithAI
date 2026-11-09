"""
Full AI Query-Plan Control milestone — Part 2: the ONE canonical structured
model for the AI's PHYSICAL query plan.

This is a record of what the AI decided (which real candidates it selected,
and why), kept separate from the SQL-ready plan shape
data.sql_planning_service.build_sql_plan already produces and validates.
core.orchestrator.ai_physical_planning translates a PhysicalQueryPlan into
that existing shape (the "smallest adapter", not a second validator/
generator) — build_sql_plan and data.sql_generation_service.generate_sql are
reused completely unchanged.

Every table/column/join reference on this object must have come from a
supplied, real candidate (data.semantic_contract_service.
gather_candidate_evidence's evidence, or an equivalent bounded evidence
set) — nothing here is ever invented. candidate_provenance maps each
identifier back to the candidate_id the AI was shown, for traceability
(Part 9).

Stage 1 populates only: intent, source_id, tables (one primary table),
columns (a single count/list column set), expected_result_shape,
confidence, candidate_provenance, evidence_references. Later stages
(filters/dates/grouping/ranking — Stage 2; joins — Stage 3; comparison —
Stage 4) populate the remaining fields on this SAME object rather than
introducing a competing plan shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Intents this milestone's stages can produce. Deliberately a closed set —
# any AI-authored value outside this set is a validation failure, never
# silently accepted (mirrors core.semantic.ai_interpreter's closed
# relationship_intent/expected_result_shape enums).
PLAN_INTENTS = frozenset({
    "entity_count", "distinct_entity_count", "list_entities", "aggregate",
})


@dataclass(frozen=True)
class PlanTable:
    candidate_id: str
    table_fqn: str
    role: str = "primary"  # "primary" | "related"
    alias: str | None = None


@dataclass(frozen=True)
class PlanColumn:
    table_fqn: str
    column_name: str
    role: str  # "measure" | "dimension" | "attribute" | "identity_key"
    aggregation: str | None = None
    distinct: bool = False
    time_grain: str | None = None


@dataclass(frozen=True)
class PlanFilter:
    table_fqn: str
    column_name: str
    operator: str
    value: Any


@dataclass(frozen=True)
class PlanJoinEdge:
    left_table: str
    left_column: str
    right_table: str
    right_column: str


@dataclass(frozen=True)
class PhysicalQueryPlan:
    intent: str
    source_id: int
    tables: tuple[PlanTable, ...] = ()
    columns: tuple[PlanColumn, ...] = ()
    filters: tuple[PlanFilter, ...] = ()
    date_column: str | None = None
    date_range: dict | None = None
    group_by: tuple[str, ...] = ()
    order_by: dict | None = None
    joins: tuple[PlanJoinEdge, ...] = ()
    comparison: dict | None = None
    expected_result_shape: str = "unknown"
    confidence: float = 0.0
    no_safe_plan: bool = False
    candidate_provenance: dict[str, str] = field(default_factory=dict)
    evidence_references: tuple[str, ...] = ()
