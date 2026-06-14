"""
core/intelligence/semantic_classifier.py

Deterministic semantic column classifier for ToolSmithAI.

Classifies each dataset column into a business-meaningful semantic type
(revenue, cost, customer, region, date, etc.) using:
  - Column name tokens (camelCase, snake_case, kebab-case aware)
  - pandas dtype (numeric vs categorical)
  - Value distributions (null rate, cardinality, range, outliers)
  - Date profile presence

No ML libraries are used. All logic is deterministic and rule-based.
Results are additive — existing profile fields are never modified.

Semantic types (25 total):
  Financial metrics  : revenue, cost, profit, amount, price
  Operational metrics: quantity, percentage, ratio, score
  Dimensions         : product, category, customer, employee,
                       region, country, state, city
  Temporal           : date, timestamp
  Identifier         : id
  Status / risk      : status, risk
  Fallback           : unknown

Usage:
  from core.intelligence.semantic_classifier import classify_columns

  semantic_profile = classify_columns(
      columns          = df.columns.tolist(),
      numeric_profile  = numeric_profile,
      categorical_meta = categorical_meta,
      date_profile     = date_profile,
      missing_values   = missing_values,
      row_count        = len(df),
  )
"""

import re
import math
from typing import Optional

# ── Token normalisation ────────────────────────────────────────────────────────

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATOR      = re.compile(r"[\s_\-\.\/\\]+")


def _tokenize(name: str) -> list[str]:
    """Split a column name into lowercase tokens.

    Handles camelCase, snake_case, kebab-case, dot.notation and mixed forms.

    Examples:
      "CustomerID"      → ["customer", "id"]
      "total_sales_usd" → ["total", "sales", "usd"]
      "GrossMargin%"    → ["gross", "margin"]
      "Date Created"    → ["date", "created"]
    """
    # Strip non-alphanumeric suffixes (%, $, #)
    name = re.sub(r"[%$#@!]+$", "", name.strip())
    # Split camelCase
    expanded = _CAMEL_BOUNDARY.sub("_", name)
    # Split on separators
    raw_tokens = _SEPARATOR.split(expanded)
    return [t.lower() for t in raw_tokens if t and t.isalpha()]


# ── Keyword dictionaries ───────────────────────────────────────────────────────
# Each dict maps a lowercase token to a base confidence score (0.50 – 0.90).
# High score = strong signal for that type.
# A token may appear in at most one dict to avoid ambiguity.

_REVENUE: dict[str, float] = {
    "revenue": 0.88, "rev": 0.72,
    "sales":   0.84, "sale": 0.68,
    "income":  0.76, "earnings": 0.76,
    "turnover": 0.72, "receipts": 0.66,
    "proceeds": 0.62, "inflow": 0.57,
    "billing":  0.62, "charges": 0.72,
}

_COST: dict[str, float] = {
    "cost":        0.84, "costs":       0.82,
    "expense":     0.82, "expenses":    0.80,
    "expenditure": 0.76, "cogs":        0.90,
    "overhead":    0.66, "spend":       0.72,
    "spending":    0.68, "outflow":     0.58,
    "budget":      0.62, "outgoing":    0.58,
}

_PROFIT: dict[str, float] = {
    "profit":       0.88, "margin":     0.82,
    "ebitda":       0.92, "ebit":       0.88,
    "gain":         0.62, "contribution": 0.58,
    "pnl":          0.85, "noi":        0.80,
    "retained":     0.60,
}

_AMOUNT: dict[str, float] = {
    "amount":   0.72, "amt":      0.68,
    "value":    0.62, "total":    0.56,
    "sum":      0.58, "subtotal": 0.66,
    "aggregate": 0.58,
}

_PRICE: dict[str, float] = {
    "price":   0.88, "prices":  0.84,
    "fee":     0.76, "charge":  0.72,
    "tariff":  0.76, "premium": 0.66,
    "fare":    0.72, "msrp":    0.88,
    "retail":  0.66, "list":    0.55,
}

_QUANTITY: dict[str, float] = {
    "quantity":  0.92, "qty":    0.92,
    "units":     0.82, "unit":   0.76,
    "pieces":    0.72, "volume": 0.66,
    "items":     0.66, "orders": 0.62,
    "shipments": 0.62, "children": 0.70,
}

_PERCENTAGE: dict[str, float] = {
    "percent":    0.92, "pct":        0.92,
    "percentage": 0.92, "ratio":      0.78,
    "share":      0.66, "proportion": 0.76,
    "fraction":   0.72, "rate":       0.66,
    "growth":     0.58, "churn":      0.68,
    "conversion": 0.68, "penetration": 0.62,
    "utilization": 0.64, "occupancy":  0.62,
}

_SCORE: dict[str, float] = {
    "score":   0.88, "rating": 0.82,
    "rank":    0.78, "index":  0.66,
    "grade":   0.72, "level":  0.58,
    "tier":    0.62, "nps":    0.88,
    "csat":    0.88, "kpi":    0.70,
    "metric":  0.58,
}

_PRODUCT: dict[str, float] = {
    "product":     0.88, "item":        0.72,
    "sku":         0.92, "part":        0.68,
    "model":       0.66, "variant":     0.72,
    "goods":       0.66, "article":     0.62,
    "merchandise": 0.68, "offering":    0.60,
    "service":     0.58,
}

_CATEGORY: dict[str, float] = {
    "category":   0.92, "cat":        0.76,
    "type":       0.72, "class":      0.66,
    "segment":    0.82, "group":      0.72,
    "sector":     0.72, "family":     0.62,
    "brand":      0.76, "line":       0.56,
    "vertical":   0.66, "department": 0.72,
    "division":   0.68,
}

_CUSTOMER: dict[str, float] = {
    "customer":   0.92, "client":     0.88,
    "account":    0.72, "contact":    0.68,
    "user":       0.72, "member":     0.68,
    "buyer":      0.78, "purchaser":  0.72,
    "consumer":   0.76, "subscriber": 0.72,
    "prospect":   0.68, "lead":       0.66,
}

_EMPLOYEE: dict[str, float] = {
    "employee":       0.92, "staff":          0.82,
    "agent":          0.72, "rep":            0.68,
    "representative": 0.82, "worker":         0.76,
    "associate":      0.68, "manager":        0.66,
    "salesperson":    0.82, "owner":          0.58,
    "vendor":         0.66, "supplier":       0.68,
}

_REGION: dict[str, float] = {
    "region":    0.92, "area":      0.72,
    "territory": 0.88, "zone":      0.82,
    "district":  0.82, "geo":       0.72,
    "geography": 0.80,
}

_COUNTRY: dict[str, float] = {
    "country":      0.96, "nation":    0.88,
    "iso":          0.68, "continent": 0.82,
    "nationality":  0.76,
}

_STATE: dict[str, float] = {
    "state":      0.80, "province":  0.88,
    "prefecture": 0.88, "county":    0.82,
}

_CITY: dict[str, float] = {
    "city":         0.92, "town":        0.82,
    "municipality": 0.88, "location":    0.66,
    "metro":        0.72, "market":      0.62,
    "site":         0.58,
}

_DATE: dict[str, float] = {
    "date":    0.92, "period":  0.76,
    "month":   0.82, "week":    0.78,
    "year":    0.82, "quarter": 0.82,
    "day":     0.72, "dt":      0.72,
    "when":    0.62, "fiscal":  0.66,
    "calendar": 0.66,
}

_TIMESTAMP: dict[str, float] = {
    "timestamp": 0.96, "datetime":  0.96,
    "created":   0.76, "updated":   0.76,
    "modified":  0.76, "logged":    0.72,
    "recorded":  0.68, "processed": 0.62,
}

_ID: dict[str, float] = {
    "id":         0.88, "key":        0.72,
    "code":       0.66, "identifier": 0.92,
    "uuid":       0.96, "guid":       0.96,
    "ref":        0.66, "reference":  0.72,
    "pk":         0.88, "fk":         0.80,
    "number":     0.62, "num":        0.62,
}

_STATUS: dict[str, float] = {
    "status":    0.92, "flag":      0.78,
    "indicator": 0.72, "active":    0.68,
    "enabled":   0.68, "mode":      0.62,
    "stage":     0.72, "phase":     0.68,
    "condition": 0.66, "result":    0.58,
    "outcome":   0.62,
}

_RISK: dict[str, float] = {
    "risk":          0.92, "priority":    0.82,
    "severity":      0.88, "impact":      0.76,
    "urgency":       0.82, "criticality": 0.88,
    "threat":        0.76, "vulnerability": 0.82,
    "exposure":      0.72, "likelihood":   0.78,
}

# Master registry: semantic_type → keyword dict
_SEMANTIC_TYPES: dict[str, dict[str, float]] = {
    "revenue":    _REVENUE,
    "cost":       _COST,
    "profit":     _PROFIT,
    "amount":     _AMOUNT,
    "price":      _PRICE,
    "quantity":   _QUANTITY,
    "percentage": _PERCENTAGE,
    "score":      _SCORE,
    "product":    _PRODUCT,
    "category":   _CATEGORY,
    "customer":   _CUSTOMER,
    "employee":   _EMPLOYEE,
    "region":     _REGION,
    "country":    _COUNTRY,
    "state":      _STATE,
    "city":       _CITY,
    "date":       _DATE,
    "timestamp":  _TIMESTAMP,
    "id":         _ID,
    "status":     _STATUS,
    "risk":       _RISK,
}

# Semantic type → semantic group
_SEMANTIC_GROUPS: dict[str, str] = {
    "revenue":    "financial_metric",
    "cost":       "financial_metric",
    "profit":     "financial_metric",
    "amount":     "financial_metric",
    "price":      "financial_metric",
    "quantity":   "operational_metric",
    "percentage": "operational_metric",
    "ratio":      "operational_metric",
    "score":      "operational_metric",
    "product":    "dimension",
    "category":   "dimension",
    "customer":   "dimension",
    "employee":   "dimension",
    "region":     "dimension",
    "country":    "dimension",
    "state":      "dimension",
    "city":       "dimension",
    "date":       "temporal",
    "timestamp":  "temporal",
    "id":         "identifier",
    "status":     "status_flag",
    "risk":       "status_flag",
    "unknown":    "unknown",
}

# Types that are naturally numeric
_NUMERIC_TYPES = frozenset({
    "revenue", "cost", "profit", "amount", "price",
    "quantity", "percentage", "score",
})

# Types that are naturally categorical / low-cardinality
_CATEGORICAL_TYPES = frozenset({
    "product", "category", "customer", "employee",
    "region", "country", "state", "city",
    "status", "risk",
})

# Types where high cardinality strengthens the signal
_HIGH_CARDINALITY_TYPES = frozenset({"id", "customer", "product"})

# Minimum confidence to assign a semantic type (below → "unknown")
_CONFIDENCE_THRESHOLD = 0.40


# ── Single-column classification ───────────────────────────────────────────────

def _classify_column(
    column_name: str,
    is_numeric: bool,
    is_date_detected: bool,
    numeric_stats: Optional[dict],
    categorical_stats: Optional[dict],
    row_count: int,
) -> dict:
    """Classify a single column and return its semantic descriptor.

    Never raises. Returns semantic_type="unknown" on any failure.
    """
    try:
        tokens = _tokenize(column_name)
        if not tokens:
            return _unknown_descriptor(column_name, is_numeric, is_date_detected, categorical_stats, row_count)

        # ── Step 1: Score each semantic type by token matches ──────────────────
        type_scores: dict[str, float] = {}
        matched_tokens: dict[str, list[str]] = {}

        for sem_type, keyword_dict in _SEMANTIC_TYPES.items():
            best_token_score = 0.0
            hits: list[str] = []
            for token in tokens:
                tok_conf = keyword_dict.get(token, 0.0)
                if tok_conf > 0:
                    hits.append(token)
                    if tok_conf > best_token_score:
                        best_token_score = tok_conf

            if best_token_score > 0:
                # Multi-token bonus: two matching tokens → +0.06
                multi_bonus = 0.06 if len(hits) >= 2 else 0.0
                type_scores[sem_type] = min(best_token_score + multi_bonus, 0.96)
                matched_tokens[sem_type] = hits

        # ── Step 2: Apply dtype modifier ───────────────────────────────────────
        for sem_type in list(type_scores.keys()):
            if is_numeric and sem_type in _NUMERIC_TYPES:
                type_scores[sem_type] = min(type_scores[sem_type] + 0.08, 0.97)
            elif not is_numeric and sem_type in _CATEGORICAL_TYPES:
                type_scores[sem_type] = min(type_scores[sem_type] + 0.08, 0.97)
            elif is_numeric and sem_type in _CATEGORICAL_TYPES:
                type_scores[sem_type] = max(type_scores[sem_type] - 0.12, 0.10)
            elif not is_numeric and sem_type in _NUMERIC_TYPES:
                type_scores[sem_type] = max(type_scores[sem_type] - 0.12, 0.10)

        # ── Step 3: Date profile boost ─────────────────────────────────────────
        if is_date_detected:
            if "timestamp" in type_scores:
                type_scores["timestamp"] = min(type_scores["timestamp"] + 0.15, 0.98)
            elif "date" in type_scores:
                type_scores["date"] = min(type_scores["date"] + 0.15, 0.98)
            else:
                # Column is in date_profile but name gave no date signal → inject date
                type_scores["date"] = 0.72

        # ── Step 4: Distribution modifiers ────────────────────────────────────
        cardinality_ratio = _compute_cardinality_ratio(categorical_stats, row_count)

        if categorical_stats is not None:
            unique_count = categorical_stats.get("unique_count", 0)

            # ID signal: almost-unique categorical or high-cardinality numeric
            if cardinality_ratio >= 0.90:
                cur = type_scores.get("id", 0.0)
                type_scores["id"] = max(cur, 0.70)

            # Status signal: very few unique values (≤ 10) in categorical
            if not is_numeric and unique_count <= 10:
                cur = type_scores.get("status", 0.0)
                if cur < 0.45:
                    type_scores["status"] = 0.45  # mild suggestion

        if numeric_stats is not None and is_numeric:
            # Percentage signal: values tightly bounded in [0, 1] or [0, 100]
            mn  = numeric_stats.get("min")
            mx  = numeric_stats.get("max")
            avg = numeric_stats.get("mean")

            if mn is not None and mx is not None and avg is not None:
                if _is_percentage_range(mn, mx):
                    has_pct_token = "percentage" in type_scores
                    is_zero_to_one = (mn >= 0 and mx <= 1.01)
                    if has_pct_token or is_zero_to_one:
                        cur = type_scores.get("percentage", 0.0)
                        type_scores["percentage"] = max(cur, 0.60)

                # Currency/revenue signal: all positive, no small fractions
                if mn >= 0 and mx > 1 and avg > 0.5:
                    # Boost financial types if already candidate
                    for ft in ("revenue", "cost", "amount", "price"):
                        if ft in type_scores:
                            type_scores[ft] = min(type_scores[ft] + 0.04, 0.97)

                # Negative values weaken revenue, strengthen cost/pnl
                neg_count = numeric_stats.get("negative_count", 0)
                if neg_count and neg_count > 0:
                    if "revenue" in type_scores:
                        type_scores["revenue"] = max(type_scores["revenue"] - 0.08, 0.10)
                    for ft in ("cost", "profit", "amount"):
                        if ft in type_scores:
                            type_scores[ft] = min(type_scores[ft] + 0.04, 0.97)

        # ── Step 5: Select winning type ────────────────────────────────────────
        if not type_scores:
            return _unknown_descriptor(column_name, is_numeric, is_date_detected, categorical_stats, row_count)

        best_type = max(type_scores, key=lambda t: type_scores[t])
        best_conf = type_scores[best_type]

        if best_conf < _CONFIDENCE_THRESHOLD:
            return _unknown_descriptor(column_name, is_numeric, is_date_detected, categorical_stats, row_count)

        # Round confidence to 2 decimals
        best_conf = round(best_conf, 2)

        # ── Step 6: Build descriptor ───────────────────────────────────────────
        return {
            "column":           column_name,
            "semantic_type":    best_type,
            "semantic_group":   _SEMANTIC_GROUPS.get(best_type, "unknown"),
            "confidence":       best_conf,
            "is_numeric":       is_numeric,
            "is_categorical":   not is_numeric,
            "is_date":          is_date_detected,
            "currency_like":    _is_currency_like(is_numeric, numeric_stats),
            "percentage_like":  _is_percentage_like(is_numeric, numeric_stats),
            "high_cardinality": cardinality_ratio >= 0.50,
            "likely_id":        best_type == "id" or cardinality_ratio >= 0.90,
            "likely_dimension": best_type in _CATEGORICAL_TYPES,
            "likely_metric":    best_type in _NUMERIC_TYPES,
            "cardinality_ratio": round(cardinality_ratio, 3),
            "matched_tokens":   matched_tokens.get(best_type, []),
        }

    except Exception:
        return _unknown_descriptor(column_name, is_numeric, is_date_detected, None, row_count)


# ── Helper predicates ──────────────────────────────────────────────────────────

def _compute_cardinality_ratio(categorical_stats: Optional[dict], row_count: int) -> float:
    """Return unique_count / row_count, clamped to [0, 1]. Returns 0 if unknown."""
    if categorical_stats is None or row_count <= 0:
        return 0.0
    unique = categorical_stats.get("unique_count")
    if unique is None:
        return 0.0
    try:
        return min(float(unique) / float(row_count), 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _is_percentage_range(mn: float, mx: float) -> bool:
    """True when the value range strongly suggests a 0-1 or 0-100 percentage."""
    try:
        if 0 <= mn and mx <= 1.01:
            return True
        if 0 <= mn and mx <= 100.01 and mx > 1:
            # Could be a 0-100 scale; only flag if max ≤ 100
            return mx <= 100.0
        return False
    except (TypeError, ValueError):
        return False


def _is_currency_like(is_numeric: bool, numeric_stats: Optional[dict]) -> bool:
    """True when the column looks like a currency/monetary value."""
    if not is_numeric or numeric_stats is None:
        return False
    try:
        mn  = numeric_stats.get("min")
        mx  = numeric_stats.get("max")
        avg = numeric_stats.get("mean")
        if mn is None or mx is None or avg is None:
            return False
        return mn >= 0 and mx > 1 and avg > 0.5
    except (TypeError, ValueError):
        return False


def _is_percentage_like(is_numeric: bool, numeric_stats: Optional[dict]) -> bool:
    """True when the column looks like a percentage or ratio."""
    if not is_numeric or numeric_stats is None:
        return False
    try:
        mn = numeric_stats.get("min")
        mx = numeric_stats.get("max")
        if mn is None or mx is None:
            return False
        return _is_percentage_range(float(mn), float(mx))
    except (TypeError, ValueError):
        return False


def _unknown_descriptor(
    column_name: str,
    is_numeric: bool,
    is_date: bool,
    categorical_stats: Optional[dict],
    row_count: int,
) -> dict:
    """Return a minimal descriptor for columns that could not be classified."""
    cardinality_ratio = _compute_cardinality_ratio(categorical_stats, row_count)
    return {
        "column":           column_name,
        "semantic_type":    "unknown",
        "semantic_group":   "unknown",
        "confidence":       0.0,
        "is_numeric":       is_numeric,
        "is_categorical":   not is_numeric,
        "is_date":          is_date,
        "currency_like":    False,
        "percentage_like":  False,
        "high_cardinality": cardinality_ratio >= 0.50,
        "likely_id":        cardinality_ratio >= 0.90,
        "likely_dimension": False,
        "likely_metric":    False,
        "cardinality_ratio": round(cardinality_ratio, 3),
        "matched_tokens":   [],
    }


# ── Public entry point ─────────────────────────────────────────────────────────

def classify_columns(
    columns: list[str],
    numeric_profile: dict,
    categorical_meta: dict,
    date_profile: dict,
    missing_values: dict,
    row_count: int,
) -> list[dict]:
    """Classify all columns in a dataset into semantic types.

    Args:
        columns:          Raw column names from the uploaded file.
        numeric_profile:  Dict of col → stats (min/max/mean/std/etc.) from upload.
        categorical_meta: Dict of col → meta (unique_count/etc.) from upload.
        date_profile:     Dict with "date_columns" list from upload.
        missing_values:   Dict of col → null_count from upload.
        row_count:        Total rows in the dataset.

    Returns:
        List of semantic column descriptors — one per column, in original order.
        Never raises. Columns that fail classification get semantic_type="unknown".
    """
    if not columns or row_count <= 0:
        return []

    # Build a set of column names confirmed as date-type by the date profiler
    date_col_names: set[str] = set()
    try:
        for dc in (date_profile or {}).get("date_columns", []):
            name = dc.get("column")
            if name:
                date_col_names.add(name)
    except Exception:
        pass

    result: list[dict] = []

    for col in columns:
        try:
            is_numeric       = col in numeric_profile
            is_date_detected = col in date_col_names
            numeric_stats    = numeric_profile.get(col) if is_numeric else None
            categorical_stats = categorical_meta.get(col) if not is_numeric else None

            descriptor = _classify_column(
                column_name      = col,
                is_numeric       = is_numeric,
                is_date_detected = is_date_detected,
                numeric_stats    = numeric_stats,
                categorical_stats = categorical_stats,
                row_count        = row_count,
            )
            result.append(descriptor)
        except Exception:
            result.append(_unknown_descriptor(col, col in numeric_profile, False, None, row_count))

    return result


# ── Utility query helpers ──────────────────────────────────────────────────────
# These are the integration hooks for KPI engine, segmentation, charting, etc.

def get_columns_by_type(semantic_profile: list[dict], semantic_type: str, min_confidence: float = 0.50) -> list[str]:
    """Return column names matching a specific semantic type."""
    return [
        s["column"] for s in semantic_profile
        if s.get("semantic_type") == semantic_type and s.get("confidence", 0) >= min_confidence
    ]


def get_columns_by_group(semantic_profile: list[dict], semantic_group: str, min_confidence: float = 0.40) -> list[str]:
    """Return column names belonging to a semantic group."""
    return [
        s["column"] for s in semantic_profile
        if s.get("semantic_group") == semantic_group and s.get("confidence", 0) >= min_confidence
    ]


def get_revenue_columns(semantic_profile: list[dict], min_confidence: float = 0.50) -> list[str]:
    """Columns classified as revenue/sales."""
    return get_columns_by_type(semantic_profile, "revenue", min_confidence)


def get_cost_columns(semantic_profile: list[dict], min_confidence: float = 0.50) -> list[str]:
    """Columns classified as cost/expense."""
    return get_columns_by_type(semantic_profile, "cost", min_confidence)


def get_profit_columns(semantic_profile: list[dict], min_confidence: float = 0.50) -> list[str]:
    """Columns classified as profit/margin."""
    return get_columns_by_type(semantic_profile, "profit", min_confidence)


def get_metric_columns(semantic_profile: list[dict], min_confidence: float = 0.45) -> list[str]:
    """All columns that are likely quantitative metrics (financial + operational)."""
    metric_groups = {"financial_metric", "operational_metric"}
    return [
        s["column"] for s in semantic_profile
        if s.get("semantic_group") in metric_groups and s.get("confidence", 0) >= min_confidence
    ]


def get_dimension_columns(semantic_profile: list[dict], min_confidence: float = 0.40) -> list[str]:
    """Columns that are likely categorical dimensions (product, region, customer, etc.)."""
    return get_columns_by_group(semantic_profile, "dimension", min_confidence)


def get_date_columns(semantic_profile: list[dict], min_confidence: float = 0.40) -> list[str]:
    """Columns classified as date or timestamp."""
    return [
        s["column"] for s in semantic_profile
        if s.get("semantic_group") == "temporal" and s.get("confidence", 0) >= min_confidence
    ]


def get_currency_columns(semantic_profile: list[dict], min_confidence: float = 0.40) -> list[str]:
    """Columns whose value distribution looks like a monetary amount."""
    return [
        s["column"] for s in semantic_profile
        if s.get("currency_like", False) and s.get("confidence", 0) >= min_confidence
    ]


def get_segmentation_candidates(semantic_profile: list[dict], min_confidence: float = 0.40) -> list[str]:
    """Low-cardinality dimension columns suitable for group-by / segmentation.

    Excludes ID columns and very high cardinality columns.
    """
    return [
        s["column"] for s in semantic_profile
        if s.get("semantic_group") == "dimension"
        and not s.get("likely_id", False)
        and s.get("cardinality_ratio", 1.0) < 0.30
        and s.get("confidence", 0) >= min_confidence
    ]


def get_id_columns(semantic_profile: list[dict], min_confidence: float = 0.50) -> list[str]:
    """Columns that are likely unique identifiers."""
    return [
        s["column"] for s in semantic_profile
        if (s.get("semantic_type") == "id" or s.get("likely_id", False))
        and s.get("confidence", 0) >= min_confidence
    ]


def get_kpi_candidates(semantic_profile: list[dict]) -> list[dict]:
    """Return ranked list of columns most suitable for business KPI display.

    Ordered by: financial_metric > operational_metric, then confidence.
    Excludes identifiers, status flags, and unknown types.
    """
    group_priority = {
        "financial_metric":  0,
        "operational_metric": 1,
        "temporal":          2,
        "dimension":         3,
        "status_flag":       4,
        "identifier":        5,
        "unknown":           6,
    }
    candidates = [
        s for s in semantic_profile
        if s.get("semantic_group") not in ("identifier", "unknown")
        and s.get("confidence", 0) >= 0.45
    ]
    candidates.sort(
        key=lambda s: (
            group_priority.get(s.get("semantic_group", "unknown"), 9),
            -s.get("confidence", 0),
        )
    )
    return candidates


def summarise_semantic_profile(semantic_profile: list[dict]) -> dict:
    """Return a concise summary of what semantic types were detected.

    Useful for logging and for passing context to the report generator.
    """
    if not semantic_profile:
        return {"total_columns": 0, "classified": 0, "groups": {}, "types": {}}

    total = len(semantic_profile)
    classified = sum(1 for s in semantic_profile if s.get("semantic_type") != "unknown")

    groups: dict[str, int] = {}
    types: dict[str, int] = {}
    for s in semantic_profile:
        g = s.get("semantic_group", "unknown")
        t = s.get("semantic_type", "unknown")
        groups[g] = groups.get(g, 0) + 1
        types[t] = types.get(t, 0) + 1

    return {
        "total_columns":       total,
        "classified":          classified,
        "classification_rate": round(classified / total, 2) if total > 0 else 0.0,
        "groups":              groups,
        "types":               types,
        "has_financial_data":  groups.get("financial_metric", 0) > 0,
        "has_dimensions":      groups.get("dimension", 0) > 0,
        "has_temporal_data":   groups.get("temporal", 0) > 0,
        "has_operational_kpis": groups.get("operational_metric", 0) > 0,
    }


# ── Column display name helper ─────────────────────────────────────────────────
# Canonical home for column-name → human-readable display transformation.
# business_kpi_engine imports this via alias so all column display logic
# lives in one module. Import with:
#   from core.intelligence.semantic_classifier import clean_col_display


def clean_col_display(col_name: str) -> str:
    """Convert snake_case / camelCase / kebab-case column name to Title Case.

    Examples:
      load_weight_kg  → Load Weight Kg
      transitDays     → Transit Days
      delay-hours     → Delay Hours
    """
    result = []
    for i, ch in enumerate(col_name):
        if i > 0 and ch.isupper() and col_name[i - 1].islower():
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
