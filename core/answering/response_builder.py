from __future__ import annotations

from core.answering.models import AnswerType, EnterpriseAnswer

# Deterministic follow-up suggestions per answer type. Seeded with the
# brief's own worked examples; sensible analogues fill the remaining
# categories.
_FOLLOW_UPS: dict[AnswerType, list[str]] = {
    AnswerType.DICTIONARY: ["What relationships exist?", "Show profiling results."],
    AnswerType.METADATA: ["What relationships exist?", "Show profiling results."],
    AnswerType.DOMAIN: ["Explain related entities.", "What relationships exist?"],
    AnswerType.ENTITY: ["Explain related entities.", "What relationships exist?"],
    AnswerType.RELATIONSHIP: ["Show profiling results.", "Explain related entities."],
    AnswerType.KNOWLEDGE_GRAPH: ["Explain related entities.", "What relationships exist?"],
    AnswerType.PROFILING: ["Generate dictionary entries.", "Check governance status."],
    AnswerType.GOVERNANCE: ["Generate governance report.", "Explain related entities."],
    AnswerType.REVIEW: ["Generate governance report.", "Show profiling results."],
    AnswerType.LIVE_METADATA: ["Show profiling results.", "What relationships exist?"],
    AnswerType.LIVE_QUERY: ["Generate governance report.", "Show profiling results."],
    AnswerType.REPORT: ["Generate governance report.", "Show profiling results."],
    AnswerType.SEARCH: ["What relationships exist?", "Show profiling results."],
    AnswerType.CROSS_SOURCE_SUMMARY: ["Generate governance report.", "What relationships exist?"],
    AnswerType.RESTRICTED: ["Contact your data steward for access."],
    AnswerType.CLARIFICATION_NEEDED: [],
    AnswerType.UNKNOWN: ["Try asking about dictionary, profiling, or governance."],
}

_NEXT_ACTIONS: dict[AnswerType, str] = {
    AnswerType.DICTIONARY: "Open the Data Dictionary to review and approve table definitions.",
    AnswerType.METADATA: "Open the Schema Explorer for full table/column detail.",
    AnswerType.DOMAIN: "Open Domain Assignments to review classifications.",
    AnswerType.ENTITY: "Open Entity Assignments to review classifications.",
    AnswerType.RELATIONSHIP: "Open the Knowledge Graph to explore full join paths.",
    AnswerType.KNOWLEDGE_GRAPH: "Open the Knowledge Graph for full asset exploration.",
    AnswerType.PROFILING: "Open the Column Profile Explorer for column-level detail.",
    AnswerType.GOVERNANCE: "Open the Governance Command Center to review pending approvals.",
    AnswerType.REVIEW: "Open the review queue to action pending items.",
    AnswerType.LIVE_METADATA: "Open the live source browser for table-level detail.",
    AnswerType.LIVE_QUERY: "Export or page through the full result set.",
    AnswerType.REPORT: "Open the Reports tab to view or export.",
    AnswerType.SEARCH: "Refine your search terms for more precise results.",
    AnswerType.CROSS_SOURCE_SUMMARY: "Drill into an individual service for full detail.",
    AnswerType.RESTRICTED: "Contact your data steward or review the Governance Command Center.",
    AnswerType.CLARIFICATION_NEEDED: "Select one of the listed options and ask again to get an answer.",
    AnswerType.UNKNOWN: "Rephrase your question with a specific catalog term.",
}


def _build_follow_up_questions(answer_type: AnswerType) -> list[str]:
    return list(_FOLLOW_UPS.get(answer_type, []))


def _build_next_actions(answer_type: AnswerType) -> list[str]:
    action = _NEXT_ACTIONS.get(answer_type)
    return [action] if action else []


def _build_related_objects(package) -> list[str]:
    objects: list[str] = []
    for item in package.evidence:
        data = item.data
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("table_fqn"):
                    objects.append(row["table_fqn"])
        elif isinstance(data, dict):
            if data.get("table_fqn"):
                objects.append(data["table_fqn"])
            for row in (data.get("tables") or []):
                if isinstance(row, dict) and row.get("table_fqn"):
                    objects.append(row["table_fqn"])
    return sorted(set(objects))


def _build_execution_summary(strategy) -> dict:
    if strategy is None:
        return {}
    return {
        "strategy_type": getattr(strategy, "strategy_type", None).value
        if getattr(strategy, "strategy_type", None) else None,
        "required_services": getattr(strategy, "required_services", []),
        "execution_order": getattr(strategy, "execution_order", []),
        "confidence": getattr(strategy, "confidence", None),
    }


def build_response(strategy, package, explanation: dict, citations, recommendations) -> EnterpriseAnswer:
    answer_type = explanation["answer_type"]
    governance_warnings = [
        item.governance_state for item in package.evidence if item.governance_state
    ]

    return EnterpriseAnswer(
        answer=explanation["answer"],
        summary=explanation["summary"],
        answer_type=answer_type,
        confidence=explanation["confidence"],
        citations=citations,
        governance_warnings=governance_warnings,
        recommendations=recommendations,
        limitations=explanation["limitations"],
        follow_up_questions=_build_follow_up_questions(answer_type),
        next_actions=_build_next_actions(answer_type),
        related_objects=_build_related_objects(package),
        execution_summary=_build_execution_summary(strategy),
        clarification=explanation.get("clarification"),
        actual_value=explanation.get("actual_value"),
        result_preview=explanation.get("result_preview") or [],
        business_entity=explanation.get("business_entity"),
        measure=explanation.get("measure"),
        aggregation=explanation.get("aggregation"),
        applied_filters=explanation.get("applied_filters") or [],
        date_context=explanation.get("date_context"),
        source_tables=explanation.get("source_tables") or [],
        source_columns=explanation.get("source_columns") or [],
        assumptions=explanation.get("assumptions") or [],
        truncation_notice=explanation.get("truncation_notice"),
        insight=explanation.get("insight"),
    )
