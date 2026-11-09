"""
core/intelligence/segmentation_engine.py

Segmentation and Drilldown Intelligence Engine for ToolSmithAI.

Splits into two responsibilities:

  UPLOAD TIME  — compute_segmentation_profile_from_df()
    Called once during dataset upload when the full pandas DataFrame is
    available.  Runs groupby aggregations for all valid metric × dimension
    pairs and stores the results in segmentation_profile_json.

  REPORT TIME  — build_segmentation_section(), build_drilldown_table_section()
    Called during report generation using only the stored profile JSON.
    No raw data access, no pandas required at this stage.

Supported metric semantics:
    revenue, profit, cost, amount, price, quantity, percentage, score, risk

Supported dimension semantics:
    region, country, state, city, product, category, customer, employee,
    status, risk (when used as a categorical grouping dimension)

Priority pairs determine the order in which breakdowns are shown.
High-cardinality dimensions (>50 unique values) are automatically excluded.
Identifier columns (likely_id=True) are always excluded as dimensions.

No ML.  No AI calls.  Deterministic and fully reproducible.
All public functions return safe fallbacks on any error.
"""

import math
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.intelligence.semantic_classifier import (
    get_segmentation_candidates,
)


# ── Display label maps ─────────────────────────────────────────────────────────

_METRIC_LABEL: dict[str, str] = {
    "revenue":    "Revenue",
    "cost":       "Cost",
    "profit":     "Profit",
    "amount":     "Amount",
    "price":      "Price",
    "quantity":   "Units",
    "percentage": "Rate",
    "score":      "Score",
    "risk":       "Risk Score",
}

_DIM_LABEL: dict[str, str] = {
    "region":   "Region",
    "country":  "Country",
    "state":    "State",
    "city":     "City",
    "product":  "Product",
    "category": "Category",
    "customer": "Customer",
    "employee": "Employee / Agent",
    "status":   "Status",
    "risk":     "Risk Level",
}

# Ordered priority pairs: (metric_semantic, dimension_semantic) → priority.
# Lower number = shown first.  Pairs not in this map get priority 99.
_PAIR_PRIORITY: dict[tuple[str, str], int] = {
    ("revenue",    "region"):    1,
    ("revenue",    "product"):   2,
    ("revenue",    "category"):  3,
    ("revenue",    "employee"):  4,
    ("revenue",    "customer"):  5,
    ("profit",     "product"):   6,
    ("profit",     "category"):  7,
    ("profit",     "region"):    8,
    ("profit",     "employee"):  9,
    ("cost",       "product"):   10,
    ("cost",       "category"):  11,
    ("cost",       "region"):    12,
    ("quantity",   "product"):   13,
    ("quantity",   "category"):  14,
    ("quantity",   "region"):    15,
    ("quantity",   "employee"):  16,
    ("price",      "product"):   17,
    ("price",      "category"):  18,
    ("price",      "region"):    19,
    ("score",      "category"):  20,
    ("score",      "employee"):  21,
    ("score",      "status"):    22,
    ("risk",       "status"):    23,
    ("risk",       "category"):  24,
    ("risk",       "employee"):  25,
    ("risk",       "region"):    26,
    ("amount",     "product"):   27,
    ("amount",     "category"):  28,
    ("amount",     "region"):    29,
    ("percentage", "category"):  30,
    ("percentage", "status"):    31,
    ("percentage", "region"):    32,
}
_DEFAULT_PRIORITY = 99

# Ordered list of (metric_semantic, [preferred dimension semantics]) for upload-time
# iteration.  The first valid pair found per metric is always preferred.
_PRIORITY_PAIRS: list[tuple[str, list[str]]] = [
    ("revenue",    ["region", "product", "category", "employee", "customer"]),
    ("profit",     ["product", "category", "region", "employee"]),
    ("cost",       ["product", "category", "region"]),
    ("quantity",   ["product", "category", "region", "employee"]),
    ("price",      ["product", "category", "region"]),
    ("score",      ["category", "employee", "status"]),
    ("risk",       ["status", "category", "employee", "region"]),
    ("amount",     ["product", "category", "region"]),
    ("percentage", ["category", "status", "region"]),
]

# Safety limits
_MAX_PAIRS       = 12   # maximum breakdowns stored per dataset
_MAX_DIM_CARD    = 50   # skip dimensions with more than this many unique values
_MIN_DIM_CARD    = 2    # skip near-constant dimensions
_MAX_ROWS_STORED = 20   # rows per breakdown (sorted desc by aggregated value)
_MIN_ROWS_VALID  = 2    # minimum rows needed to be a useful breakdown
_MAX_DF_ROWS     = 100_000  # cap DataFrame size for groupby performance


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _safe_fmt(n: float, decimals: int = 2) -> str:
    if n is None:
        return "—"
    try:
        v = float(n)
        if not math.isfinite(v):
            return "—"
        abs_v = abs(v)
        sign  = "-" if v < 0 else ""
        if abs_v >= 1_000_000_000:
            return f"{sign}${abs_v / 1_000_000_000:.2f}B"
        if abs_v >= 1_000_000:
            return f"{sign}${abs_v / 1_000_000:.2f}M"
        if abs_v >= 1_000:
            return f"{sign}${abs_v / 1_000:.1f}K"
        formatted = f"{v:,.{decimals}f}"
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted
    except (TypeError, ValueError):
        return str(n)


def _fmt_pct(v: float) -> str:
    try:
        return f"{round(float(v), 1)}%"
    except Exception:
        return "—"


# ══════════════════════════════════════════════════════════════════════════════
# UPLOAD-TIME: compute and store cross-tab aggregations
# ══════════════════════════════════════════════════════════════════════════════

def compute_segmentation_profile_from_df(
    df: pd.DataFrame,
    semantic_profile: list,
    numeric_profile: dict,
    row_count: int,
) -> dict:
    """
    Compute metric × dimension cross-tabulations at upload time.

    For each high-priority (metric_col, dimension_col) pair that has semantic
    classifications, this function runs df.groupby(dim)[metric].agg(...) and
    stores the aggregated results.

    Args:
        df:               Full pandas DataFrame from the uploaded file.
        semantic_profile: Output of classify_columns() — list of column descriptors.
        numeric_profile:  Dict of col → stats from upload profiling.
        row_count:        Total rows in the dataset.

    Returns:
        Dict with "breakdowns" list and metadata.
        Always returns a valid dict — never raises.
    """
    empty = {"breakdowns": [], "computed_pairs": 0, "computed_at": datetime.now(timezone.utc).isoformat()}

    try:
        if not semantic_profile or row_count <= 0 or df is None or df.empty:
            return empty

        # Cap DataFrame size for groupby performance (deterministic: take first N rows)
        df_seg = df.head(_MAX_DF_ROWS) if len(df) > _MAX_DF_ROWS else df

        # Build semantic_type → [column_names] lookup (ordered by confidence desc)
        type_to_cols: dict[str, list[tuple[str, float]]] = {}
        for s in semantic_profile:
            st   = s.get("semantic_type", "unknown")
            col  = s.get("column", "")
            conf = float(s.get("confidence", 0.0))
            if st == "unknown" or not col:
                continue
            if st not in type_to_cols:
                type_to_cols[st] = []
            type_to_cols[st].append((col, conf))

        # Sort each type's columns by confidence desc
        for st in type_to_cols:
            type_to_cols[st].sort(key=lambda x: x[1], reverse=True)

        breakdowns: list[dict] = []
        computed = 0
        seen_pairs: set[tuple[str, str]] = set()

        for metric_sem, dim_sem_list in _PRIORITY_PAIRS:
            if computed >= _MAX_PAIRS:
                break

            metric_candidates = type_to_cols.get(metric_sem, [])
            if not metric_candidates:
                continue

            # Use the highest-confidence metric column that is numeric
            metric_col: Optional[str] = None
            metric_conf: float = 0.5
            for col, conf in metric_candidates:
                if col in numeric_profile and col in df_seg.columns:
                    metric_col  = col
                    metric_conf = conf
                    break
            if metric_col is None:
                continue

            for dim_sem in dim_sem_list:
                if computed >= _MAX_PAIRS:
                    break

                dim_candidates = type_to_cols.get(dim_sem, [])
                if not dim_candidates:
                    continue

                # Use highest-confidence dimension column that is not an ID
                dim_col: Optional[str] = None
                dim_conf: float = 0.5
                for col, conf in dim_candidates:
                    if col not in df_seg.columns:
                        continue
                    # Skip likely-ID columns
                    descriptor = next(
                        (s for s in semantic_profile if s["column"] == col), {}
                    )
                    if descriptor.get("likely_id", False):
                        continue
                    dim_col  = col
                    dim_conf = conf
                    break
                if dim_col is None:
                    continue

                pair = (metric_col, dim_col)
                if pair in seen_pairs:
                    continue

                try:
                    # Cardinality check
                    n_unique = int(df_seg[dim_col].nunique(dropna=True))
                    if n_unique < _MIN_DIM_CARD or n_unique > _MAX_DIM_CARD:
                        continue

                    # Drop rows where either column is null
                    valid = df_seg[[dim_col, metric_col]].dropna()
                    if len(valid) < 10:
                        continue

                    # Ensure metric is numeric
                    valid = valid.copy()
                    valid[metric_col] = pd.to_numeric(valid[metric_col], errors="coerce")
                    valid = valid.dropna(subset=[metric_col])
                    if len(valid) < 10:
                        continue

                    # Groupby aggregation
                    grp = (
                        valid.groupby(dim_col, as_index=False)[metric_col]
                        .agg(value="sum", count="count", avg="mean")
                    )
                    grp = grp[grp["count"] > 0].copy()
                    grp = grp.sort_values("value", ascending=False).head(_MAX_ROWS_STORED)

                    if len(grp) < _MIN_ROWS_VALID:
                        continue

                    total_val = float(grp["value"].sum())

                    rows: list[dict] = []
                    for _, r in grp.iterrows():
                        try:
                            v   = float(r["value"]) if pd.notna(r["value"]) else 0.0
                            cnt = int(r["count"])   if pd.notna(r["count"])  else 0
                            a   = float(r["avg"])   if pd.notna(r["avg"])    else (v / max(cnt, 1))
                            pct = round(v / total_val * 100, 2) if total_val else 0.0
                            label = str(r[dim_col]) if pd.notna(r[dim_col]) else ""
                            if not label:
                                continue
                            rows.append({
                                "label":        label,
                                "value":        round(v, 4),
                                "count":        cnt,
                                "avg":          round(a, 4),
                                "pct_of_total": pct,
                            })
                        except Exception:
                            continue

                    if len(rows) < _MIN_ROWS_VALID:
                        continue

                    confidence = round((metric_conf + dim_conf) / 2, 2)
                    priority   = _PAIR_PRIORITY.get((metric_sem, dim_sem), _DEFAULT_PRIORITY)

                    breakdowns.append({
                        "metric_col":         metric_col,
                        "metric_semantic":    metric_sem,
                        "dimension_col":      dim_col,
                        "dimension_semantic": dim_sem,
                        "agg_func":           "sum",
                        "priority":           priority,
                        "total":              round(total_val, 4),
                        "record_count":       row_count,
                        "rows":               rows,
                        "top_label":          rows[0]["label"]  if rows else "",
                        "top_value":          rows[0]["value"]  if rows else 0.0,
                        "bottom_label":       rows[-1]["label"] if rows else "",
                        "bottom_value":       rows[-1]["value"] if rows else 0.0,
                        "confidence":         confidence,
                    })
                    seen_pairs.add(pair)
                    computed += 1

                except Exception:
                    continue

        return {
            "breakdowns":    breakdowns,
            "computed_pairs": computed,
            "computed_at":   datetime.now(timezone.utc).isoformat(),
        }

    except Exception:
        return empty


# ══════════════════════════════════════════════════════════════════════════════
# REPORT-TIME: functions that work on stored breakdown dicts
# ══════════════════════════════════════════════════════════════════════════════

def compute_metric_by_dimension(breakdown: dict) -> list[dict]:
    """
    Return fully formatted segment rows from a stored breakdown dict.

    Each row contains: label, value, avg, count, pct_of_total, rank.
    Rows are in the original stored order (sorted by value desc at upload time).
    Never raises.
    """
    try:
        rows   = breakdown.get("rows", [])
        total  = float(breakdown.get("total", 0) or 1)
        result: list[dict] = []
        for i, row in enumerate(rows):
            try:
                value = float(row.get("value", 0) or 0)
                count = int(row.get("count", 0) or 0)
                avg   = float(row.get("avg", value / max(count, 1)) or 0)
                pct   = round(value / total * 100, 1) if total else 0.0
                result.append({
                    "label":        str(row.get("label", "")),
                    "value":        round(value, 4),
                    "avg":          round(avg, 4),
                    "count":        count,
                    "pct_of_total": pct,
                    "rank":         i + 1,
                })
            except Exception:
                continue
        return result
    except Exception:
        return []


def compute_top_segments(breakdown: dict, n: int = 5) -> list[dict]:
    """Return top N segments by total value, sorted descending. Never raises."""
    try:
        rows = compute_metric_by_dimension(breakdown)
        return sorted(rows, key=lambda r: r.get("value", 0), reverse=True)[:n]
    except Exception:
        return []


def compute_bottom_segments(breakdown: dict, n: int = 5) -> list[dict]:
    """Return bottom N segments by total value, sorted ascending. Never raises."""
    try:
        rows = compute_metric_by_dimension(breakdown)
        return sorted(rows, key=lambda r: r.get("value", 0))[:n]
    except Exception:
        return []


def compute_segment_share(breakdown: dict) -> list[dict]:
    """Return all segments sorted by pct_of_total descending. Never raises."""
    try:
        rows = compute_metric_by_dimension(breakdown)
        return sorted(rows, key=lambda r: r.get("pct_of_total", 0), reverse=True)
    except Exception:
        return []


def compute_dimension_breakdowns(segmentation_profile: dict) -> list[dict]:
    """
    Return all valid breakdowns from the stored segmentation profile,
    sorted by priority (lowest priority number = highest business importance).
    Filters out breakdowns with fewer than 2 rows.
    Never raises.
    """
    if not segmentation_profile:
        return []
    try:
        breakdowns = segmentation_profile.get("breakdowns", [])
        if not breakdowns:
            return []

        def _priority(b: dict) -> int:
            key = (b.get("metric_semantic", ""), b.get("dimension_semantic", ""))
            return _PAIR_PRIORITY.get(key, _DEFAULT_PRIORITY)

        valid = [b for b in breakdowns if b.get("rows") and len(b["rows"]) >= _MIN_ROWS_VALID]
        return sorted(valid, key=_priority)
    except Exception:
        return []


# ── Insight generation helpers ─────────────────────────────────────────────────

def _build_insight_summary(
    breakdown: dict,
    top_rows: list[dict],
    total: float,
    metric_label: str,
    dim_label: str,
) -> str:
    try:
        if not top_rows:
            return f"No {metric_label.lower()} segments available."
        top      = top_rows[0]
        top_lbl  = top.get("label", "")
        top_val  = top.get("value", 0)
        top_pct  = top.get("pct_of_total", 0)
        n_segs   = len(breakdown.get("rows", []))
        return (
            f"{top_lbl} leads {dim_label.lower()} with {_safe_fmt(top_val)} "
            f"({_fmt_pct(top_pct)} of total {metric_label.lower()}) "
            f"across {n_segs} {dim_label.lower()} segment{'s' if n_segs != 1 else ''}."
        )
    except Exception:
        return f"Top {metric_label.lower()} by {dim_label.lower()} computed."


def _build_recommended_action(metric_sem: str, dim_sem: str, top_label: str) -> str:
    actions: dict[str, str] = {
        "revenue":    f"Prioritise growth in '{top_label}' — the leading revenue contributor.",
        "profit":     f"Protect margins in '{top_label}' — the highest-profit segment.",
        "cost":       f"Review cost drivers in '{top_label}' — the highest-cost segment.",
        "quantity":   f"Align inventory with '{top_label}' — the highest-volume segment.",
        "risk":       f"Investigate risk concentration in '{top_label}' — highest risk segment.",
        "score":      f"Replicate '{top_label}' best practices across lower-performing segments.",
        "price":      f"Benchmark pricing strategy against '{top_label}' — the highest-price segment.",
        "amount":     f"Focus allocation on '{top_label}' — the highest-amount segment.",
        "percentage": f"Monitor rates in '{top_label}' — the leading percentage segment.",
    }
    return actions.get(
        metric_sem,
        f"Focus strategic attention on '{top_label}' for {metric_sem} optimisation.",
    )


# ── Section builders ───────────────────────────────────────────────────────────

def build_segmentation_section(
    segmentation_profile: dict,
    row_count: int,
    max_breakdowns: int = 4,
    top_n: int = 5,
) -> Optional[dict]:
    """
    Build a type='segmentation_insights' report section.

    Generates narrative insights for the top metric/dimension breakdowns.
    Returns None when no valid breakdowns exist.
    Never raises.

    Payload shape (renderer-friendly):
      {
        type:       "segmentation_insights",
        heading:    str,
        segments:   [{ metric, dimension, top_segments, bottom_segments,
                        total, insight_summary, recommended_action,
                        chart_hint, priority, confidence, ... }],
        items:      [str],   # plain-text lines for email/export
        priority:   "executive",
        confidence: float,
      }
    """
    try:
        if not segmentation_profile:
            return None

        breakdowns = compute_dimension_breakdowns(segmentation_profile)
        if not breakdowns:
            return None

        segments: list[dict] = []

        for bd in breakdowns:
            if len(segments) >= max_breakdowns:
                break
            try:
                metric_sem  = bd.get("metric_semantic", "")
                dim_sem     = bd.get("dimension_semantic", "")
                metric_col  = bd.get("metric_col", metric_sem)
                dim_col     = bd.get("dimension_col", dim_sem)
                total       = float(bd.get("total", 0) or 0)
                rec_count   = int(bd.get("record_count", row_count) or row_count)
                agg_func    = bd.get("agg_func", "sum")
                confidence  = round(float(bd.get("confidence", 0.75)), 2)

                metric_label = _METRIC_LABEL.get(metric_sem, metric_col.title())
                dim_label    = _DIM_LABEL.get(dim_sem, dim_col.title())

                top_rows = compute_top_segments(bd, n=top_n)
                bot_rows = compute_bottom_segments(bd, n=min(3, top_n))
                if not top_rows:
                    continue

                top_label = top_rows[0].get("label", "")
                priority  = _PAIR_PRIORITY.get((metric_sem, dim_sem), _DEFAULT_PRIORITY)

                segments.append({
                    "metric":             metric_label,
                    "metric_col":         metric_col,
                    "metric_semantic":    metric_sem,
                    "dimension":          dim_label,
                    "dimension_col":      dim_col,
                    "dimension_semantic": dim_sem,
                    "agg_func":           agg_func,
                    "total":              round(total, 4),
                    "record_count":       rec_count,
                    "top_segments":       top_rows,
                    "bottom_segments":    bot_rows,
                    "insight_summary":    _build_insight_summary(
                        bd, top_rows, total, metric_label, dim_label
                    ),
                    "recommended_action": _build_recommended_action(
                        metric_sem, dim_sem, top_label
                    ),
                    "chart_hint":  "bar",
                    "priority":    priority,
                    "confidence":  confidence,
                })

            except Exception:
                continue

        if not segments:
            return None

        items = [s.get("insight_summary", "") for s in segments if s.get("insight_summary")]

        return {
            "type":       "segmentation_insights",
            "heading":    "Segmentation Analysis",
            "segments":   segments,
            "items":      items,
            "priority":   "executive",
            "confidence": segments[0].get("confidence", 0.75),
        }

    except Exception:
        return None


def build_drilldown_table_section(
    segmentation_profile: dict,
    max_rows: int = 10,
    max_tables: int = 2,
) -> Optional[dict]:
    """
    Build a type='drilldown_table' report section for the top breakdowns.

    Presents aggregated segment data as structured tables.
    Returns None when no valid breakdowns exist.
    Never raises.

    Payload shape (renderer-friendly):
      {
        type:    "drilldown_table",
        heading: str,
        tables:  [{
          metric, metric_col, metric_semantic,
          dimension, dimension_col, dim_semantic,
          agg_func, total, priority, confidence,
          chart_hint: "bar",
          columns: [str],          # header labels
          rows: [{                 # top N rows sorted by value desc
            label, value, avg, count, pct_of_total, rank
          }],
          summary: str,
        }],
        items:      [str],
        priority:   "executive",
        confidence: float,
      }
    """
    try:
        if not segmentation_profile:
            return None

        breakdowns = compute_dimension_breakdowns(segmentation_profile)
        if not breakdowns:
            return None

        tables: list[dict] = []

        for bd in breakdowns[:max_tables]:
            try:
                metric_sem  = bd.get("metric_semantic", "")
                dim_sem     = bd.get("dimension_semantic", "")
                metric_col  = bd.get("metric_col", metric_sem)
                dim_col     = bd.get("dimension_col", dim_sem)
                total       = float(bd.get("total", 0) or 0)
                agg_func    = bd.get("agg_func", "sum")
                priority    = _PAIR_PRIORITY.get((metric_sem, dim_sem), _DEFAULT_PRIORITY)
                confidence  = round(float(bd.get("confidence", 0.75)), 2)

                metric_label = _METRIC_LABEL.get(metric_sem, metric_col.title())
                dim_label    = _DIM_LABEL.get(dim_sem, dim_col.title())

                all_rows = compute_metric_by_dimension(bd)
                if len(all_rows) < _MIN_ROWS_VALID:
                    continue

                top_rows  = sorted(all_rows, key=lambda r: r.get("value", 0), reverse=True)[:max_rows]
                top_label = top_rows[0].get("label", "") if top_rows else ""
                top_pct   = top_rows[0].get("pct_of_total", 0) if top_rows else 0

                agg_label = "Total" if agg_func == "sum" else "Average"
                summary = (
                    f"{top_label} accounts for {_fmt_pct(top_pct)} of "
                    f"total {metric_label.lower()} "
                    f"({_safe_fmt(total)} aggregate across {len(all_rows)} segments)."
                )

                tables.append({
                    "metric":          metric_label,
                    "metric_col":      metric_col,
                    "metric_semantic": metric_sem,
                    "dimension":       dim_label,
                    "dimension_col":   dim_col,
                    "dim_semantic":    dim_sem,
                    "agg_func":        agg_func,
                    "total":           round(total, 4),
                    "priority":        priority,
                    "confidence":      confidence,
                    "chart_hint":      "bar",
                    "columns":         [dim_label, agg_label, "Avg per Row", "Record Count", "Share %"],
                    "rows":            top_rows,
                    "summary":         summary,
                })

            except Exception:
                continue

        if not tables:
            return None

        items = [t["summary"] for t in tables if t.get("summary")]

        return {
            "type":       "drilldown_table",
            "heading":    "Drilldown Analysis",
            "tables":     tables,
            "items":      items,
            "priority":   "executive",
            "confidence": tables[0].get("confidence", 0.75),
        }

    except Exception:
        return None
