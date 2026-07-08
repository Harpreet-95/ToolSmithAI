from __future__ import annotations

import re

from core.execution.execution_strategy import StrategyType
from core.orchestrator.models import IntentType

# ---------------------------------------------------------------------------
# Pattern predicates — pure text checks, no service calls. Each one exists to
# close a specific, verified gap in core/orchestrator/intent_resolver.py's
# keyword table (see the Phase 9 plan for the exact worked examples that
# fail today) rather than being speculative.
# ---------------------------------------------------------------------------

_PII_RE = re.compile(r"\bpii\b|\bsensitive\b|\bpersonal data\b|\bconfidential\b", re.IGNORECASE)
_SINGLE_WORD_EXPLAIN_RE = re.compile(r"^\s*explain\s+[a-zA-Z0-9_]+\s*\??\s*$", re.IGNORECASE)
_DEFINITION_RE = re.compile(r"^\s*what\s+is\b", re.IGNORECASE)
_METADATA_RE = re.compile(
    r"\bwhat tables\b|\bwhich tables\b|\btables contain\b|\bcolumns contain\b|"
    r"\bwhat columns\b|\bwhich columns\b",
    re.IGNORECASE,
)
_REPORT_GEN_RE = re.compile(r"\b(generate|create|build|make)\b.*\breport\b", re.IGNORECASE)
_ANALYTICAL_RE = re.compile(
    r"\btop\s+\d+\b|\btotal\b|\bsum of\b|\baverage\b|\bhighest\b|\blowest\b|\bcount of\b",
    re.IGNORECASE,
)

_ANALYTICAL_SEMANTIC_INTENTS = frozenset({
    "aggregate_by_dimension", "aggregate_overall", "list_by_dimension",
})


def mentions_pii(question: str) -> bool:
    return bool(_PII_RE.search(question or ""))


def is_single_word_explain(question: str) -> bool:
    return bool(_SINGLE_WORD_EXPLAIN_RE.match(question or ""))


def is_definition_question(question: str) -> bool:
    return bool(_DEFINITION_RE.match(question or ""))


def is_metadata_question(question: str) -> bool:
    return bool(_METADATA_RE.search(question or ""))


def is_report_generation_request(question: str) -> bool:
    return bool(_REPORT_GEN_RE.search(question or ""))


def is_analytical_question(question: str) -> bool:
    """Keyword heuristic for 'this needs aggregation/SQL', checked alongside
    (not only in absence of) the Semantic Plan's own intent_type signal."""
    return bool(_ANALYTICAL_RE.search(question or ""))


def semantic_plan_is_analytical(semantic_plan) -> bool:
    if semantic_plan is None:
        return False
    strategy = getattr(semantic_plan, "recommended_query_strategy", None) or {}
    return strategy.get("intent_type") in _ANALYTICAL_SEMANTIC_INTENTS


# ---------------------------------------------------------------------------
# Direct IntentType -> strategy fallback map, for every intent not already
# handled by a more specific pattern rule in decision_tree.py.
# ---------------------------------------------------------------------------

INTENT_FALLBACK_MAP: dict[IntentType, dict] = {
    IntentType.DICTIONARY: {
        "strategy_type": StrategyType.DICTIONARY_LOOKUP,
        "required_services": ["dictionary"],
        "requires_dictionary": True,
    },
    IntentType.DOMAIN: {
        "strategy_type": StrategyType.METADATA_LOOKUP,
        "required_services": ["domain"],
        "requires_metadata": True,
    },
    IntentType.ENTITY: {
        "strategy_type": StrategyType.METADATA_LOOKUP,
        "required_services": ["entity"],
        "requires_metadata": True,
    },
    IntentType.PROFILING: {
        "strategy_type": StrategyType.PROFILING,
        "required_services": ["profiling"],
        "requires_profiling": True,
    },
    IntentType.GOVERNANCE: {
        "strategy_type": StrategyType.GOVERNANCE_CHECK,
        "required_services": ["governance", "schema"],
        "requires_metadata": True,
        "governance_checks": ["pii_review"],
    },
    IntentType.RELATIONSHIP: {
        "strategy_type": StrategyType.RELATIONSHIP_LOOKUP,
        "required_services": ["relationship", "semantic_layer"],
        "requires_metadata": True,
    },
    IntentType.KNOWLEDGE_GRAPH: {
        "strategy_type": StrategyType.KNOWLEDGE_GRAPH_LOOKUP,
        "required_services": ["knowledge_graph"],
        "requires_metadata": True,
    },
    IntentType.REPORTS: {
        "strategy_type": StrategyType.REPORT_GENERATION,
        "required_services": ["reports"],
        "requires_report": True,
    },
    IntentType.REPORT_GENERATION: {
        "strategy_type": StrategyType.REPORT_GENERATION,
        "required_services": ["reports"],
        "requires_report": True,
    },
    IntentType.SQL_REQUEST: {
        "strategy_type": StrategyType.SQL_REQUIRED,
        "required_services": ["semantic_query_plan", "sql_planner", "sql_generator", "live_query"],
        "requires_sql": True,
        "requires_live_data": True,
    },
    IntentType.WORKFLOW: {
        "strategy_type": StrategyType.WORKFLOW,
        "required_services": ["workflow"],
        "requires_workflow": True,
    },
    IntentType.REVIEW: {
        "strategy_type": StrategyType.GOVERNANCE_CHECK,
        "required_services": ["dictionary", "governance", "profiling"],
        "requires_dictionary": True,
        "requires_profiling": True,
        "requires_metadata": True,
    },
    IntentType.METADATA_LOOKUP: {
        "strategy_type": StrategyType.METADATA_LOOKUP,
        "required_services": ["schema", "search"],
        "requires_metadata": True,
        "requires_search": True,
    },
    IntentType.SEMANTIC_QUERY_PLAN: {
        "strategy_type": StrategyType.SQL_REQUIRED,
        "required_services": ["semantic_query_plan", "sql_planner", "sql_generator", "live_query"],
        "requires_sql": True,
        "requires_live_data": True,
    },
}
