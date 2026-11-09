"""
core/intelligence/metric_discovery_engine.py

Enterprise Metric Discovery Engine for ToolSmithAI — Phase 1 + 2.

Phase 1: raw pass-through — enriches each role_profile item with unit detection,
discovery scoring, and tier classification.

Phase 2: derived metric generation — emits additional MONEY÷COUNT ratio,
MONEY−MONEY difference, and COUNT÷COUNT rate candidates using pre-computed
values from numeric_profile stats. No AI. Deterministic only.

No ML. No AI calls. Deterministic rule-based only.
Standalone — imports nothing from core.*

Public interface:
    discover_metrics(role_profile, numeric_profile, row_count) -> list[dict]

Raw candidate output extends each input item with:
    candidate_type    str        "raw"
    source_columns    list[str]  [column]
    derivation        None
    formula           None
    unit              str|None
    unit_system       str|None
    business_concept  None
    concept_confidence float     0.0
    discovery_score   float      0.0–1.0
    score_components  dict
    discovery_tier    int        1 | 2 | 3 | 0

Derived candidate output adds:
    derived_value     float      pre-computed result (values are pre-scaled for formatting)
    derived_format    str        format_type override: "currency" | "percent"
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

# ── Phase 1 scoring constants ──────────────────────────────────────────────────

_W_ROLE       = 0.40   # weight: role_confidence contribution
_W_SEMANTIC   = 0.20   # weight: semantic classifier confidence contribution
_W_UNIT_MATCH = 0.15   # bonus: unit token detected AND matches expected role unit_system
_W_UNIT_TOKEN = 0.10   # bonus: unit token detected but role has no unit expectation
_W_COMP_FULL  = 0.10   # bonus: null_count == 0 (or absent — old dataset)
_W_COMP_HIGH  = 0.05   # bonus: null_count / row_count < 0.10

# Tier thresholds (shared by Phase 1 raw and Phase 2 derived candidates)
_TIER1_MIN = 0.75
_TIER2_MIN = 0.55
_TIER3_MIN = 0.40

# ── Phase 2 derivation constants ───────────────────────────────────────────────

_DERIVED_PENALTY        = 0.70   # all derived scores scaled down vs raw sources
_DERIVATION_WEIGHTS: dict[str, float] = {
    "ratio_money_count": 0.90,   # MONEY ÷ COUNT — high business value
    "difference_money":  0.85,   # MONEY − MONEY
    "ratio_count_count": 0.80,   # COUNT ÷ COUNT
}

_MIN_SOURCE_TIER        = 2      # both source candidates must reach at least this tier
_MAX_RATIO_MONEY_COUNT  = 3      # cap: MONEY ÷ COUNT pairs
_MAX_DIFF_MONEY         = 2      # cap: MONEY − MONEY pairs
_MAX_RATIO_COUNT_COUNT  = 2      # cap: COUNT ÷ COUNT pairs
_MAX_DERIVED_TOTAL      = 8      # global cap across all derivation rules


# ── Phase 1 internal helpers ───────────────────────────────────────────────────

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


# ── Phase 1 per-item enrichment ────────────────────────────────────────────────

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


# ── Phase 2 helpers ────────────────────────────────────────────────────────────

def _safe_divide(
    numerator: float | None,
    denominator: float | None,
) -> float | None:
    """Divide numerator by denominator. Returns None on any unsafe condition."""
    if numerator is None or denominator is None:
        return None
    if denominator == 0.0:
        return None
    try:
        result = numerator / denominator
        return result if math.isfinite(result) else None
    except (TypeError, ZeroDivisionError, OverflowError):
        return None


def _stat_for_col(numeric_profile: dict, col: str, preferred: str = "sum") -> float | None:
    """Return preferred stat for col, falling back to mean. Returns None when absent."""
    stats = numeric_profile.get(col, {})
    v = _safe_float(stats.get(preferred))
    if v is None:
        v = _safe_float(stats.get("mean"))
    return v


def _synthetic_col(op: str, col_a: str, col_b: str) -> str:
    """Build a synthetic column name for a derived metric."""
    if op == "ratio":
        return f"{col_a}_per_{col_b}"
    if op == "difference":
        return f"{col_a}_minus_{col_b}"
    return f"{col_a}_{op}_{col_b}"


def _derived_score_and_tier(
    score_a: float,
    score_b: float,
    derivation_key: str,
) -> tuple[float, dict, int]:
    """Return (discovery_score, score_components, discovery_tier) for a derived candidate.

    Score is bounded by the weaker source (min) then penalised.
    Derived metrics cannot exceed Tier 2 due to _DERIVED_PENALTY.
    """
    weight    = _DERIVATION_WEIGHTS.get(derivation_key, 0.80)
    raw_score = min(score_a, score_b) * _DERIVED_PENALTY * weight
    score     = round(min(max(raw_score, 0.0), 1.0), 3)

    components = {
        "role":        0.0,
        "semantic":    0.0,
        "derivation":  score,
        "completeness": 0.0,
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


def _make_derived(
    col_a: str,
    col_b: str,
    op: str,
    role: str,
    aggregation: str,
    derived_value: float,
    derived_format: str,
    unit: str | None,
    unit_system: str | None,
    role_confidence: float,
    score: float,
    components: dict,
    tier: int,
) -> dict:
    """Build a derived MetricCandidate dict."""
    sep     = " / " if op == "ratio" else " - "
    formula = f"{col_a}{sep}{col_b}"
    return {
        # Minimal semantic profile fields — no original item to copy from
        "column":              _synthetic_col(op, col_a, col_b),
        "semantic_type":       "unknown",
        "confidence":          0.0,
        "likely_id":           False,
        "percentage_like":     False,
        # Role classifier fields — assigned by derivation rule
        "role":                role,
        "role_confidence":     round(role_confidence, 3),
        "aggregation":         aggregation,
        "multi_card":          False,
        "eligible_by_default": True,
        # Phase 1 discovery fields
        "candidate_type":      "derived",
        "source_columns":      [col_a, col_b],
        "derivation":          op,
        "formula":             formula,
        "unit":                unit,
        "unit_system":         unit_system,
        "business_concept":    None,
        "concept_confidence":  0.0,
        "discovery_score":     score,
        "score_components":    components,
        "discovery_tier":      tier,
        # Phase 2 fields
        "derived_value":       derived_value,
        "derived_format":      derived_format,
    }


# ── Phase 2 derivation rules ───────────────────────────────────────────────────

def _generate_ratio_money_count(
    raw_candidates: list[dict],
    numeric_profile: dict,
) -> list[dict]:
    """MONEY ÷ COUNT → currency-per-unit ratio candidates. Cap: _MAX_RATIO_MONEY_COUNT."""
    money  = [c for c in raw_candidates
              if c.get("role") == "MONEY"
              and 0 < c.get("discovery_tier", 0) <= _MIN_SOURCE_TIER]
    counts = [c for c in raw_candidates
              if c.get("role") == "COUNT"
              and 0 < c.get("discovery_tier", 0) <= _MIN_SOURCE_TIER]
    if not money or not counts:
        return []

    pairs: list[tuple[float, dict]] = []
    for mc in money:
        for cc in counts:
            col_a = mc.get("column", "")
            col_b = cc.get("column", "")
            if not col_a or not col_b or col_a == col_b:
                continue

            val = _safe_divide(
                _stat_for_col(numeric_profile, col_a, "sum"),
                _stat_for_col(numeric_profile, col_b, "sum"),
            )
            if val is None:
                continue

            score, components, tier = _derived_score_and_tier(
                mc.get("discovery_score", 0.0),
                cc.get("discovery_score", 0.0),
                "ratio_money_count",
            )
            combined = mc.get("discovery_score", 0.0) + cc.get("discovery_score", 0.0)

            pairs.append((combined, _make_derived(
                col_a          = col_a,
                col_b          = col_b,
                op             = "ratio",
                role           = "MONEY",
                aggregation    = "mean",
                derived_value  = val,
                derived_format = "currency",
                unit           = mc.get("unit"),
                unit_system    = mc.get("unit_system"),
                role_confidence = min(
                    mc.get("role_confidence", 0.0),
                    cc.get("role_confidence", 0.0),
                ),
                score          = score,
                components     = components,
                tier           = tier,
            )))

    pairs.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in pairs[:_MAX_RATIO_MONEY_COUNT]]


def _generate_difference_money(
    raw_candidates: list[dict],
    numeric_profile: dict,
) -> list[dict]:
    """MONEY − MONEY → currency difference candidates. Cap: _MAX_DIFF_MONEY."""
    money = [c for c in raw_candidates
             if c.get("role") == "MONEY"
             and c.get("unit_system") == "currency"
             and 0 < c.get("discovery_tier", 0) <= _MIN_SOURCE_TIER]
    if len(money) < 2:
        return []

    pairs: list[tuple[float, dict]] = []
    for i, mc_a in enumerate(money):
        for mc_b in money[i + 1:]:
            col_a = mc_a.get("column", "")
            col_b = mc_b.get("column", "")
            if not col_a or not col_b:
                continue

            val_a = _stat_for_col(numeric_profile, col_a, "sum")
            val_b = _stat_for_col(numeric_profile, col_b, "sum")
            if val_a is None or val_b is None:
                continue
            diff = val_a - val_b
            if not math.isfinite(diff):
                continue

            score, components, tier = _derived_score_and_tier(
                mc_a.get("discovery_score", 0.0),
                mc_b.get("discovery_score", 0.0),
                "difference_money",
            )
            combined = mc_a.get("discovery_score", 0.0) + mc_b.get("discovery_score", 0.0)

            pairs.append((combined, _make_derived(
                col_a          = col_a,
                col_b          = col_b,
                op             = "difference",
                role           = "MONEY",
                aggregation    = "sum",
                derived_value  = diff,
                derived_format = "currency",
                unit           = mc_a.get("unit"),
                unit_system    = "currency",
                role_confidence = min(
                    mc_a.get("role_confidence", 0.0),
                    mc_b.get("role_confidence", 0.0),
                ),
                score          = score,
                components     = components,
                tier           = tier,
            )))

    pairs.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in pairs[:_MAX_DIFF_MONEY]]


def _generate_ratio_count_count(
    raw_candidates: list[dict],
    numeric_profile: dict,
) -> list[dict]:
    """COUNT ÷ COUNT → proportion rate candidates (pre-scaled to 0–100). Cap: _MAX_RATIO_COUNT_COUNT."""
    counts = [c for c in raw_candidates
              if c.get("role") == "COUNT"
              and 0 < c.get("discovery_tier", 0) <= _MIN_SOURCE_TIER]
    if len(counts) < 2:
        return []

    pairs: list[tuple[float, dict]] = []
    for i, cc_a in enumerate(counts):
        for cc_b in counts[i + 1:]:
            col_a = cc_a.get("column", "")
            col_b = cc_b.get("column", "")
            if not col_a or not col_b:
                continue

            sum_a = _stat_for_col(numeric_profile, col_a, "sum")
            sum_b = _stat_for_col(numeric_profile, col_b, "sum")
            if sum_a is None or sum_b is None:
                continue

            # Orient so numerator ≤ denominator (rate must be in (0, 1])
            if sum_a <= sum_b:
                num_col, den_col, numerator, denominator = col_a, col_b, sum_a, sum_b
            else:
                num_col, den_col, numerator, denominator = col_b, col_a, sum_b, sum_a

            val = _safe_divide(numerator, denominator)
            if val is None or not (0.0 < val <= 1.0):
                continue

            score, components, tier = _derived_score_and_tier(
                cc_a.get("discovery_score", 0.0),
                cc_b.get("discovery_score", 0.0),
                "ratio_count_count",
            )
            combined = cc_a.get("discovery_score", 0.0) + cc_b.get("discovery_score", 0.0)

            pairs.append((combined, _make_derived(
                col_a          = num_col,
                col_b          = den_col,
                op             = "ratio",
                role           = "RATIO",
                aggregation    = "mean",
                derived_value  = round(val * 100.0, 4),   # pre-scaled: 0.035 → 3.5
                derived_format = "percent",
                unit           = "%",
                unit_system    = "proportion",
                role_confidence = min(
                    cc_a.get("role_confidence", 0.0),
                    cc_b.get("role_confidence", 0.0),
                ),
                score          = score,
                components     = components,
                tier           = tier,
            )))

    pairs.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in pairs[:_MAX_RATIO_COUNT_COUNT]]


def _apply_derived_cap(derived: list[dict]) -> list[dict]:
    """Enforce global derived candidate cap, keeping highest-scoring candidates."""
    if len(derived) <= _MAX_DERIVED_TOTAL:
        return derived
    return sorted(derived, key=lambda d: d.get("discovery_score", 0.0), reverse=True)[
        :_MAX_DERIVED_TOTAL
    ]


# ── Public interface ───────────────────────────────────────────────────────────

def discover_metrics(
    role_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Enrich role_profile items and generate derived metric candidates.

    Phase 1: every input item is returned as a raw candidate with unit,
    scoring, and tier fields added.

    Phase 2: derived candidates (MONEY÷COUNT, MONEY−MONEY, COUNT÷COUNT)
    are appended after the raw candidates. Derived candidates carry
    derived_value (pre-computed) and derived_format (format_type override).

    Returns [] on bad input. Never raises.
    """
    if not isinstance(role_profile, list):
        return []
    numeric_profile = numeric_profile or {}

    # Phase 1 — enrich raw candidates
    raw_candidates: list[dict] = [
        _enrich_one(item, numeric_profile, row_count)
        for item in role_profile
    ]

    # Phase 2 — derive additional candidates from raw pairs
    # Wrapped in try/except so Phase 1 output is never affected by Phase 2 errors.
    derived: list[dict] = []
    try:
        derived += _generate_ratio_money_count(raw_candidates, numeric_profile)
        derived += _generate_difference_money(raw_candidates, numeric_profile)
        derived += _generate_ratio_count_count(raw_candidates, numeric_profile)
        derived  = _apply_derived_cap(derived)
    except Exception:
        derived = []

    return raw_candidates + derived
