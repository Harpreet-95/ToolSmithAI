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
        # Primary: literal SQL vocabulary plus the same analytical signal
        # words core.execution.rules.is_analytical_question already uses to
        # detect an aggregation question (top N, total, average, highest,
        # lowest, sum of, count of) — reused here, not reinvented, so a
        # bare "top 10 X" / "average X" question resolves as SQL_REQUEST
        # the same way it already resolves as analytical for ExecutionPlanner.
        #
        # Milestone M-1 (Enterprise Question Intelligence) additions —
        # "distinct"/"unique"/"bottom"/"latest"/"newest"/"earliest"/"oldest":
        # collision-checked against every other IntentType's primary/
        # secondary lists in this file; none of these seven words appear
        # anywhere else, so they're added at primary weight like their
        # existing "top "/"average"/"highest"/"lowest" siblings above.
        [
            "sql", "query", "select", "generate sql", "write query", "generate query",
            "top ", "average", "highest", "lowest", "total", "sum of", "count of",
            "distinct", "unique", "bottom", "latest", "newest", "earliest", "oldest",
            # Phase 6.3 (Enterprise Intent Coverage) additions — imperative
            # request verbs: collision-checked against every other IntentType's
            # primary/secondary lists. "find" and "show me" already exist as
            # METADATA_LOOKUP *secondary* (0.2) keywords; adding the bare verbs
            # here at primary weight (0.4) is a deliberate, checked precedence
            # change — a genuine metadata question keeps winning outright
            # whenever it also matches its own primary keyword ("schema",
            # "list tables", etc., e.g. "show me the schema" scores
            # METADATA_LOOKUP 0.6 vs SQL_REQUEST 0.4 here), and REPORT_GENERATION's
            # "show trend(s)" phrases already score 0.8 on their own two primary
            # hits, so they're unaffected. "which" collides with
            # SEMANTIC_QUERY_PLAN's secondary "which tables"/"which columns" and
            # GOVERNANCE's primary "pii" on phrases like "which columns are
            # flagged as pii" — verified the existing insertion-order tie-break
            # (GOVERNANCE/SEMANTIC_QUERY_PLAN are both declared earlier in this
            # dict than SQL_REQUEST) keeps those questions on their own intent
            # on an exact score tie, so bare "which" is safe to add here.
            # NOTE: "give me" was tried and reverted — it broke an existing,
            # passing test (test_chat_phrase_routes_to_semantic_query_plan_intent,
            # "give me a query plan for revenue by region"): SEMANTIC_QUERY_PLAN's
            # own "query plan" match (0.4) only wins its pre-existing tie with
            # SQL_REQUEST's bare "query" match (0.4) via the specificity
            # tie-break (11 > 10); any second SQL_REQUEST hit — "give me" at
            # either weight — pushes SQL_REQUEST's score strictly above 0.4 and
            # wins outright, regardless of specificity. Every "Give me ..."
            # case in this milestone's own corpus/tests already carries another
            # qualifying signal (e.g. "active"/"recent") that independently
            # reaches SQL_REQUEST, so dropping "give me" costs no required
            # coverage while keeping this pre-existing test intact.
            "show", "list", "display", "find", "return", "which",
            # Recency adjectives, same group as the existing latest/newest/
            # earliest/oldest primary keywords above; "current"/"recent" were
            # not in that original M-1 set. No collision found elsewhere.
            "current", "recent",
            # Status-word coverage. Milestone M-1 deliberately excluded these
            # to avoid misrouting non-analytical text (see the superseded
            # comment on the secondary list below); Phase 6.3's brief
            # explicitly requires this coverage, so the trade-off is now
            # accepted — a sentence merely
            # containing "active"/"open"/etc. with no other business-question
            # shape can now resolve SQL_REQUEST at 0.4 confidence. Bare
            # "pending" is deliberately NOT included: it remains a REVIEW
            # primary keyword (confirmed decision, 2026-07-13) so genuine
            # review-queue questions ("pending review", "pending approval")
            # are not misrouted; "pending" status filtering on a business
            # question still reaches SQL_REQUEST once another SQL_REQUEST
            # signal (e.g. an imperative verb) is present in the same question.
            "active", "inactive", "open", "closed", "cancelled", "completed",
        ],
        # "how many" / "number of" are weighted secondary (0.2), not primary —
        # collision analysis found they tie 0.4-vs-0.4 with METADATA_LOOKUP's
        # "schema", WORKFLOW's "workflow", RELATIONSHIP's "relationship",
        # GOVERNANCE's "governance", etc. on phrases like "how many workflows
        # do I have", and specificity/insertion-order tie-breaks then wrongly
        # hand those to SQL_REQUEST. At secondary weight, a bare "how many X"
        # with no other intent's keyword still clears SQL_REQUEST's own
        # confidence floor (0.2 >= 0.15), but a genuine metadata/workflow/
        # relationship/governance question keeps winning on its own stronger
        # primary-keyword match instead of being overridden.
        #
        # Milestone M-1 date-intelligence phrases are added at this same
        # secondary weight, for the same reason: a bare date phrase alone is
        # too generic to safely outrank another intent's own primary match
        # (e.g. "profiling this month" should stay PROFILING, not flip to
        # SQL_REQUEST), but combined with any other SQL_REQUEST signal it
        # correctly tips the question toward SQL_REQUEST.
        #
        # Phase 6.3 (Enterprise Intent Coverage) note: status words
        # (active/inactive/open/closed/completed/cancelled) were originally
        # excluded from this whole intent for the reason given above — they
        # now live in the PRIMARY list instead (see above; "pending" remains
        # excluded, still reserved for REVIEW). Grouping/sorting/date-range/
        # ratio phrases below stay at this secondary weight for the same
        # "how many"/"number of" reasoning: each is too generic alone to
        # safely outrank another intent's own primary match, but tips a
        # bare business question (e.g. "Sorted by client") over the
        # confidence floor once combined with another SQL_REQUEST signal —
        # collision-checked, none of these phrases appear in any other
        # IntentType's primary/secondary lists in this file.
        [
            "from table", "where clause", "join query", "fetch data", "run query", "how many", "number of",
            "today", "yesterday", "this week", "last week", "this month", "last month",
            "this quarter", "last quarter", "this year", "last year",
            "grouped by", "sorted by", "sort by", "sort ",
            "by department", "by recruiter", "by client", "by month", "by year",
            "between ", "percentage", "percent ",
            # "Student enrollments" is the one Phase 6.3 validated-problem
            # example with no leading verb, status word, or recency adjective
            # at all (unlike the brief's other seven, e.g. "Show active
            # clients", "Current payroll") — a genuinely bare business-record
            # noun phrase. No collision found; narrow, single-term addition
            # rather than a general bare-noun-phrase detector, matching this
            # milestone's "extend, don't redesign" scope.
            "enrollment",
        ],
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

# Phase 6.3 (Enterprise Intent Coverage) normalization: strips punctuation
# (question marks, commas, periods, quotes, parens) to spaces and collapses
# whitespace before keyword matching, so phrasing like "Show active clients?"
# or "List job orders, sorted by client." matches the same as its
# punctuation-free equivalent. Purely additive to the existing `.lower()` —
# no keyword in _SIGNALS relies on adjacent punctuation.
_PUNCTUATION_RE = re.compile(r"[.,!?;:()\"'‘’“”]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(query: str) -> str:
    text = _PUNCTUATION_RE.sub(" ", query.lower())
    return _WHITESPACE_RE.sub(" ", text).strip()


class IntentResolver(IIntentResolver):
    """
    Keyword-based enterprise intent resolution. No AI or LLM involved.

    Scores each IntentType against the normalized query using primary and
    secondary keyword signals, then returns the highest-scoring intent with
    secondary intents above the confidence floor.
    """

    def resolve(self, query: str) -> ResolvedIntent:
        normalized = _normalize(query)

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
