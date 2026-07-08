from __future__ import annotations

import logging

from core.semantic import concept_resolver, context_builder, relationship_resolver
from core.semantic.execution_plan import ExecutionPlan

logger = logging.getLogger(__name__)

_RESTRICTING_WARNING_TYPES = frozenset({"pii_involved", "no_join_path_found"})


def _build_strategy(source_id: int, user_id: str, query_plan: dict) -> dict:
    from data.semantic_layer_service import semantic_table_profile

    intent = query_plan.get("intent") or {}
    join_plan = query_plan.get("join_plan") or {}
    table_roles: dict[str, str] = {}
    for table_fqn in (query_plan.get("tables") or []):
        try:
            profile = semantic_table_profile(source_id, user_id, table_fqn)
        except Exception:  # noqa: BLE001
            profile = None
        if profile:
            table_roles[table_fqn] = profile.get("semantic_role")

    return {
        "intent_type": intent.get("type"),
        "aggregation": intent.get("aggregation"),
        "table_roles": table_roles,
        "join_required": join_plan.get("required", False),
        "primary_table": join_plan.get("primary_table"),
    }


def _is_governance_restricted(warnings: list[dict]) -> bool:
    return any(
        w.get("severity") == "HIGH" and w.get("type") in _RESTRICTING_WARNING_TYPES
        for w in warnings
    )


class SemanticQueryPlanner:
    """
    Converts a business question into a structured, non-SQL Execution Plan.

    Reuses data.query_planning_service.plan_business_query wholesale for
    concept-to-table/column resolution, join planning, and governance
    warnings. Adds: NL question -> concept terms, a richer per-concept
    match report (ambiguous/unknown detection), FK/dependency enrichment,
    and Semantic Context aggregation. Never generates SQL.
    """

    def plan(self, source_id: int, user_id: str, question: str, filters: list | None = None) -> ExecutionPlan | None:
        from data.query_planning_service import plan_business_query

        concepts, measures, dimensions = concept_resolver.extract_terms(question)
        concept_matches = concept_resolver.resolve_concepts(source_id, user_id, concepts)

        query_plan = plan_business_query(source_id, user_id, {
            "question": question,
            "concepts": concepts,
            "measures": measures,
            "dimensions": dimensions,
            "filters": filters or [],
        })
        if query_plan is None:
            return None

        relationship_result = relationship_resolver.resolve(source_id, user_id, query_plan)
        semantic_context = context_builder.build(source_id, user_id, query_plan, relationship_result)
        strategy = _build_strategy(source_id, user_id, query_plan)

        warnings = query_plan.get("warnings") or []

        return ExecutionPlan(
            question=question,
            concepts=concept_matches,
            relevant_tables=query_plan.get("tables") or [],
            relevant_columns=query_plan.get("columns") or {},
            relationships=relationship_result,
            required_filters=query_plan.get("filters") or [],
            governance_restricted=_is_governance_restricted(warnings),
            governance_checks=warnings,
            recommended_query_strategy=strategy,
            semantic_context=semantic_context,
            confidence=query_plan.get("confidence", 0),
            warnings=warnings,
            explanation=query_plan.get("explanation", ""),
        )
