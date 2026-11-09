"""
core/intelligence/metric_role_classifier.py

Metric Role Classifier for ToolSmithAI.

Maps each column in a semantic_profile to a canonical metric role.
Role is the enterprise KPI layer between semantic_classifier output
and KPI eligibility / computation decisions.

No ML. No AI calls. Deterministic rule-based only.
Standalone — imports nothing from core.*

Public interface:
    classify_metric_roles(semantic_profile) -> list[dict]

Input:
    semantic_profile  list[dict] — output of semantic_classifier.classify_columns()

Output:
    Same list with five additional keys per item:
        role                str   — canonical role constant (e.g. "MONEY", "COUNT")
        role_confidence     float — confidence in the role assignment (0.0–1.0)
        aggregation         str   — "sum" | "mean" | "dominant" | "none"
        multi_card          bool  — True when the role can produce > 1 KPI card
        eligible_by_default bool  — False only for IDENTIFIER and TEMPORAL_AXIS
"""

import re

# ── Canonical role constants ───────────────────────────────────────────────────

IDENTIFIER      = "IDENTIFIER"       # primary keys, surrogate ids, zip/postal codes
TEMPORAL_AXIS   = "TEMPORAL_AXIS"    # date, timestamp, year, quarter, month columns
ATTRIBUTE       = "ATTRIBUTE"        # descriptive scalars: age, tenure, seniority
MONEY           = "MONEY"            # revenue, cost, profit, amount, price
COUNT           = "COUNT"            # quantity, units, orders — additive whole numbers
PERCENT         = "PERCENT"          # percentage columns (0–100 or 0–1 range)
RATIO           = "RATIO"            # dimensionless ratios and rates
SCORE           = "SCORE"            # scores, ratings, indices
DURATION        = "DURATION"         # elapsed time: days, hours, minutes
DISTANCE        = "DISTANCE"         # spatial measurements: km, miles, meters
WEIGHT          = "WEIGHT"           # mass measurements: kg, lbs, tonnes
RISK            = "RISK"             # risk scores, severity, criticality
DIMENSION       = "DIMENSION"        # categorical grouping columns
GENERIC_MEASURE = "GENERIC_MEASURE"  # numeric columns with no stronger signal

# ── Role metadata ──────────────────────────────────────────────────────────────

_ROLE_META: dict[str, dict] = {
    IDENTIFIER:      {"aggregation": "none",     "multi_card": False, "eligible_by_default": False},
    TEMPORAL_AXIS:   {"aggregation": "none",     "multi_card": False, "eligible_by_default": False},
    ATTRIBUTE:       {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    MONEY:           {"aggregation": "sum",      "multi_card": True,  "eligible_by_default": True},
    COUNT:           {"aggregation": "sum",      "multi_card": True,  "eligible_by_default": True},
    PERCENT:         {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    RATIO:           {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    SCORE:           {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    DURATION:        {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    DISTANCE:        {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    WEIGHT:          {"aggregation": "sum",      "multi_card": False, "eligible_by_default": True},
    RISK:            {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
    DIMENSION:       {"aggregation": "dominant", "multi_card": False, "eligible_by_default": True},
    GENERIC_MEASURE: {"aggregation": "mean",     "multi_card": False, "eligible_by_default": True},
}

# ── Primary dispatch: semantic_type → role ────────────────────────────────────
# Covers all 21 types produced by semantic_classifier.classify_columns().

_SEMANTIC_TYPE_TO_ROLE: dict[str, str] = {
    # Identifiers
    "id":         IDENTIFIER,
    # Temporal
    "date":       TEMPORAL_AXIS,
    "timestamp":  TEMPORAL_AXIS,
    # Financial
    "revenue":    MONEY,
    "cost":       MONEY,
    "profit":     MONEY,
    "amount":     MONEY,
    "price":      MONEY,
    # Volume
    "quantity":   COUNT,
    # Proportion
    "percentage": PERCENT,
    # Scored / rated
    "score":      SCORE,
    # Risk
    "risk":       RISK,
    # Categorical dimensions
    "product":    DIMENSION,
    "category":   DIMENSION,
    "customer":   DIMENSION,
    "employee":   DIMENSION,
    "region":     DIMENSION,
    "country":    DIMENSION,
    "state":      DIMENSION,
    "city":       DIMENSION,
    "status":     DIMENSION,
}

# ── Token-based fallback rules ────────────────────────────────────────────────
# Applied in order only when semantic_type == "unknown".
# Each entry: (role, signal_tokens, role_confidence_override)
# First match wins; tokens are matched against the set of column name tokens.

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATOR      = re.compile(r"[\s_\-\.\/\\]+")


def _tokenize(name: str) -> set[str]:
    expanded = _CAMEL_BOUNDARY.sub("_", name.strip())
    raw      = _SEPARATOR.split(expanded)
    return {t.lower() for t in raw if t and t.isalpha()}


_TOKEN_FALLBACK_RULES: list[tuple[str, frozenset, float]] = [
    # IDENTIFIER — id-like tokens not caught by semantic classifier
    (IDENTIFIER,    frozenset({"id", "key", "uuid", "guid", "pk", "fk",
                               "zip", "postal", "code", "ref", "num"}),       0.70),
    # TEMPORAL_AXIS — date-like tokens below confidence threshold
    (TEMPORAL_AXIS, frozenset({"year", "quarter", "month", "week", "day",
                               "dt", "fiscal", "date", "period"}),            0.70),
    # ATTRIBUTE — descriptive scalar (age, tenure, experience)
    (ATTRIBUTE,     frozenset({"age", "tenure", "seniority",
                               "experience", "years"}),                        0.65),
    # COUNT — volume/frequency columns not caught by semantic classifier
    (COUNT,         frozenset({"count", "num", "records", "transactions",
                               "visits", "sessions", "events", "calls",
                               "occurrences", "instances"}),                   0.65),
    # RATIO — dimensionless rate/ratio columns unclassified by semantic layer
    (RATIO,         frozenset({"ratio", "rate", "proportion",
                               "fraction", "share"}),                          0.65),
    # DURATION — elapsed time measurements
    (DURATION,      frozenset({"duration", "days", "hours", "minutes",
                               "seconds", "elapsed", "lag", "lead",
                               "wait", "ttl", "ttm"}),                         0.65),
    # DISTANCE — spatial measurements
    (DISTANCE,      frozenset({"distance", "km", "miles", "meters",
                               "radius", "dist"}),                             0.60),
    # WEIGHT — mass measurements
    (WEIGHT,        frozenset({"weight", "kg", "lbs", "mass",
                               "tonnes", "grams", "load"}),                    0.60),
]


def _role_from_tokens(column: str) -> tuple[str, float] | None:
    """Return (role, confidence) from column name tokens, or None if no rule fires."""
    tokens = _tokenize(column)
    if not tokens:
        return None
    for role, signal_tokens, conf in _TOKEN_FALLBACK_RULES:
        if tokens & signal_tokens:
            return role, conf
    return None


# ── Per-column classifier ──────────────────────────────────────────────────────

def _classify_one(item: dict) -> dict:
    """Return item extended with role fields. Never raises."""
    try:
        sem_type   = item.get("semantic_type", "unknown")
        confidence = float(item.get("confidence", 0.0))
        column     = item.get("column", "")

        # 1. Direct semantic_type → role (covers all 21 known types)
        if sem_type in _SEMANTIC_TYPE_TO_ROLE:
            role            = _SEMANTIC_TYPE_TO_ROLE[sem_type]
            role_confidence = round(confidence, 3)

        # 2. likely_id flag on unknown column → IDENTIFIER
        elif item.get("likely_id"):
            role            = IDENTIFIER
            role_confidence = round(min(confidence + 0.10, 0.90), 3)

        # 3. Token-based fallback for unknown columns
        else:
            token_result = _role_from_tokens(column)
            if token_result:
                role, role_confidence = token_result[0], round(token_result[1], 3)
            else:
                # 4. No signal found — treat as generic numeric measure
                role            = GENERIC_MEASURE
                role_confidence = round(min(confidence, 0.45), 3)

        meta = _ROLE_META[role]
        return {
            **item,
            "role":                role,
            "role_confidence":     role_confidence,
            "aggregation":         meta["aggregation"],
            "multi_card":          meta["multi_card"],
            "eligible_by_default": meta["eligible_by_default"],
        }

    except Exception:
        return {
            **item,
            "role":                GENERIC_MEASURE,
            "role_confidence":     0.0,
            "aggregation":         "mean",
            "multi_card":          False,
            "eligible_by_default": False,
        }


# ── Public interface ───────────────────────────────────────────────────────────

def classify_metric_roles(semantic_profile: list[dict]) -> list[dict]:
    """Extend each semantic_profile item with role classification fields.

    Preserves original list order. Never raises. Returns [] on bad input.
    """
    if not isinstance(semantic_profile, list):
        return []
    return [_classify_one(item) for item in semantic_profile]
