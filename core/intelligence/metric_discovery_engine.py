"""
core/intelligence/metric_discovery_engine.py

Enterprise Metric Discovery Engine for ToolSmithAI — Phase 1.

Enriches each role_profile item with unit detection, discovery scoring,
and tier classification. Phase 1 is raw pass-through only: no derived
metrics, no business concept detection.

No ML. No AI calls. Deterministic rule-based only.
Standalone — imports nothing from core.*

Public interface:
    discover_metrics(role_profile, numeric_profile, row_count) -> list[dict]

Each item in the output extends the input item with:
    candidate_type    str        always "raw" in Phase 1
    source_columns    list[str]  always [column] in Phase 1
    derivation        None       reserved for Phase 3 derived metrics
    formula           None       reserved for Phase 3 derived metrics
    unit              str|None   e.g. "USD", "days", "km", "kg", "%"
    unit_system       str|None   e.g. "currency", "time", "distance", "weight", "proportion"
    business_concept  None       reserved for Phase 4 concept detection
    concept_confidence float     always 0.0 in Phase 1
    discovery_score   float      composite score 0.0–1.0
    score_components  dict       per-component breakdown
    discovery_tier    int        1 | 2 | 3 | 0  (0 = below threshold)
"""

import math
import re

# ── Tokenizer ──────────────────────────────────────────────────────────────────

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATOR      = re.compile(r"[\s_\-\.\/\\]+")


def _tokenize(name: str) -> set[str]:
    expanded = _CAMEL_BOUNDARY.sub("_", name.strip())
    raw      = _SEPARATOR.split(expanded)
    return {t.lower() for t in raw if t and t.isalpha()}


# ── Unit token map ─────────────────────────────────────────────────────────────
# Ordered: more specific or higher-priority units listed first.
# First matching entry wins within _detect_unit_from_tokens.

_UNIT_TOKEN_MAP: list[tuple[str, frozenset]] = [
    # Currency
    ("USD",   frozenset({"revenue", "cost", "profit", "amount", "price",
                         "fee", "usd", "dollar", "dollars", "salary",
                         "wage", "budget", "spend", "earning", "income",
                         "payment", "invoice"})),
    ("EUR",   frozenset({"eur", "euro", "euros"})),
    ("GBP",   frozenset({"gbp", "sterling"})),
    # Time
    ("days",  frozenset({"days", "day", "duration", "lag", "lead",
                         "ttl", "ttm", "elapsed"})),
    ("hours", frozenset({"hours", "hour", "hrs"})),
    # Distance
    ("km",    frozenset({"km", "kilometers", "kilometres", "dist", "distance"})),
    ("miles", frozenset({"miles", "mi", "mile"})),
    # Weight
    ("kg",    frozenset({"kg", "kilograms", "kilo", "kilos", "mass"})),
    ("lbs",   frozenset({"lbs", "lb"})),
    # Proportion — after currency/time so "rate" doesn't steal from more specific matches
    ("%",     frozenset({"percent", "pct", "rate", "ratio", "fraction",
                         "proportion", "share", "percentage", "margin"})),
    # Volume / count
    ("count", frozenset({"count", "num", "qty", "quantity", "units",
                         "items", "orders", "transactions", "records"})),
]

_UNIT_SYSTEM: dict[str, str] = {
    "USD":   "currency",
    "EUR":   "currency",
    "GBP":   "currency",
    "days":  "time",
    "hours": "time",
    "km":    "distance",
    "miles": "distance",
    "kg":    "weight",
    "lbs":   "weight",
    "%":     "proportion",
    "count": "volume",
}

# Expected unit_system per role — used to award the full unit bonus when they match.
_ROLE_EXPECTED_UNIT_SYSTEM: dict[str, str] = {
    "MONEY":    "currency",
    "DURATION": "time",
    "DISTANCE": "distance",
    "WEIGHT":   "weight",
    "PERCENT":  "proportion",
    "RATIO":    "proportion",
    "COUNT":    "volume",
}

# ── Scoring constants ──────────────────────────────────────────────────────────

_W_ROLE       = 0.40   # weight: role_confidence contribution
_W_SEMANTIC   = 0.20   # weight: semantic classifier confidence contribution
_W_UNIT_MATCH = 0.15   # bonus: unit token detected AND matches expected role unit_system
_W_UNIT_TOKEN = 0.10   # bonus: unit token detected but role has no unit expectation
_W_COMP_FULL  = 0.10   # bonus: null_count == 0 (or absent — old dataset)
_W_COMP_HIGH  = 0.05   # bonus: null_count / row_count < 0.10

# Tier thresholds
_TIER1_MIN = 0.75
_TIER2_MIN = 0.55
_TIER3_MIN = 0.40


# ── Internal helpers ───────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _detect_unit_from_tokens(column: str) -> tuple[str | None, str | None, bool]:
    """Return (unit, unit_system, found) via column name token matching.

    Returns (None, None, False) when no rule fires.
    """
    tokens = _tokenize(column)
    if not tokens:
        return None, None, False
    for unit, signal_tokens in _UNIT_TOKEN_MAP:
        if tokens & signal_tokens:
            return unit, _UNIT_SYSTEM[unit], True
    return None, None, False


def _detect_unit_from_values(
    role: str,
    stats: dict,
    percentage_like: bool,
) -> tuple[str | None, str | None]:
    """Value-range heuristic — applied only when token detection finds nothing.

    Returns (unit, unit_system) or (None, None).
    """
    mean = _safe_float(stats.get("mean"))
    if mean is None:
        return None, None

    if percentage_like and 0.0 <= mean <= 1.0:
        return "%", "proportion"
    if role == "MONEY" and 100.0 <= mean <= 10_000_000.0:
        return "USD", "currency"
    if role == "DURATION" and 0.0 < mean <= 3_650.0:
        return "days", "time"
    return None, None


def _completeness_bonus(stats: dict, row_count: int) -> float:
    """Return completeness proxy bonus.

    When null_count is absent (old dataset), assumes fully complete.
    """
    if row_count <= 0:
        return 0.0
    null_count = _safe_float(stats.get("null_count"))
    if null_count is None:
        return _W_COMP_FULL          # absent → treat as fully complete
    if null_count == 0.0:
        return _W_COMP_FULL
    if null_count / row_count < 0.10:
        return _W_COMP_HIGH
    return 0.0


def _compute_score_and_tier(
    role: str,
    role_confidence: float,
    semantic_confidence: float,
    unit: str | None,
    unit_system: str | None,
    unit_from_tokens: bool,
    stats: dict,
    row_count: int,
) -> tuple[float, dict, int]:
    """Return (discovery_score, score_components, discovery_tier)."""
    role_part     = round(role_confidence     * _W_ROLE,    4)
    semantic_part = round(semantic_confidence * _W_SEMANTIC, 4)

    # Unit bonus: full when token found and unit_system matches expected for role;
    # partial when token found but no role expectation exists;
    # heuristic-only earns the lower partial weight.
    expected_system = _ROLE_EXPECTED_UNIT_SYSTEM.get(role)
    if unit is not None and unit_from_tokens:
        if expected_system and unit_system == expected_system:
            unit_part = _W_UNIT_MATCH
        else:
            unit_part = _W_UNIT_TOKEN
    elif unit is not None:            # heuristic detection only
        unit_part = _W_UNIT_TOKEN
    else:
        unit_part = 0.0

    comp_part = _completeness_bonus(stats, row_count)

    raw   = role_part + semantic_part + unit_part + comp_part
    score = round(min(max(raw, 0.0), 1.0), 3)

    components = {
        "role":         role_part,
        "semantic":     semantic_part,
        "unit":         unit_part,
        "completeness": comp_part,
    }

    if score >= _TIER1_MIN:
        tier = 1
    elif score >= _TIER2_MIN:
        tier = 2
    elif score >= _TIER3_MIN:
        tier = 3
    else:
        tier = 0

    return score, components, tier


# ── Per-item enrichment ────────────────────────────────────────────────────────

def _enrich_one(item: dict, numeric_profile: dict, row_count: int) -> dict:
    """Enrich a single role_profile item with discovery fields. Never raises."""
    try:
        col             = item.get("column", "")
        role            = item.get("role", "GENERIC_MEASURE")
        role_confidence = float(item.get("role_confidence", 0.0))
        sem_confidence  = float(item.get("confidence", 0.0))
        pct_like        = bool(item.get("percentage_like", False))

        stats = numeric_profile.get(col, {})

        unit, unit_system, unit_from_tokens = _detect_unit_from_tokens(col)
        if unit is None:
            unit, unit_system = _detect_unit_from_values(role, stats, pct_like)
            unit_from_tokens  = False

        discovery_score, score_components, discovery_tier = _compute_score_and_tier(
            role             = role,
            role_confidence  = role_confidence,
            semantic_confidence = sem_confidence,
            unit             = unit,
            unit_system      = unit_system,
            unit_from_tokens = unit_from_tokens,
            stats            = stats,
            row_count        = row_count,
        )

        return {
            **item,
            "candidate_type":    "raw",
            "source_columns":    [col],
            "derivation":        None,
            "formula":           None,
            "unit":              unit,
            "unit_system":       unit_system,
            "business_concept":  None,
            "concept_confidence": 0.0,
            "discovery_score":   discovery_score,
            "score_components":  score_components,
            "discovery_tier":    discovery_tier,
        }

    except Exception:
        return {
            **item,
            "candidate_type":    "raw",
            "source_columns":    [item.get("column", "")],
            "derivation":        None,
            "formula":           None,
            "unit":              None,
            "unit_system":       None,
            "business_concept":  None,
            "concept_confidence": 0.0,
            "discovery_score":   0.0,
            "score_components":  {
                "role": 0.0, "semantic": 0.0,
                "unit": 0.0, "completeness": 0.0,
            },
            "discovery_tier":    0,
        }


# ── Public interface ───────────────────────────────────────────────────────────

def discover_metrics(
    role_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Enrich role_profile items with unit, scoring, and tier fields.

    Phase 1: raw pass-through only. All incoming fields are preserved.
    Returns [] on bad input. Never raises.
    """
    if not isinstance(role_profile, list):
        return []
    numeric_profile = numeric_profile or {}
    return [
        _enrich_one(item, numeric_profile, row_count)
        for item in role_profile
    ]
