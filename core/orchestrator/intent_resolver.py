from __future__ import annotations

import re
from typing import Dict, List, Tuple

from core.orchestrator.interfaces import IIntentResolver
from core.orchestrator.models import IntentType, ResolvedIntent, ServiceCapability


# Maps IntentType → (primary_keywords, secondary_keywords).
# Primary keywords are strong signals (weight 0.4 each).
# Secondary keywords are supporting signals (weight 0.2 each).
_SIGNALS: Dict[IntentType, Tuple[List[str], List[str]]] = {
    IntentType.DICTIONARY: (
        ["dictionary", "glossary", "business name", "definition", "define"],
        ["meaning", "column description", "table description", "what does", "what is"],
    ),
    IntentType.DOMAIN: (
        ["domain", "subject area", "business area", "classify", "classification"],
        ["category", "belongs to", "business domain", "domain assignment"],
    ),
    IntentType.ENTITY: (
        ["entity", "master data", "reference data", "dimension table"],
        ["entity type", "master record", "lookup table", "entity rule"],
    ),
    IntentType.PROFILING: (
        ["profile", "profiling", "data quality", "statistics", "completeness", "null"],
        ["distribution", "column stats", "row count", "quality score", "outlier"],
    ),
    IntentType.GOVERNANCE: (
        ["governance", "pii", "sensitive", "compliance", "stewardship"],
        ["approve", "approval", "policy", "lifecycle", "regulated", "steward"],
    ),
    IntentType.RELATIONSHIP: (
        ["relationship", "foreign key", "join", "related tables"],
        ["connect", "relate", "dependency", "reference", "links"],
    ),
    IntentType.KNOWLEDGE_GRAPH: (
        ["knowledge graph", "knowledge", "graph", "network", "assets"],
        ["related", "discover", "connected", "path", "neighbours"],
    ),
    IntentType.REPORTS: (
        ["report", "reports", "analysis", "generated report"],
        ["saved report", "view report", "open report", "download report"],
    ),
    IntentType.REPORT_GENERATION: (
        [
            "generate report", "create report", "build report", "make report",
            "create executive summary",
            "build kpi report",
            "generate quality report",
            "create pdf report",
            "show trends", "show trend",
            "analyze this dataset", "analyse this dataset",
            "analyze dataset", "analyse dataset",
        ],
        [
            "generate a report", "create a report", "build a report",
            "produce report", "run a report",
            "dataset analysis", "data analysis report",
            "kpi report", "quality report", "pdf report",
            "executive summary",
        ],
    ),
    IntentType.SQL_REQUEST: (
        ["sql", "query", "select", "generate sql", "write query", "generate query"],
        ["from table", "where clause", "join query", "fetch data", "run query"],
    ),
    IntentType.WORKFLOW: (
        ["workflow", "schedule", "automate", "run workflow"],
        ["execution", "trigger", "cron", "automation", "task"],
    ),
    IntentType.REVIEW: (
        ["review", "pending", "queue", "needs review", "awaiting"],
        ["suggestion", "ai suggestion", "review queue", "action required", "to approve"],
    ),
    IntentType.METADATA_LOOKUP: (
        ["metadata", "schema", "data source", "table info", "column info",
         "what tables exist", "list tables", "show tables", "tables exist"],
        ["show me", "find", "lookup", "details about", "information about"],
    ),
    IntentType.QUESTION_ANSWERING: (
        ["explain", "tell me", "what is", "why is", "how does"],
        ["understand", "context", "purpose", "overview", "describe"],
    ),
    IntentType.SEMANTIC_QUERY_PLAN: (
        ["query plan", "execution plan", "plan this query", "plan this question",
         "what tables would i need", "how would i query"],
        ["which tables", "which columns", "business question", "would i join"],
    ),
}

# What capabilities each intent requires from the ServiceRegistry.
_INTENT_CAPABILITIES: Dict[IntentType, List[ServiceCapability]] = {
    IntentType.QUESTION_ANSWERING: [
        ServiceCapability.SEARCH_READ,
        ServiceCapability.BUSINESS_CONTEXT_READ,
        ServiceCapability.SEMANTIC_READ,
    ],
    IntentType.METADATA_LOOKUP: [
        ServiceCapability.SCHEMA_READ,
        ServiceCapability.DICTIONARY_READ,
        ServiceCapability.BUSINESS_CONTEXT_READ,
        ServiceCapability.LIVE_METADATA,
        ServiceCapability.LIVE_SCHEMA,
    ],
    IntentType.DICTIONARY: [
        ServiceCapability.DICTIONARY_READ,
    ],
    IntentType.DOMAIN: [
        ServiceCapability.DOMAIN_READ,
    ],
    IntentType.ENTITY: [
        ServiceCapability.ENTITY_READ,
    ],
    IntentType.PROFILING: [
        ServiceCapability.PROFILING_READ,
    ],
    IntentType.GOVERNANCE: [
        ServiceCapability.GOVERNANCE_READ,
    ],
    IntentType.RELATIONSHIP: [
        ServiceCapability.RELATIONSHIP_READ,
        ServiceCapability.SEMANTIC_READ,
    ],
    IntentType.KNOWLEDGE_GRAPH: [
        ServiceCapability.KNOWLEDGE_GRAPH_READ,
        ServiceCapability.LINEAGE_READ,
        ServiceCapability.SEMANTIC_READ,
    ],
    IntentType.REPORTS: [
        ServiceCapability.REPORT_READ,
    ],
    IntentType.REPORT_GENERATION: [
        ServiceCapability.REPORT_READ,
    ],
    IntentType.SQL_REQUEST: [
        ServiceCapability.SCHEMA_READ,
        ServiceCapability.RELATIONSHIP_READ,
        ServiceCapability.SEMANTIC_READ,
        ServiceCapability.LIVE_QUERY,
    ],
    IntentType.WORKFLOW: [
        ServiceCapability.WORKFLOW_READ,
    ],
    IntentType.REVIEW: [
        ServiceCapability.DICTIONARY_READ,
        ServiceCapability.PROFILING_READ,
        ServiceCapability.GOVERNANCE_READ,
    ],
    IntentType.SEMANTIC_QUERY_PLAN: [
        ServiceCapability.SEMANTIC_QUERY_PLAN,
    ],
    IntentType.UNKNOWN: [
        ServiceCapability.SEARCH_READ,
    ],
}

# Tie-break priority: higher = preferred when two intents score equally.
# More specific intents beat more general ones.
_SPECIFICITY: Dict[IntentType, int] = {
    IntentType.DICTIONARY:         10,
    IntentType.DOMAIN:             10,
    IntentType.ENTITY:             10,
    IntentType.PROFILING:          10,
    IntentType.GOVERNANCE:         10,
    IntentType.RELATIONSHIP:       10,
    IntentType.KNOWLEDGE_GRAPH:    10,
    IntentType.REPORTS:            10,
    IntentType.REPORT_GENERATION:  11,
    IntentType.SQL_REQUEST:        10,
    IntentType.WORKFLOW:           10,
    IntentType.REVIEW:              9,
    IntentType.SEMANTIC_QUERY_PLAN: 11,
    IntentType.METADATA_LOOKUP:     8,
    IntentType.QUESTION_ANSWERING:  5,
    IntentType.UNKNOWN:             0,
}

# Minimum score to consider an intent resolved; below this → UNKNOWN.
_CONFIDENCE_FLOOR = 0.15


class IntentResolver(IIntentResolver):
    """
    Keyword-based enterprise intent resolution. No AI or LLM involved.

    Scores each IntentType against the normalized query using primary and
    secondary keyword signals, then returns the highest-scoring intent with
    secondary intents above the confidence floor.
    """

    def resolve(self, query: str) -> ResolvedIntent:
        normalized = query.lower()

        scores: Dict[IntentType, float] = {}
        matched_by: Dict[IntentType, List[str]] = {}

        for intent_type, (primary, secondary) in _SIGNALS.items():
            p_hits = [kw for kw in primary if kw in normalized]
            s_hits = [kw for kw in secondary if kw in normalized]

            # Each primary match = 0.4; each secondary = 0.2; capped at 1.0
            score = min(len(p_hits) * 0.4 + len(s_hits) * 0.2, 1.0)

            scores[intent_type] = score
            matched_by[intent_type] = p_hits + s_hits

        best = max(
            scores,
            key=lambda k: (scores[k], _SPECIFICITY.get(k, 0)),
        )

        if scores[best] < _CONFIDENCE_FLOOR:
            best = IntentType.UNKNOWN
            scores[best] = 0.0

        secondary = sorted(
            [
                it for it, sc in scores.items()
                if it != best and sc >= _CONFIDENCE_FLOOR
            ],
            key=lambda it: (scores[it], _SPECIFICITY.get(it, 0)),
            reverse=True,
        )

        return ResolvedIntent(
            intent_type=best,
            confidence=round(scores[best], 4),
            required_capabilities=_INTENT_CAPABILITIES.get(
                best, [ServiceCapability.SEARCH_READ]
            ),
            secondary_intents=secondary[:3],
            keywords_matched=matched_by.get(best, []),
        )
