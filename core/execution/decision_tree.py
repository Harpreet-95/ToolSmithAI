from __future__ import annotations

from core.execution import rules
from core.execution.execution_strategy import StrategyType
from core.orchestrator.models import IntentType

_SQL_REQUIRED_ORDER = ["semantic_query_plan", "sql_planner", "sql_generator", "live_query"]

# Confidence per matching step — higher for more specific/certain matches.
_STEP_CONFIDENCE = {
    "pattern": 90,
    "fallback": 70,
    "question_answering": 40,
    "unknown": 10,
}


def _base_result(strategy_type: StrategyType, required_services: list[str], **flags) -> dict:
    result = {
        "strategy_type": strategy_type,
        "required_services": list(required_services),
        "execution_order": list(flags.pop("execution_order", required_services)),
        "governance_checks": flags.pop("governance_checks", []),
        "requires_live_data": False,
        "requires_sql": False,
        "requires_report": False,
        "requires_search": False,
        "requires_dictionary": False,
        "requires_profiling": False,
        "requires_metadata": False,
        "requires_workflow": False,
        "warnings": [],
    }
    result.update(flags)
    return result


def decide(question: str, resolved_intent, semantic_plan=None) -> dict:
    """
    Decide HOW a request should be answered. Never executes anything —
    returns a plain dict consumed by core/execution/planner.py to build an
    ExecutionStrategy. Evaluated most-specific-first; each step corresponds
    to a verified gap in intent_resolver.py's keyword table or a brief
    worked example (see the Phase 9 plan).
    """
    intent_type = getattr(resolved_intent, "intent_type", None)

    # 1. PII / sensitive-data mention -> Governance + Metadata.
    if rules.mentions_pii(question) or intent_type == IntentType.GOVERNANCE:
        result = _base_result(
            StrategyType.GOVERNANCE_CHECK, ["governance", "schema"],
            governance_checks=["pii_review"], requires_metadata=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["pattern"]}

    # 2. "Explain <single word>" -> Dictionary + Domain + Entity + Knowledge Graph.
    if rules.is_single_word_explain(question):
        result = _base_result(
            StrategyType.EXPLAIN_ENTITY,
            ["dictionary", "domain", "entity", "knowledge_graph"],
            requires_dictionary=True, requires_metadata=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["pattern"]}

    # 3. "What is <term>?" definition question -> Dictionary.
    if rules.is_definition_question(question):
        result = _base_result(
            StrategyType.DICTIONARY_LOOKUP, ["dictionary"], requires_dictionary=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["pattern"]}

    # 4. "What/which tables/columns ..." -> Metadata.
    if rules.is_metadata_question(question) or intent_type == IntentType.METADATA_LOOKUP:
        result = _base_result(
            StrategyType.METADATA_LOOKUP, ["schema", "search"],
            requires_metadata=True, requires_search=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["pattern"]}

    # 5. "Generate/create/build a report" -> Report Engine.
    if rules.is_report_generation_request(question) or intent_type == IntentType.REPORT_GENERATION:
        result = _base_result(
            StrategyType.REPORT_GENERATION, ["reports"], requires_report=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["pattern"]}

    # 6. Analytical / SQL-required — either the Semantic Plan's own signal
    #    (Phase 8's _build_intent) or the keyword heuristic can trigger this;
    #    a semantic plan that failed to resolve a term (e.g. "top 10
    #    customers" mis-parsed as a measure by the simple before/after-"by"
    #    split) doesn't mean the question isn't analytical.
    if rules.semantic_plan_is_analytical(semantic_plan) or rules.is_analytical_question(question):
        result = _base_result(
            StrategyType.SQL_REQUIRED, _SQL_REQUIRED_ORDER,
            requires_sql=True, requires_live_data=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["pattern"]}

    # 7. Direct IntentType fallback mapping.
    mapped = rules.INTENT_FALLBACK_MAP.get(intent_type)
    if mapped is not None:
        result = _base_result(mapped["strategy_type"], mapped["required_services"], **{
            k: v for k, v in mapped.items() if k not in ("strategy_type", "required_services")
        })
        return {**result, "confidence": _STEP_CONFIDENCE["fallback"]}

    # 8. Generic question-answering — nothing more specific matched.
    if intent_type == IntentType.QUESTION_ANSWERING:
        result = _base_result(
            StrategyType.GENERAL_QUESTION, ["search", "business_knowledge"],
            requires_search=True,
        )
        return {**result, "confidence": _STEP_CONFIDENCE["question_answering"]}

    # 9. Unknown.
    result = _base_result(StrategyType.UNKNOWN, ["search"], requires_search=True)
    result["warnings"] = ["Could not determine a confident execution strategy."]
    return {**result, "confidence": _STEP_CONFIDENCE["unknown"]}
