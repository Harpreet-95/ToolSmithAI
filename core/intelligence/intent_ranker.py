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

    intent_lowered = (intent_text or "").lower()
    filtered.sort(key=lambda c: (
        -_intent_boost(c, intent_lowered),                               # higher boost → sorts first
        _PRIORITY_ORDER.get(c.get("priority", "operational"), 9),
        _SEMANTIC_PRIORITY.get(c.get("semantic_source", "unknown"), 99),
        -c.get("confidence", 0),
    ))

    return filtered[:max_kpis]
