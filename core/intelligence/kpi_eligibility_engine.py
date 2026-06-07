"""
core/intelligence/kpi_eligibility_engine.py

KPI Eligibility Engine for ToolSmithAI.

Decides which columns from a role_profile qualify to become KPI cards.
Consolidates all per-role eligibility gates in one place so that no
downstream compute function re-implements completeness or stat checks.

No ML. No AI calls. Deterministic rule-based only.
Standalone — imports nothing from core.*

Public interface:
    evaluate_kpi_eligibility(role_profile, numeric_profile,
                             categorical_meta, row_count) -> list[dict]

Input:
    role_profile      list[dict]  — output of metric_role_classifier.classify_metric_roles()
    numeric_profile   dict        — col → {sum, mean, null_count, histogram_bins, …}
    categorical_meta  dict        — col → {unique_count, null_count, …}
    row_count         int         — total rows in the dataset

Output:
    Same list with four additional keys per item:
        completeness       float       — fraction of non-null values (0.0–1.0)
        eligibility_score  float       — composite gate score (0.0–1.0)
        eligible           bool        — True when the column passes all gates
        reason_excluded    str | None  — set only on excluded items; None when eligible
"""

import math

# ── Role classification constants (string literals, not imported) ──────────────
# Kept in sync with metric_role_classifier.py role names.

_ALWAYS_EXCLUDED  = frozenset({"IDENTIFIER", "TEMPORAL_AXIS"})
_ATTRIBUTE_ROLE   = "ATTRIBUTE"
_DIMENSION_ROLE   = "DIMENSION"

# Roles that are eligible and consult numeric_profile
_MONEY_COUNT_ROLES = frozenset({"MONEY", "COUNT"})          # accept sum OR mean
_MEAN_ONLY_ROLES   = frozenset({                             # require mean only
    "PERCENT", "RATIO", "SCORE", "RISK",
    "DURATION", "DISTANCE", "WEIGHT", "GENERIC_MEASURE",
})

# Minimum completeness thresholds per role
_MIN_COMPLETENESS: dict[str, float] = {
    "MONEY":          0.50,
    "COUNT":          0.50,
    "PERCENT":        0.40,
    "RATIO":          0.40,
    "SCORE":          0.40,
    "RISK":           0.40,
    "DURATION":       0.40,
    "DISTANCE":       0.40,
    "WEIGHT":         0.40,
    "GENERIC_MEASURE": 0.40,
}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    """Cast v to float, returning None on failure or non-finite values."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _compute_completeness(stats: dict, row_count: int) -> float:
    """Return completeness as (1 - null_count / row_count).

    Returns 1.0 when null_count is absent (old dataset; assume fully complete).
    Returns 0.0 when row_count <= 0.
    """
    if row_count <= 0:
        return 0.0
    null_count = _safe_float(stats.get("null_count"))
    if null_count is None:
        return 1.0
    return max(0.0, 1.0 - (null_count / row_count))


def _histogram_gate(stats: dict) -> tuple[bool, str | None]:
    """Check histogram_bins when the key is present.

    - Key absent  → OK (old dataset; skip check entirely)
    - Key present, ≥1 bin with count > 0 → OK
    - Key present, empty list or all-zero counts → excluded
    """
    if "histogram_bins" not in stats:
        return True, None
    bins = stats["histogram_bins"] or []
    if not bins:
        return False, "histogram_bins_empty"
    if not any(b.get("count", 0) > 0 for b in bins):
        return False, "histogram_bins_all_zero"
    return True, None


def _eligibility_score(completeness: float, role_confidence: float) -> float:
    return round(completeness * min(role_confidence, 1.0), 3)


def _excluded(item: dict, completeness: float, reason: str) -> dict:
    return {
        **item,
        "completeness":     round(completeness, 3),
        "eligibility_score": 0.0,
        "eligible":         False,
        "reason_excluded":  reason,
    }


def _eligible(item: dict, completeness: float, role_confidence: float) -> dict:
    return {
        **item,
        "completeness":     round(completeness, 3),
        "eligibility_score": _eligibility_score(completeness, role_confidence),
        "eligible":         True,
        "reason_excluded":  None,
    }


# ── Per-column evaluator ───────────────────────────────────────────────────────

def _evaluate_one(
    item: dict,
    numeric_profile: dict,
    categorical_meta: dict,
    row_count: int,
) -> dict:
    """Evaluate eligibility for a single role_profile item. Never raises."""
    try:
        role      = item.get("role", "GENERIC_MEASURE")
        col       = item.get("column", "")
        role_conf = float(item.get("role_confidence", 0.0))

        # ── Always-excluded roles ──────────────────────────────────────────────
        if role in _ALWAYS_EXCLUDED:
            return _excluded(item, 1.0, f"role_{role.lower()}_excluded")

        # ── ATTRIBUTE: descriptive scalar, excluded from primary KPI output ───
        if role == _ATTRIBUTE_ROLE:
            stats = numeric_profile.get(col, {})
            comp  = _compute_completeness(stats, row_count)
            return _excluded(item, comp, "attribute_excluded")

        # ── DIMENSION: evaluated against categorical_meta ─────────────────────
        if role == _DIMENSION_ROLE:
            cat  = categorical_meta.get(col) or {}
            comp = _compute_completeness(cat, row_count)

            raw_unique = cat.get("unique_count")
            if raw_unique is None:
                return _excluded(item, comp, "unique_count_missing")
            try:
                unique_count = int(raw_unique)
            except (TypeError, ValueError):
                return _excluded(item, comp, "unique_count_invalid")
            if unique_count < 2:
                return _excluded(item, comp, "unique_count_too_low")
            if unique_count > 500:
                return _excluded(item, comp, "unique_count_too_high")

            return _eligible(item, comp, role_conf)

        # ── Numeric roles (MONEY, COUNT, PERCENT, …) ──────────────────────────
        stats    = numeric_profile.get(col, {})
        comp     = _compute_completeness(stats, row_count)
        min_comp = _MIN_COMPLETENESS.get(role, 0.40)

        if comp < min_comp:
            return _excluded(item, comp, f"completeness_below_{int(min_comp * 100)}pct")

        hist_ok, hist_reason = _histogram_gate(stats)
        if not hist_ok:
            return _excluded(item, comp, hist_reason)

        if role in _MONEY_COUNT_ROLES:
            has_stat = (
                _safe_float(stats.get("sum"))  is not None or
                _safe_float(stats.get("mean")) is not None
            )
        else:
            has_stat = _safe_float(stats.get("mean")) is not None

        if not has_stat:
            return _excluded(item, comp, "required_stat_missing")

        return _eligible(item, comp, role_conf)

    except Exception:
        return {
            **item,
            "completeness":     0.0,
            "eligibility_score": 0.0,
            "eligible":         False,
            "reason_excluded":  "evaluation_error",
        }


# ── Public interface ───────────────────────────────────────────────────────────

def evaluate_kpi_eligibility(
    role_profile: list[dict],
    numeric_profile: dict,
    categorical_meta: dict,
    row_count: int,
) -> list[dict]:
    """Evaluate KPI eligibility for every column in role_profile.

    Preserves original list order. Never raises. Returns [] on bad input.
    """
    if not isinstance(role_profile, list):
        return []
    numeric_profile  = numeric_profile  or {}
    categorical_meta = categorical_meta or {}
    return [
        _evaluate_one(item, numeric_profile, categorical_meta, row_count)
        for item in role_profile
    ]
