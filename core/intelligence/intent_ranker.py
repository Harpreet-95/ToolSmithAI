"""
core/intelligence/intent_ranker.py

KPI intent-based ranking for ToolSmithAI.

Extracted from core/intelligence/business_kpi_engine.py so that ranking
logic is reusable without importing the full KPI computation engine.

Public interface:
    rank_kpis(cards, dataset_type, max_kpis=8, intent_text=None) -> list[dict]
"""

_PRIORITY_ORDER = {"executive": 0, "operational": 1, "risk": 2, "trend": 3}
_SEMANTIC_PRIORITY = {
    "revenue":    0, "profit": 1,  "cost": 2,
    "quantity":   3, "price":  4,  "customer": 5,
    "percentage": 6, "score":  7,  "risk": 8,
    "region":     9, "product": 10, "category": 11,
    # Generic measure KPIs (Phase 1 discovery) always rank below domain-specific types.
    "measure":   12,
}

# Maps intent phrases → semantic_sources whose cards get a ranking boost.
# Longer / more-specific phrases listed first so they win over short substrings.
_INTENT_KPI_BOOST: list[tuple[str, frozenset]] = [
    ("customer churn",  frozenset({"customer", "risk", "percentage", "status"})),
    ("churn",           frozenset({"customer", "risk", "percentage", "status"})),
    ("retention",       frozenset({"customer", "percentage", "risk"})),
    ("customer",        frozenset({"customer", "revenue", "percentage"})),
    ("regional",        frozenset({"region", "revenue", "country", "state", "city"})),
    ("region",          frozenset({"region", "revenue"})),
    ("product",         frozenset({"product", "revenue", "quantity", "price"})),
    ("operations",      frozenset({"quantity", "status", "score", "percentage"})),
    ("operational",     frozenset({"quantity", "status", "score", "percentage"})),
    ("anomaly",         frozenset({"risk", "status", "percentage"})),
    ("outlier",         frozenset({"risk", "status"})),
    ("risk",            frozenset({"risk", "status", "percentage"})),
    ("trend",           frozenset({"revenue", "quantity", "percentage"})),
    ("forecast",        frozenset({"revenue", "quantity"})),
    ("margin",          frozenset({"profit", "cost", "revenue"})),
    ("profit",          frozenset({"profit", "revenue", "cost"})),
    ("cost",            frozenset({"cost", "profit"})),
    ("sales",           frozenset({"revenue", "quantity", "product"})),
    ("revenue",         frozenset({"revenue", "profit"})),
    ("employee",        frozenset({"employee", "quantity", "score"})),
    ("price",           frozenset({"price", "revenue"})),
]


# Maps strategy intent_type → semantic_source → ranking boost float.
# Used when rank_kpis() receives a resolved strategy_intent_type instead of
# raw intent_text, so classification runs once (in report_strategy_engine)
# rather than being re-derived from keywords here.
_KPI_SOURCE_BOOST_BY_INTENT: dict[str, dict[str, float]] = {
    "executive_brief": {
        "revenue":    2.0,
        "profit":     1.5,
        "cost":       1.0,
        "customer":   0.5,
        "quantity":   0.0,
        "price":      0.0,
        "score":      0.0,
        "percentage": 0.0,
        "risk":       0.0,
        "measure":    0.0,
    },
    "kpi_scorecard": {
        "revenue":    1.5,
        "profit":     1.5,
        "cost":       1.0,
        "quantity":   1.0,
        "price":      0.8,
        "customer":   0.8,
        "score":      0.5,
        "percentage": 0.5,
        "risk":       0.5,
        "measure":    0.3,
    },
    "anomaly_focus": {
        "risk":       2.0,
        "percentage": 1.0,
        "score":      0.8,
        "revenue":    0.2,
        "cost":       0.2,
        "profit":     0.2,
        "quantity":   0.2,
        "price":      0.0,
        "customer":   0.5,
        "measure":    0.0,
    },
    "trend_monitoring": {
        "revenue":    2.0,
        "quantity":   1.2,
        "percentage": 1.0,
        "profit":     0.8,
        "cost":       0.5,
        "price":      0.3,
        "score":      0.3,
        "risk":       0.3,
        "customer":   0.0,
        "measure":    0.0,
    },
    "data_quality": {
        "percentage": 1.5,
        "score":      1.0,
        "risk":       0.8,
        "quantity":   0.5,
        "revenue":    0.0,
        "profit":     0.0,
        "cost":       0.0,
        "price":      0.0,
        "customer":   0.3,
        "measure":    0.0,
    },
    "visual_dashboard": {
        "revenue":    1.5,
        "profit":     1.2,
        "quantity":   1.0,
        "percentage": 1.0,
        "customer":   0.8,
        "price":      0.8,
        "score":      0.5,
        "risk":       0.5,
        "cost":       0.5,
        "measure":    0.3,
    },
}


_KPI_MAX_BY_INTENT: dict[str, int] = {
    "executive_brief":  5,
    "kpi_scorecard":   12,
    "anomaly_focus":    8,
    "trend_monitoring": 8,
    "data_quality":     4,
    "visual_dashboard": 8,
}


def _strategy_boost(card: dict, strategy_intent_type: str) -> float:
    """Return a ranking boost from the centralized strategy boost table."""
    src = card.get("semantic_source", "")
    return _KPI_SOURCE_BOOST_BY_INTENT.get(strategy_intent_type, {}).get(src, 0.0)


def _intent_boost(card: dict, intent_lowered: str) -> float:
    """Return a ranking boost [0.0, 2.0] for a KPI card based on intent keywords.

    Cards whose semantic_source matches the intent get a positive boost.
    Boost is additive — multiple phrase matches compound up to the cap.
    No card values are modified; only sort order changes.
    """
    if not intent_lowered:
        return 0.0
    src   = card.get("semantic_source", "")
    total = 0.0
    for phrase, boosted_sources in _INTENT_KPI_BOOST:
        if phrase in intent_lowered and src in boosted_sources:
            total += 1.0
    return min(total, 2.0)


def rank_kpis(
    cards: list[dict],
    dataset_type: str,
    max_kpis: int = 8,
    intent_text: str | None = None,
    strategy_intent_type: str | None = None,
) -> list[dict]:
    """Sort KPI cards by priority order and select the top N.

    Ordering: executive first, then by semantic_source priority, then confidence desc.
    Deduplication: only one card per semantic_source type is kept unless the
    dataset_type explicitly needs multiple (e.g. sales_financial shows both revenue + cost).
    """
    if not cards:
        return []

    # Allow multiple cards from the same source for financial-heavy datasets
    multi_allowed = {"revenue", "profit", "cost"} if dataset_type in ("sales_financial", "financial_ops") else {"revenue"}

    seen_sources: dict[str, int] = {}
    filtered: list[dict] = []
    for card in cards:
        src = card.get("semantic_source", "unknown")
        count = seen_sources.get(src, 0)
        max_per_source = 8 if src == "measure" else (3 if src in multi_allowed else 1)
        if count < max_per_source:
            filtered.append(card)
            seen_sources[src] = count + 1

    if strategy_intent_type is not None:
        max_kpis = _KPI_MAX_BY_INTENT.get(strategy_intent_type, max_kpis)

    intent_lowered = (intent_text or "").lower()
    if strategy_intent_type is not None:
        filtered.sort(key=lambda c: (
            -_strategy_boost(c, strategy_intent_type),
            _PRIORITY_ORDER.get(c.get("priority", "operational"), 9),
            _SEMANTIC_PRIORITY.get(c.get("semantic_source", "unknown"), 99),
            -c.get("confidence", 0),
        ))
    else:
        filtered.sort(key=lambda c: (
            -_intent_boost(c, intent_lowered),                               # higher boost → sorts first
            _PRIORITY_ORDER.get(c.get("priority", "operational"), 9),
            _SEMANTIC_PRIORITY.get(c.get("semantic_source", "unknown"), 99),
            -c.get("confidence", 0),
        ))

    return filtered[:max_kpis]
