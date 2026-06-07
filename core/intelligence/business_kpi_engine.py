"""
core/intelligence/business_kpi_engine.py

Business KPI Engine for ToolSmithAI.

Generates business-aware KPI cards from semantic column classifications
and stored dataset profiles. All computations are deterministic and
derive exclusively from profile data computed at upload time.

No ML, no AI calls, no raw data access required.
Every public function is wrapped in try/except and returns None on failure.

Generated KPI types:
  Financial   : total_revenue, avg_revenue, total_cost, total_profit,
                gross_margin_pct, avg_price, revenue_trend
  Quantity    : total_units, avg_order_size
  Customer    : customer_count
  Dimension   : top_region, top_product, top_category, top_brand
  Operational : avg_score, avg_risk_score, avg_percentage
  Status      : dominant_status

Usage:
  from core.intelligence.business_kpi_engine import build_business_kpi_section

  section = build_business_kpi_section(
      semantic_profile    = semantic_profile,    # from semantic_classifier
      numeric_profile     = numeric_profile,     # from upload profiling
      categorical_profile = categorical_profile, # from upload profiling
      categorical_meta    = categorical_meta,     # from upload profiling
      date_profile        = date_profile,         # from upload profiling
      row_count           = row_count,
  )
  # Returns a dict with type="business_kpis", or None when no data available
"""

import math
from typing import Optional

from core.output.kpi_formatter import format_kpi_value
from core.intelligence.intent_ranker import rank_kpis
from core.intelligence.metric_role_classifier import classify_metric_roles
from core.intelligence.kpi_eligibility_engine import evaluate_kpi_eligibility
from core.intelligence.metric_registry import get_metric_definition
from core.intelligence.semantic_classifier import (
    get_revenue_columns,
    get_cost_columns,
    get_profit_columns,
    get_metric_columns,
    get_dimension_columns,
    get_date_columns,
    get_segmentation_candidates,
    get_columns_by_type,
    summarise_semantic_profile,
)



def _safe_stat(profile: dict, col: str, stat: str) -> Optional[float]:
    """Safely extract a numeric statistic from the profile."""
    try:
        v = profile.get(col, {}).get(stat)
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _completeness_ok(profile: dict, col: str, row_count: int, threshold: float = 0.5) -> bool:
    """Return True when column has enough non-null values to be meaningful."""
    if row_count <= 0:
        return False
    null_count = _safe_stat(profile, col, "null_count")
    if null_count is None:
        return True  # unknown → assume OK
    completeness = 1 - (null_count / row_count)
    return completeness >= threshold


def _trend_direction(pct_change: Optional[float]) -> tuple[str, str]:
    """Return (trend, delta_direction) from a pct_change value."""
    if pct_change is None:
        return "neutral", "neutral"
    if pct_change > 5:
        return "up", "up"
    if pct_change < -5:
        return "down", "down"
    return "neutral", "neutral"


def _status_from_trend(trend: str, positive_good: bool = True) -> str:
    """Map a trend direction to a status badge."""
    if trend == "up":
        return "good" if positive_good else "risk"
    if trend == "down":
        return "risk" if positive_good else "good"
    return "warning"


# ── KPI card builders ──────────────────────────────────────────────────────────

def _kpi_card(
    label: str,
    value: float,
    fmt: str,
    description: str,
    explanation: str,
    priority: str,
    semantic_source: str,
    trend: str = "neutral",
    delta: Optional[float] = None,
    delta_direction: str = "neutral",
    status: str = "good",
    confidence: float = 1.0,
) -> dict:
    """Build a single business KPI card compatible with the existing KPI card schema."""
    return {
        "label":            label,
        "value":            round(value, 4) if isinstance(value, float) else value,
        "format":           fmt,
        "trend":            trend,
        "description":      description,
        "delta":            delta,
        "delta_direction":  delta_direction,
        "status":           status,
        "explanation":      explanation,
        "priority":         priority,
        "semantic_source":  semantic_source,
        "confidence":       round(confidence, 2),
        "value_formatted":  format_kpi_value(value, fmt),
    }


# ── Metric computers ───────────────────────────────────────────────────────────

def compute_revenue_metrics(
    semantic_profile: list[dict],
    numeric_profile: dict,
    date_profile: dict,
    row_count: int,
) -> list[dict]:
    """Compute revenue-related KPI cards."""
    cards: list[dict] = []
    try:
        rev_cols = get_revenue_columns(semantic_profile, min_confidence=0.50)
        if not rev_cols:
            return []

        # Use the highest-confidence revenue column
        col = rev_cols[0]
        conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)

        if not _completeness_ok(numeric_profile, col, row_count):
            return []

        total = _safe_stat(numeric_profile, col, "sum")
        avg   = _safe_stat(numeric_profile, col, "mean")

        # Revenue trend from date_profile
        trend_pct: Optional[float] = None
        for ti in (date_profile or {}).get("trend_insights", []):
            if ti.get("column") == col:
                trend_pct = ti.get("pct_change")
                break

        trend, delta_dir = _trend_direction(trend_pct)

        if total is not None:
            sign = "" if total >= 0 else "-"
            cards.append(_kpi_card(
                label          = "Total Revenue",
                value          = total,
                fmt            = "currency",
                description    = f"Sum of {col}",
                explanation    = (
                    f"Derived from {row_count:,} records in column '{col}'. "
                    + (f"Trend: {trend_pct:+.1f}% period-over-period." if trend_pct is not None else "")
                ),
                priority       = "executive",
                semantic_source = "revenue",
                trend          = trend,
                delta          = round(trend_pct, 1) if trend_pct is not None else None,
                delta_direction = delta_dir,
                status         = _status_from_trend(trend, positive_good=True),
                confidence     = conf,
            ))

        if avg is not None:
            cards.append(_kpi_card(
                label          = "Avg Revenue / Record",
                value          = avg,
                fmt            = "currency",
                description    = f"Mean of {col}",
                explanation    = f"Average revenue per row in '{col}'.",
                priority       = "executive",
                semantic_source = "revenue",
                confidence     = conf * 0.9,
            ))

        # Revenue trend as its own card when significant
        if trend_pct is not None and abs(trend_pct) >= 5:
            cards.append(_kpi_card(
                label          = "Revenue Trend",
                value          = abs(trend_pct),
                fmt            = "percent",
                description    = f"Period-over-period change in {col}",
                explanation    = (
                    f"{col} shows a {trend} trend of {trend_pct:+.1f}% "
                    "from the first half to the second half of the time range."
                ),
                priority       = "executive",
                semantic_source = "revenue",
                trend          = trend,
                delta          = round(trend_pct, 1),
                delta_direction = delta_dir,
                status         = _status_from_trend(trend, positive_good=True),
                confidence     = conf * 0.85,
            ))
    except Exception:
        pass
    return cards


def compute_cost_metrics(
    semantic_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Compute cost/expense-related KPI cards."""
    cards: list[dict] = []
    try:
        cost_cols = get_cost_columns(semantic_profile, min_confidence=0.50)
        if not cost_cols:
            return []
        col = cost_cols[0]
        conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
        if not _completeness_ok(numeric_profile, col, row_count):
            return []
        total = _safe_stat(numeric_profile, col, "sum")
        if total is not None:
            cards.append(_kpi_card(
                label          = "Total Cost",
                value          = total,
                fmt            = "currency",
                description    = f"Sum of {col}",
                explanation    = f"Total cost/expense across {row_count:,} records in '{col}'.",
                priority       = "executive",
                semantic_source = "cost",
                confidence     = conf,
            ))
    except Exception:
        pass
    return cards


def compute_profit_metrics(
    semantic_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Compute profit and margin KPI cards.

    If an explicit profit column exists, use it.
    If margin/percentage-like column exists, show avg margin %.
    Else attempt revenue − cost derivation.
    """
    cards: list[dict] = []
    try:
        # 1. Explicit profit column
        profit_cols = get_profit_columns(semantic_profile, min_confidence=0.50)
        if profit_cols:
            col  = profit_cols[0]
            conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
            is_pct = next(
                (s.get("percentage_like", False) for s in semantic_profile if s["column"] == col),
                False
            )
            if _completeness_ok(numeric_profile, col, row_count):
                val  = _safe_stat(numeric_profile, col, "sum" if not is_pct else "mean")
                if val is not None:
                    fmt = "percent" if is_pct else "currency"
                    label = "Avg Margin %" if is_pct else "Total Profit"
                    if is_pct and 0 < val <= 1:
                        val = val * 100  # convert 0-1 to 0-100
                    cards.append(_kpi_card(
                        label          = label,
                        value          = val,
                        fmt            = fmt,
                        description    = f"{'Mean' if is_pct else 'Sum'} of {col}",
                        explanation    = f"Derived from '{col}' across {row_count:,} records.",
                        priority       = "executive",
                        semantic_source = "profit",
                        status         = "good" if val > 0 else "risk",
                        confidence     = conf,
                    ))

        # 2. Fallback: revenue - cost derivation
        if not cards:
            rev_cols  = get_revenue_columns(semantic_profile, min_confidence=0.60)
            cost_cols = get_cost_columns(semantic_profile, min_confidence=0.60)
            if rev_cols and cost_cols:
                r_sum = _safe_stat(numeric_profile, rev_cols[0], "sum")
                c_sum = _safe_stat(numeric_profile, cost_cols[0], "sum")
                if r_sum is not None and c_sum is not None and r_sum != 0:
                    derived_profit  = r_sum - c_sum
                    derived_margin  = (derived_profit / r_sum) * 100
                    conf = 0.65  # lower confidence — derived, not direct
                    cards.append(_kpi_card(
                        label          = "Derived Gross Profit",
                        value          = derived_profit,
                        fmt            = "currency",
                        description    = f"{rev_cols[0]} − {cost_cols[0]}",
                        explanation    = (
                            f"Estimated from {rev_cols[0]} (sum: {format_kpi_value(r_sum, 'currency')}) "
                            f"minus {cost_cols[0]} (sum: {format_kpi_value(c_sum, 'currency')})."
                        ),
                        priority       = "executive",
                        semantic_source = "profit",
                        status         = "good" if derived_profit > 0 else "risk",
                        confidence     = conf,
                    ))
                    cards.append(_kpi_card(
                        label          = "Derived Gross Margin",
                        value          = derived_margin,
                        fmt            = "percent",
                        description    = "Gross profit as % of revenue",
                        explanation    = f"Margin = (revenue − cost) / revenue × 100.",
                        priority       = "executive",
                        semantic_source = "profit",
                        status         = "good" if derived_margin > 20 else ("warning" if derived_margin > 0 else "risk"),
                        confidence     = conf,
                    ))
    except Exception:
        pass
    return cards


def compute_quantity_metrics(
    semantic_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Compute quantity/units KPI cards."""
    cards: list[dict] = []
    try:
        qty_cols = get_columns_by_type(semantic_profile, "quantity", min_confidence=0.50)
        if not qty_cols:
            return []
        col  = qty_cols[0]
        conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
        if not _completeness_ok(numeric_profile, col, row_count):
            return []
        total = _safe_stat(numeric_profile, col, "sum")
        avg   = _safe_stat(numeric_profile, col, "mean")
        if total is not None:
            cards.append(_kpi_card(
                label          = "Total Units",
                value          = total,
                fmt            = "number",
                description    = f"Sum of {col}",
                explanation    = f"Total quantity across {row_count:,} records in '{col}'.",
                priority       = "operational",
                semantic_source = "quantity",
                confidence     = conf,
            ))
        if avg is not None:
            cards.append(_kpi_card(
                label          = "Avg Units / Record",
                value          = avg,
                fmt            = "decimal",
                description    = f"Mean of {col}",
                explanation    = f"Average quantity per row in '{col}'.",
                priority       = "operational",
                semantic_source = "quantity",
                confidence     = conf * 0.9,
            ))
    except Exception:
        pass
    return cards


def compute_price_metrics(
    semantic_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Compute average price KPI card."""
    cards: list[dict] = []
    try:
        price_cols = get_columns_by_type(semantic_profile, "price", min_confidence=0.55)
        if not price_cols:
            return []
        col  = price_cols[0]
        conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
        if not _completeness_ok(numeric_profile, col, row_count):
            return []
        avg = _safe_stat(numeric_profile, col, "mean")
        if avg is not None:
            cards.append(_kpi_card(
                label          = "Avg Price",
                value          = avg,
                fmt            = "currency",
                description    = f"Mean of {col}",
                explanation    = f"Average price across {row_count:,} records in '{col}'.",
                priority       = "operational",
                semantic_source = "price",
                confidence     = conf,
            ))
    except Exception:
        pass
    return cards


def compute_customer_metrics(
    semantic_profile: list[dict],
    categorical_meta: dict,
) -> list[dict]:
    """Compute customer count KPI from categorical cardinality."""
    cards: list[dict] = []
    try:
        cust_cols = get_columns_by_type(semantic_profile, "customer", min_confidence=0.50)
        # Also look for high-cardinality customer-like columns
        if not cust_cols:
            return []
        col  = cust_cols[0]
        conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
        meta = categorical_meta.get(col)
        if meta is None:
            return []
        unique_count = meta.get("unique_count")
        if unique_count and isinstance(unique_count, int) and unique_count > 0:
            cards.append(_kpi_card(
                label          = "Unique Customers",
                value          = unique_count,
                fmt            = "number",
                description    = f"Distinct values in {col}",
                explanation    = f"Number of unique {col} values across the dataset.",
                priority       = "executive",
                semantic_source = "customer",
                confidence     = conf,
            ))
    except Exception:
        pass
    return cards


def compute_segmentation_metrics(
    semantic_profile: list[dict],
    categorical_profile: dict,
    categorical_meta: dict,
    row_count: int,
) -> list[dict]:
    """Compute top dimension KPI cards (top region, top product, top category, etc.)."""
    cards: list[dict] = []
    try:
        seg_cols = get_segmentation_candidates(semantic_profile, min_confidence=0.45)
        # Get at most 3 segmentation dimension cards
        for col in seg_cols[:3]:
            try:
                top_vals = categorical_profile.get(col, [])
                if not top_vals:
                    continue
                top_entry = top_vals[0]
                top_value = top_entry.get("value", "")
                top_count = top_entry.get("count", 0)
                if not top_value or not top_count:
                    continue
                pct = round(top_count / row_count * 100, 1) if row_count > 0 else 0
                sem_type = next(
                    (s["semantic_type"] for s in semantic_profile if s["column"] == col),
                    "category"
                )
                conf = next(
                    (s["confidence"] for s in semantic_profile if s["column"] == col),
                    0.6
                )
                label_map = {
                    "region":   "Top Region",
                    "country":  "Top Country",
                    "state":    "Top State",
                    "city":     "Top City",
                    "product":  "Top Product",
                    "category": "Top Category",
                    "customer": "Top Customer",
                    "employee": "Top Employee",
                }
                label = label_map.get(sem_type, f"Top {col}")
                cards.append({
                    "label":            label,
                    "value":            pct,
                    "format":           "percent",
                    "value_display":    top_value,
                    "value_formatted":  f"{top_value} ({pct}%)",
                    "trend":            "neutral",
                    "description":      f"Most frequent value in {col}",
                    "delta":            None,
                    "delta_direction":  "neutral",
                    "status":           "good",
                    "explanation":      (
                        f"'{top_value}' accounts for {pct}% of all {col} records "
                        f"({top_count:,} of {row_count:,} rows)."
                    ),
                    "priority":         "operational",
                    "semantic_source":  sem_type,
                    "confidence":       round(conf, 2),
                })
            except Exception:
                continue
    except Exception:
        pass
    return cards


def compute_operational_metrics(
    semantic_profile: list[dict],
    numeric_profile: dict,
    row_count: int,
) -> list[dict]:
    """Compute score, percentage, and risk KPI cards."""
    cards: list[dict] = []
    try:
        # Score columns → avg score
        score_cols = get_columns_by_type(semantic_profile, "score", min_confidence=0.55)
        for col in score_cols[:2]:
            try:
                conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
                if not _completeness_ok(numeric_profile, col, row_count, threshold=0.4):
                    continue
                avg = _safe_stat(numeric_profile, col, "mean")
                mn  = _safe_stat(numeric_profile, col, "min")
                mx  = _safe_stat(numeric_profile, col, "max")
                if avg is None:
                    continue
                # Determine if it's a 0-100 or 0-10 or 0-1 scale
                if mx and mx <= 1.01:
                    display_val = avg * 100
                    fmt = "percent"
                elif mx and mx <= 10.1:
                    display_val = avg
                    fmt = "decimal"
                else:
                    display_val = avg
                    fmt = "number"

                range_str = f"{format_kpi_value(mn, 'decimal')}–{format_kpi_value(mx, 'decimal')}" if mn is not None and mx is not None else ""
                cards.append(_kpi_card(
                    label          = f"Avg {col}",
                    value          = display_val,
                    fmt            = fmt,
                    description    = f"Mean of {col}",
                    explanation    = f"Average score in '{col}'." + (f" Range: {range_str}." if range_str else ""),
                    priority       = "operational",
                    semantic_source = "score",
                    status         = "good" if display_val >= (70 if fmt == "percent" else 0) else "warning",
                    confidence     = conf,
                ))
            except Exception:
                continue

        # Percentage columns → avg percentage
        pct_cols = get_columns_by_type(semantic_profile, "percentage", min_confidence=0.55)
        for col in pct_cols[:1]:
            try:
                conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
                if not _completeness_ok(numeric_profile, col, row_count, threshold=0.4):
                    continue
                avg = _safe_stat(numeric_profile, col, "mean")
                if avg is None:
                    continue
                # Convert 0-1 range to 0-100
                if 0 <= avg <= 1:
                    avg = avg * 100
                cards.append(_kpi_card(
                    label          = f"Avg {col}",
                    value          = avg,
                    fmt            = "percent",
                    description    = f"Mean of {col}",
                    explanation    = f"Average {col} across {row_count:,} records.",
                    priority       = "operational",
                    semantic_source = "percentage",
                    confidence     = conf,
                ))
            except Exception:
                continue

        # Risk columns → avg risk score
        risk_cols = get_columns_by_type(semantic_profile, "risk", min_confidence=0.55)
        for col in risk_cols[:1]:
            try:
                conf = next((s["confidence"] for s in semantic_profile if s["column"] == col), 0.7)
                if not _completeness_ok(numeric_profile, col, row_count, threshold=0.4):
                    continue
                avg = _safe_stat(numeric_profile, col, "mean")
                if avg is None:
                    continue
                mx = _safe_stat(numeric_profile, col, "max")
                if mx and mx <= 1.01:
                    avg = avg * 100
                cards.append(_kpi_card(
                    label          = f"Avg Risk: {col}",
                    value          = avg,
                    fmt            = "decimal",
                    description    = f"Mean of {col}",
                    explanation    = f"Average risk score in '{col}' across {row_count:,} records.",
                    priority       = "risk",
                    semantic_source = "risk",
                    status         = "risk" if avg > 70 else ("warning" if avg > 40 else "good"),
                    confidence     = conf,
                ))
            except Exception:
                continue

    except Exception:
        pass
    return cards


# ── Dataset type detection ─────────────────────────────────────────────────────

_DATASET_TYPE_LABELS = {
    "sales_financial":     "Sales / Financial",
    "financial_ops":       "Financial Operations",
    "risk_operational":    "Risk & Operations",
    "customer_analytics":  "Customer Analytics",
    "time_series":         "Time Series",
    "operational":         "Operational",
    "general":             "General",
}


def detect_dataset_type(summary: dict) -> str:
    """Infer the dataset's business domain from the semantic summary."""
    has_financial = summary.get("has_financial_data", False)
    has_dims      = summary.get("has_dimensions", False)
    has_temporal  = summary.get("has_temporal_data", False)
    has_ops       = summary.get("has_operational_kpis", False)
    types         = summary.get("types", {})

    has_risk     = types.get("risk", 0) > 0
    has_customer = types.get("customer", 0) > 0
    has_revenue  = types.get("revenue", 0) > 0

    if has_financial and has_revenue and has_dims:
        return "sales_financial"
    if has_financial and has_ops:
        return "financial_ops"
    if has_risk:
        return "risk_operational"
    if has_customer and has_financial:
        return "customer_analytics"
    if has_temporal and (has_financial or has_ops):
        return "time_series"
    if has_ops:
        return "operational"
    return "general"


# ── Generic Metric Discovery — Phase 1 ───────────────────────────────────────
# Columns whose semantic_type is already handled by a domain-specific compute
# function are skipped so generic KPIs never duplicate domain-specific ones.
# Classification determines confidence, not eligibility.
_GENERIC_SKIP_TYPES: frozenset = frozenset({
    "revenue", "cost", "profit",
    "quantity", "price",
    "score", "percentage", "risk",
})


def _clean_col_display(col: str) -> str:
    """Convert snake_case / camelCase / kebab-case column name to Title Case.

    Examples:
      load_weight_kg  → Load Weight Kg
      transitDays     → Transit Days
      delay-hours     → Delay Hours
    """
    result = []
    for i, ch in enumerate(col):
        if i > 0 and ch.isupper() and col[i - 1].islower():
            result.append(" ")
        result.append(ch)
    return (
        "".join(result)
        .replace("_", " ")
        .replace("-", " ")
        .replace(".", " ")
        .strip()
        .title()
    )


def compute_generic_measure_kpis(
    numeric_profile: dict,
    semantic_profile: list[dict],
    row_count: int,
    already_claimed_cols: set[str] | None = None,
) -> list[dict]:
    """Discover KPI candidates from any unclaimed numeric column.

    Uses metric_role_classifier, kpi_eligibility_engine, and metric_registry
    for role-aware eligibility and formatting. Domain-specific columns already
    covered by the 8 compute_*_metrics functions are excluded via
    already_claimed_cols and _GENERIC_SKIP_TYPES.

    Output shape is identical to the previous implementation.
    Never raises. Returns [] on any failure.
    """
    cards: list[dict] = []
    claimed = already_claimed_cols or set()

    # Unclaimed, numeric, non-domain columns — input to the new enterprise layer
    candidates = [
        s for s in (semantic_profile or [])
        if s.get("column") not in claimed
        and s.get("column") in numeric_profile
        and s.get("semantic_type") not in _GENERIC_SKIP_TYPES
    ]
    if not candidates:
        return []

    # Role classification + eligibility via new enterprise layer.
    # categorical_meta is empty: all candidates are confirmed numeric columns.
    role_profile = classify_metric_roles(candidates)
    evaluated    = evaluate_kpi_eligibility(role_profile, numeric_profile, {}, row_count)

    for item in evaluated:
        if not item.get("eligible"):
            continue
        try:
            col  = item["column"]
            role = item.get("role", "GENERIC_MEASURE")

            # Look up display rules; fall back to GENERIC_MEASURE when role has
            # no registry entry (e.g. DIMENSION excluded via eligibility).
            defn = get_metric_definition(role) or get_metric_definition("GENERIC_MEASURE")

            agg      = defn["aggregation"]
            fmt_type = defn["format_type"]

            if agg == "dominant":
                continue

            # Resolve display value using the role's primary aggregation
            if agg == "sum":
                display_val = _safe_stat(numeric_profile, col, "sum")
                if display_val is None:
                    display_val = _safe_stat(numeric_profile, col, "mean")
            else:
                display_val = _safe_stat(numeric_profile, col, "mean")

            if display_val is None:
                continue

            # Scale 0–1 percentage values to 0–100 display range
            if role == "PERCENT" and item.get("percentage_like") and 0.0 <= display_val <= 1.0:
                display_val = display_val * 100

            # Scale 0–1 ratio values to 0–100 display range
            if role == "RATIO" and 0.0 <= display_val <= 1.0:
                display_val = display_val * 100

            clean_label = _clean_col_display(col)
            label       = defn["label_template"].replace("{col}", clean_label)
            explanation = (
                f"Total {clean_label.lower()} across {row_count:,} records."
                if agg == "sum"
                else f"Average {clean_label.lower()} across {row_count:,} records."
            )

            cards.append(_kpi_card(
                label           = label,
                value           = display_val,
                fmt             = fmt_type,
                description     = f"{agg.title()} of {col}",
                explanation     = explanation,
                priority        = defn["priority"],
                semantic_source = "measure",
                confidence      = 0.45,
            ))
        except Exception:
            continue

    return cards


# ── Main orchestrator ──────────────────────────────────────────────────────────

def build_business_kpi_section(
    semantic_profile: list[dict],
    numeric_profile: dict,
    categorical_profile: dict,
    categorical_meta: dict,
    date_profile: dict,
    row_count: int,
    max_kpis: int = 8,
    intent_text: str | None = None,
) -> Optional[dict]:
    """Build a business_kpis section dict for insertion into the report.

    Returns None when:
    - semantic_profile is empty or all columns are "unknown"
    - no numeric/categorical data is available
    - no business KPIs could be computed

    Never raises.
    """
    try:
        if not semantic_profile or row_count <= 0:
            return None

        summary = summarise_semantic_profile(semantic_profile)
        if summary.get("classified", 0) == 0 and not numeric_profile:
            return None

        dataset_type = detect_dataset_type(summary)

        # Compute all KPI classes
        all_cards: list[dict] = []
        all_cards += compute_revenue_metrics(semantic_profile, numeric_profile, date_profile, row_count)
        all_cards += compute_cost_metrics(semantic_profile, numeric_profile, row_count)
        all_cards += compute_profit_metrics(semantic_profile, numeric_profile, row_count)
        all_cards += compute_quantity_metrics(semantic_profile, numeric_profile, row_count)
        all_cards += compute_price_metrics(semantic_profile, numeric_profile, row_count)
        all_cards += compute_customer_metrics(semantic_profile, categorical_meta)
        all_cards += compute_segmentation_metrics(semantic_profile, categorical_profile, categorical_meta, row_count)
        all_cards += compute_operational_metrics(semantic_profile, numeric_profile, row_count)

        # Phase 1 — Generic Metric Discovery:
        # Columns claimed by the 8 domain functions above (identified by semantic_type)
        # are passed as already_claimed_cols so generic discovery never duplicates them.
        _claimed: set[str] = {
            s["column"] for s in semantic_profile
            if s.get("semantic_type") in _GENERIC_SKIP_TYPES
            and s.get("confidence", 0) >= 0.45
            and s.get("column") in numeric_profile
        }
        all_cards += compute_generic_measure_kpis(
            numeric_profile, semantic_profile, row_count,
            already_claimed_cols=_claimed,
        )

        if not all_cards:
            return None

        ranked = rank_kpis(all_cards, dataset_type, max_kpis=max_kpis, intent_text=intent_text)
        if not ranked:
            return None

        # Build items list for email body rendering
        items: list[str] = []
        for card in ranked:
            formatted = card.get("value_formatted") or card.get("value_display") or str(card.get("value", ""))
            items.append(f"{card['label']}: {formatted}")

        dataset_label = _DATASET_TYPE_LABELS.get(dataset_type, "General")

        return {
            "type":         "business_kpis",
            "heading":      "Business Intelligence KPIs",
            "kpis":         ranked,
            "dataset_type": dataset_type,
            "dataset_label": dataset_label,
            "semantic_summary": {
                "classified":       summary.get("classified", 0),
                "total_columns":    summary.get("total_columns", 0),
                "has_financial":    summary.get("has_financial_data", False),
                "has_dimensions":   summary.get("has_dimensions", False),
                "has_temporal":     summary.get("has_temporal_data", False),
            },
            "items": items,
        }

    except Exception:
        return None
