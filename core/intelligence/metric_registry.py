"""
core/intelligence/metric_registry.py

Metric Registry for ToolSmithAI.

Single source of truth for KPI display rules. Maps (role, aggregation)
pairs to their label template, format type, priority, and card limits.

No ML. No AI calls. Deterministic static data only.
Standalone — imports nothing from core.*

Public interface:
    get_metric_definition(role, aggregation=None) -> dict | None
    get_metric_definitions()                       -> list[dict]

Each definition dict contains:
    role            str   — canonical role name (e.g. "MONEY")
    aggregation     str   — "sum" | "mean" | "dominant"
    label_template  str   — display label; {col} is replaced by the column name
    format_type     str   — "currency" | "number" | "percent" | "decimal"
    priority        str   — "executive" | "operational" | "risk"
    positive_good   bool  — True = upward trend is good (green); False = bad (red)
    max_cards       int   — max KPI cards this definition may emit per report
"""

# ── Registry ───────────────────────────────────────────────────────────────────
# Ordered: within each role, primary aggregation comes first.
# get_metric_definition(role) without aggregation returns the first match.

_REGISTRY: list[dict] = [

    # ── MONEY ──────────────────────────────────────────────────────────────────
    {
        "role":           "MONEY",
        "aggregation":    "sum",
        "label_template": "Total {col}",
        "format_type":    "currency",
        "priority":       "executive",
        "positive_good":  True,
        "max_cards":      3,
    },
    {
        "role":           "MONEY",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "currency",
        "priority":       "executive",
        "positive_good":  True,
        "max_cards":      1,
    },

    # ── COUNT ───────────────────────────────────────────────────────────────────
    {
        "role":           "COUNT",
        "aggregation":    "sum",
        "label_template": "Total {col}",
        "format_type":    "number",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      2,
    },
    {
        "role":           "COUNT",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "decimal",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      1,
    },

    # ── PERCENT ─────────────────────────────────────────────────────────────────
    {
        "role":           "PERCENT",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "percent",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      1,
    },

    # ── RATIO ───────────────────────────────────────────────────────────────────
    {
        "role":           "RATIO",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "percent",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      1,
    },

    # ── SCORE ───────────────────────────────────────────────────────────────────
    {
        "role":           "SCORE",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "decimal",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      2,
    },

    # ── DURATION ────────────────────────────────────────────────────────────────
    {
        "role":           "DURATION",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "decimal",
        "priority":       "operational",
        "positive_good":  False,
        "max_cards":      1,
    },

    # ── DISTANCE ────────────────────────────────────────────────────────────────
    {
        "role":           "DISTANCE",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "decimal",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      1,
    },

    # ── WEIGHT ──────────────────────────────────────────────────────────────────
    {
        "role":           "WEIGHT",
        "aggregation":    "sum",
        "label_template": "Total {col}",
        "format_type":    "number",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      1,
    },

    # ── RISK ────────────────────────────────────────────────────────────────────
    {
        "role":           "RISK",
        "aggregation":    "mean",
        "label_template": "Avg Risk: {col}",
        "format_type":    "decimal",
        "priority":       "risk",
        "positive_good":  False,
        "max_cards":      1,
    },

    # ── DIMENSION ───────────────────────────────────────────────────────────────
    {
        "role":           "DIMENSION",
        "aggregation":    "dominant",
        "label_template": "Top {col}",
        "format_type":    "percent",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      3,
    },

    # ── GENERIC_MEASURE ─────────────────────────────────────────────────────────
    {
        "role":           "GENERIC_MEASURE",
        "aggregation":    "mean",
        "label_template": "Avg {col}",
        "format_type":    "decimal",
        "priority":       "operational",
        "positive_good":  True,
        "max_cards":      8,
    },
]

# ── Lookup indices (built once at import time) ─────────────────────────────────

# (role, aggregation) → definition
_BY_ROLE_AGG: dict[tuple[str, str], dict] = {
    (d["role"], d["aggregation"]): d
    for d in _REGISTRY
}

# role → first definition for that role (primary aggregation)
_BY_ROLE: dict[str, dict] = {}
for _d in _REGISTRY:
    _BY_ROLE.setdefault(_d["role"], _d)


# ── Public interface ───────────────────────────────────────────────────────────

def get_metric_definition(role: str, aggregation: str | None = None) -> dict | None:
    """Return the metric definition for a role and optional aggregation.

    When aggregation is None, returns the primary definition for the role
    (first entry in _REGISTRY for that role).

    Returns None when no matching definition exists.
    """
    if aggregation is not None:
        return _BY_ROLE_AGG.get((role, aggregation))
    return _BY_ROLE.get(role)


def get_metric_definitions() -> list[dict]:
    """Return all metric definitions as a shallow copy of the registry."""
    return list(_REGISTRY)
