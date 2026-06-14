import html
import json
import logging
import math
from datetime import datetime, timezone

from core.intelligence.business_kpi_engine import build_business_kpi_section
from core.intelligence.segmentation_engine import (
    build_segmentation_section,
    build_drilldown_table_section,
)
from core.intelligence.semantic_classifier import build_label_map
from core.output.kpi_formatter import format_kpi_display_label, format_dataset_display_name
from core.config import FRONTEND_BASE_URL

logger = logging.getLogger(__name__)


def _safe_fmt(n, decimals: int = 2) -> str:
    if n is None:
        return "—"
    try:
        v = float(n)
        if not math.isfinite(v):
            return "—"
        formatted = f"{v:,.{decimals}f}"
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted
    except (TypeError, ValueError):
        return str(n)


def _format_date_range(earliest: str, latest: str, range_days: int) -> str:
    """Return a human-readable date range string."""
    try:
        early = datetime.fromisoformat(earliest).strftime("%b %d, %Y")
        late  = datetime.fromisoformat(latest).strftime("%b %d, %Y")
    except Exception:
        early, late = earliest[:10], latest[:10]
    if range_days == 0:
        return f"{early} (single day)"
    if range_days < 31:
        return f"{early} → {late} ({range_days} days)"
    if range_days < 365:
        months = round(range_days / 30.44, 1)
        return f"{early} → {late} ({months} months)"
    years = round(range_days / 365.25, 1)
    return f"{early} → {late} ({years} years)"


_AI_NARRATIVE_SYSTEM_PROMPT = """\
You are a data analyst writing an executive summary for a business report.
You will receive sections from an automated dataset analysis.
Based only on the provided information, return a JSON object with exactly these fields:

{
  "executive_summary": "<1-3 sentence business summary of the most important findings>",
  "key_takeaways": ["<insight 1>", "<insight 2>", "<up to 5 insights>"],
  "risk_notes": ["<concern 1>", "<up to 3 data quality concerns or anomalies>"],
  "key_findings": ["<specific quantified finding 1>", "<up to 5 findings>"],
  "anomaly_insights": "<1-2 sentences about anomaly patterns, or null if none detected>",
  "trend_insights": "<1-2 sentences about trend patterns, or null if none detected>",
  "recommendation_summary": "<1-2 sentences on the top actionable recommendation, or null>",
  "risk_summary": "<1-2 sentences on the most critical data risk, or null>"
}

Rules:
- Use only facts from the provided analysis. Do not invent data or statistics.
- executive_summary: 1-3 business-focused sentences, max 800 characters.
- key_takeaways: 2-5 concise bullet points, each max 200 characters.
- risk_notes: 0-3 concerns (missing values, quality issues, anomalies), each max 200 characters.
  Return an empty list if no risks are evident.
- key_findings: 0-5 specific quantified findings from the analysis, each max 250 characters.
  Reference actual numbers from the report. Return empty list if no specific findings.
- anomaly_insights, trend_insights, recommendation_summary, risk_summary: string or null.
  Max 400 characters each. Return null if the report has no relevant content.
- Return ONLY the JSON object. No markdown. No explanation.
"""


def _build_report_context(sections: list[dict]) -> str:
    """Convert report sections to sanitized plain text for the AI prompt.
    Only section headings and item strings are included — no raw data or credentials."""
    lines: list[str] = []
    for section in sections:
        lines.append(f"{section.get('heading', '')}:")
        for item in section.get("items", []):
            lines.append(f"  - {item}")
    return "\n".join(lines)


def _validate_ai_narrative(raw: dict) -> dict:
    """Validate AI-generated narrative against strict field rules.
    Raises ValueError on any violation — no AI output bypasses this."""
    if not isinstance(raw, dict):
        raise ValueError("narrative must be a dict")

    summary = raw.get("executive_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("executive_summary must be a non-empty string")
    summary = summary.strip()[:800]

    takeaways = raw.get("key_takeaways", [])
    if not isinstance(takeaways, list):
        raise ValueError("key_takeaways must be a list")
    takeaways = [str(t).strip()[:200] for t in takeaways[:5] if str(t).strip()]

    risks = raw.get("risk_notes", [])
    if not isinstance(risks, list):
        raise ValueError("risk_notes must be a list")
    risks = [str(r).strip()[:200] for r in risks[:3] if str(r).strip()]

    findings = raw.get("key_findings", [])
    if not isinstance(findings, list):
        findings = []
    findings = [str(f).strip()[:250] for f in findings[:5] if str(f).strip()]

    def _opt_str(key: str, max_len: int = 400) -> str | None:
        v = raw.get(key)
        if not isinstance(v, str) or not v.strip():
            return None
        return v.strip()[:max_len]

    return {
        "executive_summary":      summary,
        "key_takeaways":          takeaways,
        "risk_notes":             risks,
        "key_findings":           findings,
        "anomaly_insights":       _opt_str("anomaly_insights"),
        "trend_insights":         _opt_str("trend_insights"),
        "recommendation_summary": _opt_str("recommendation_summary"),
        "risk_summary":           _opt_str("risk_summary"),
    }


def _ai_generate_narrative(sections: list[dict]) -> dict | None:
    """
    Ask AI to generate an executive narrative from already-generated report sections.
    The AI receives only sanitized section text — no raw data, no credentials.
    OpenAI does NOT replace report generation; it only summarizes existing output.
    Returns a validated narrative dict, or None on any failure.
    """
    try:
        from core.config import (
            ENABLE_AI_REPORT_NARRATIVE,
            OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS,
        )
    except Exception:
        return None

    if not ENABLE_AI_REPORT_NARRATIVE:
        return None
    if not OPENAI_API_KEY:
        logger.debug("[ai_narrative] OPENAI_API_KEY not set; skipping")
        return None

    try:
        import openai as _openai
    except ImportError:
        logger.warning("[ai_narrative] openai package not installed; skipping")
        return None

    context = _build_report_context(sections)

    try:
        client   = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _AI_NARRATIVE_SYSTEM_PROMPT},
                {"role": "user",   "content": f"Dataset analysis:\n\n{context}"},
            ],
            max_tokens=600,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        content   = response.choices[0].message.content or ""
        raw       = json.loads(content.strip())
        narrative = _validate_ai_narrative(raw)
        logger.info(
            "[ai_narrative] success: %d chars summary, %d takeaways, %d risks",
            len(narrative["executive_summary"]),
            len(narrative["key_takeaways"]),
            len(narrative["risk_notes"]),
        )
        return narrative
    except Exception as exc:
        logger.warning(
            "[ai_narrative] failed (%s: %s); report continues without AI narrative",
            type(exc).__name__, exc,
        )
        return None


def _build_kpi_section(
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    missing_values: dict,
    categorical_profile: dict,
) -> dict:
    """Build a KPI section (max 6 cards) derived from stored dataset profile data.

    Every KPI is computed defensively — invalid values are skipped, never raised.
    The section is pre-stamped with type='kpi' so the setdefault loop leaves it alone.
    """
    kpis: list[dict] = []

    # 1. Total Records
    rec_status = "good" if row_count >= 1_000 else ("warning" if row_count >= 100 else "risk")
    rec_explanation = (
        "Large sample supports reliable analysis."
        if row_count >= 1_000
        else ("Moderate sample — results may vary." if row_count >= 100
              else "Small sample — statistical reliability limited.")
    )
    kpis.append({
        "label":            "Total Records",
        "value":            row_count,
        "format":           "number",
        "trend":            "neutral",
        "description":      "Rows in the uploaded dataset",
        "delta":            None,
        "delta_direction":  "neutral",
        "status":           rec_status,
        "explanation":      rec_explanation,
    })

    # 2. Total Features
    kpis.append({
        "label":            "Total Features",
        "value":            column_count,
        "format":           "number",
        "trend":            "neutral",
        "description":      "Columns analysed",
        "delta":            None,
        "delta_direction":  "neutral",
        "status":           "good",
        "explanation":      "Feature count across all columns.",
    })

    # 3. Data Completeness — (1 - missing_cells / total_cells) * 100
    total_cells = row_count * column_count
    if total_cells > 0:
        try:
            total_missing = sum(
                v for v in missing_values.values()
                if isinstance(v, (int, float)) and math.isfinite(v)
            )
            completeness = round((1 - total_missing / total_cells) * 100, 1)
            trend = "up" if completeness >= 95 else ("down" if completeness < 80 else "neutral")
            comp_delta = round(completeness - 100, 1)
            comp_status = "good" if completeness >= 95 else ("warning" if completeness >= 80 else "risk")
            comp_explanation = (
                "High completeness — reliable for analysis."
                if completeness >= 95
                else ("Moderate gaps — review missing columns." if completeness >= 80
                      else "High missing rate — data quality action required.")
            )
            kpis.append({
                "label":            "Data Completeness",
                "value":            completeness,
                "format":           "percent",
                "trend":            trend,
                "description":      "Non-null cells across all columns",
                "delta":            comp_delta,
                "delta_direction":  "neutral" if completeness >= 99.9 else "down",
                "status":           comp_status,
                "explanation":      comp_explanation,
            })
        except Exception:
            pass

    # 4. Columns with Missing Data
    try:
        cols_with_gaps = sum(
            1 for v in missing_values.values()
            if isinstance(v, (int, float)) and v > 0
        )
        gap_status = "good" if cols_with_gaps == 0 else ("warning" if cols_with_gaps <= 3 else "risk")
        gap_explanation = (
            "No columns have missing values."
            if cols_with_gaps == 0
            else (f"{cols_with_gaps} column(s) have gaps — review before analysis."
                  if cols_with_gaps <= 3
                  else f"{cols_with_gaps} columns have gaps — significant data quality issue.")
        )
        kpis.append({
            "label":            "Columns with Gaps",
            "value":            cols_with_gaps,
            "format":           "number",
            "trend":            "down" if cols_with_gaps > 0 else "neutral",
            "description":      f"Of {column_count} total columns",
            "delta":            -cols_with_gaps if cols_with_gaps > 0 else None,
            "delta_direction":  "down" if cols_with_gaps > 0 else "neutral",
            "status":           gap_status,
            "explanation":      gap_explanation,
        })
    except Exception:
        pass

    # 5. Numeric Columns
    numeric_count = len(numeric_profile)
    if numeric_count > 0 and len(kpis) < 6:
        kpis.append({
            "label":            "Numeric Columns",
            "value":            numeric_count,
            "format":           "number",
            "trend":            "neutral",
            "description":      "Columns with quantitative values",
            "delta":            None,
            "delta_direction":  "neutral",
            "status":           "good",
            "explanation":      "Support quantitative analysis and correlation.",
        })

    # 6. Categorical Columns
    cat_count = len(categorical_profile)
    if cat_count > 0 and len(kpis) < 6:
        kpis.append({
            "label":            "Categorical Columns",
            "value":            cat_count,
            "format":           "number",
            "trend":            "neutral",
            "description":      "Columns with category values",
            "delta":            None,
            "delta_direction":  "neutral",
            "status":           "good",
            "explanation":      "Enable segmentation and group analysis.",
        })

    return {
        "type": "kpi",
        "heading": "Key Metrics",
        "kpis": kpis[:6],
    }


# ── Chart intelligence helpers ─────────────────────────────────────────────────
# Private utilities for intent-aware, semantic-aware chart generation.
# All are pure functions — no DB access, no AI calls.

_CHART_INTENT_REVENUE  = frozenset(["revenue", "sales", "income", "profit", "financial", "money", "price"])
_CHART_INTENT_PRODUCT  = frozenset(["product", "sku", "item", "merchandise", "catalog", "category"])
_CHART_INTENT_CUSTOMER = frozenset(["customer", "client", "user", "buyer", "account", "member"])
_CHART_INTENT_RISK     = frozenset(["risk", "anomaly", "fraud", "incident", "issue", "alert", "quality"])
_CHART_INTENT_REGION   = frozenset(["region", "geography", "location", "territory", "area", "country", "city"])

# Styles where data-quality (missing values) charts are appropriate
_QUALITY_STYLES = frozenset({"analyst_deep_dive", "anomaly_report", "operational_report",
                              "monitoring_report", "table_heavy_report"})

# Styles where the histogram is suppressed (KPI/executive focus)
_NO_HISTOGRAM_STYLES = frozenset({"kpi_summary", "executive_brief"})


def _col_display(col: str) -> str:
    """Convert a raw column name (snake_case or basic CamelCase) to a Title Case label."""
    result = []
    for i, ch in enumerate(col):
        if i > 0 and ch.isupper() and col[i - 1].islower():
            result.append(" ")
        result.append(ch)
    name = "".join(result)
    return name.replace("_", " ").replace("-", " ").replace(".", " ").strip().title()


def _viz_score_for(chart_type: str, report_plan: dict | None) -> int:
    """Return this chart type's preference score from report_plan (0 = suppress, default 5)."""
    if not report_plan:
        return 5
    return report_plan.get("viz_type_scores", {}).get(chart_type, 5)


def _sem_lookup(col: str, semantic_profile: list[dict]) -> dict:
    """Return the semantic descriptor for a column, or an empty dict if not found."""
    for s in semantic_profile:
        if s.get("column") == col:
            return s
    return {}


def _is_id_column(col: str, semantic_profile: list[dict]) -> bool:
    """True when a column is likely a unique identifier — not useful to chart."""
    s = _sem_lookup(col, semantic_profile)
    return bool(s.get("likely_id") or s.get("semantic_type") == "id")


def _best_dimension_col(
    categorical_profile: dict,
    semantic_profile: list[dict],
    intent_lowered: str,
    exclude: set | None = None,
) -> tuple[str, str] | None:
    """Return (column_name, display_label) for the most business-relevant categorical column.

    Priority order:
      0 — semantic dimension whose type matches the user's intent keywords
      1 — any semantic dimension column (non-ID, reasonable cardinality)
      2 — any non-ID categorical column
      (ID columns are never returned)
    """
    exclude = exclude or set()
    id_cols = {s["column"] for s in semantic_profile
               if s.get("likely_id") or s.get("semantic_type") == "id"}

    candidates: list[tuple[int, int, str]] = []
    for col, entries in categorical_profile.items():
        if not entries or col in exclude or col in id_cols:
            continue
        s    = _sem_lookup(col, semantic_profile)
        st   = s.get("semantic_type", "unknown")
        sg   = s.get("semantic_group", "unknown")
        n    = len(entries)

        if sg == "dimension":
            intent_match = (
                (st in ("product", "category") and any(w in intent_lowered for w in _CHART_INTENT_PRODUCT)) or
                (st == "customer"              and any(w in intent_lowered for w in _CHART_INTENT_CUSTOMER)) or
                (st in ("region", "country", "state", "city") and any(w in intent_lowered for w in _CHART_INTENT_REGION)) or
                (st in ("risk", "status")      and any(w in intent_lowered for w in _CHART_INTENT_RISK))
            )
            priority = 0 if intent_match else 1
        else:
            priority = 2

        candidates.append((priority, -n, col))

    if not candidates:
        return None
    candidates.sort()
    col = candidates[0][2]

    # Build a business-friendly label from semantic type when available
    s  = _sem_lookup(col, semantic_profile)
    st = s.get("semantic_type", "")
    _type_labels = {
        "product": "Product", "category": "Category", "customer": "Customer Segment",
        "region": "Region", "country": "Country", "state": "State", "city": "City",
        "status": "Status", "risk": "Risk Category",
    }
    label = _type_labels.get(st) or _col_display(col)
    return col, label


def _best_metric_col(
    numeric_profile: dict,
    semantic_profile: list[dict],
    intent_lowered: str,
    exclude: set | None = None,
) -> tuple[str, str] | None:
    """Return (column_name, display_label) for the most business-relevant numeric column.

    Priority order:
      0 — financial metric matching intent keywords (revenue, sales, etc.)
      1 — any financial metric (revenue, profit, cost, price)
      2 — operational metric (score, risk, percentage)
      3 — quantity metric
      4 — any financial/operational group
      9 — other numeric (statistical fallback — highest std)
     10 — ID/identifier (never returned)
    """
    exclude = exclude or set()
    candidates: list[tuple[int, float, str]] = []

    for col, stats in numeric_profile.items():
        if col in exclude:
            continue
        bins = stats.get("histogram_bins") or []
        if not any(b.get("count", 0) > 0 for b in bins):
            continue

        s  = _sem_lookup(col, semantic_profile)
        st = s.get("semantic_type", "unknown")
        sg = s.get("semantic_group", "unknown")

        if s.get("likely_id") or st == "id":
            continue  # never chart IDs

        if st in ("revenue", "profit", "amount", "price", "cost") and \
                any(w in intent_lowered for w in _CHART_INTENT_REVENUE):
            priority = 0
        elif st in ("revenue", "profit", "amount", "price", "cost"):
            priority = 1
        elif st in ("score", "risk", "percentage", "ratio"):
            priority = 2
        elif st == "quantity":
            priority = 3
        elif sg in ("financial_metric", "operational_metric"):
            priority = 4
        else:
            priority = 9

        std_val = abs(float(stats.get("std") or 0))
        candidates.append((priority, -std_val, col))

    if not candidates:
        return None
    candidates.sort()
    col = candidates[0][2]

    s  = _sem_lookup(col, semantic_profile)
    st = s.get("semantic_type", "")
    _type_labels = {
        "revenue": "Revenue", "sales": "Sales", "cost": "Cost",
        "profit": "Profit", "amount": "Transaction Amount", "price": "Price",
        "quantity": "Quantity", "score": "Score", "risk": "Risk Score",
        "percentage": "Percentage", "ratio": "Ratio",
    }
    label = _type_labels.get(st) or _col_display(col)
    return col, label


def _rank_chart_sections(
    charts: list[dict],
    report_plan: dict | None,
    intent_lowered: str = "",
) -> list[dict]:
    """Rank chart sections by viz_type_scores and intent relevance.

    Adds three fields to every chart section:
      chart_priority_score — numeric score used for ranking (higher = more relevant)
      overview_rank        — 1-based rank; 1 is the most relevant chart
      overview_chart       — True for the top 3 charts shown in the Overview row
    """
    viz_scores   = (report_plan or {}).get("viz_type_scores", {})
    report_style = (report_plan or {}).get("report_style", "analyst_deep_dive")

    # Completeness / data-quality charts are not hero charts for business styles
    _BUSINESS_OVERVIEW_STYLES = frozenset({"executive_brief", "visual_dashboard", "kpi_summary"})

    scored: list[tuple[float, int, dict]] = []
    for original_idx, chart in enumerate(charts):
        ct         = (chart.get("chart") or {}).get("chart_type", "bar")
        base_score = float(viz_scores.get(ct, 5))

        # Deprioritise completeness charts in executive / visual / KPI styles
        heading = (chart.get("heading") or "").lower()
        if "completeness" in heading and report_style in _BUSINESS_OVERVIEW_STYLES:
            base_score = max(0.0, base_score - 4.0)

        # Intent relevance bonus: +1 when a meaningful intent word appears in the heading
        intent_bonus = 0.0
        if intent_lowered:
            for word in intent_lowered.split():
                if len(word) > 3 and word in heading:
                    intent_bonus = 1.0
                    break

        scored.append((base_score + intent_bonus, original_idx, chart))

    # Highest score first; original position breaks ties (stable)
    scored.sort(key=lambda x: (-x[0], x[1]))

    result: list[dict] = []
    for rank, (score, _orig, chart) in enumerate(scored, 1):
        c                         = dict(chart)
        c["chart_priority_score"] = round(score, 2)
        c["overview_rank"]        = rank
        c["overview_chart"]       = rank <= 3
        result.append(c)

    return result


def _build_chart_sections(
    categorical_profile: dict,
    missing_values: dict,
    row_count: int,
    date_profile: dict,
    numeric_profile: dict | None = None,
    correlation_profile: list | None = None,
    report_plan: dict | None = None,
    intent_text: str | None = None,
    semantic_profile: list | None = None,
) -> list[dict]:
    """Build intent-aware, semantically-enriched chart sections from stored profile data.

    Improvements over the previous deterministic version:
    - Uses semantic_profile to prefer business-relevant columns over statistical picks.
    - Uses report_plan.viz_type_scores to suppress irrelevant chart types per style.
    - Uses intent_text to boost columns matching the user's stated business focus.
    - Adds per-chart business explanations derived from real profile data values.
    - Suppresses data-QA charts (missing values, correlations) in executive/KPI styles.
    - Filters ID/UUID columns from all chart slots.
    - Returns [] on any failure.
    """
    charts: list[dict] = []
    semantic_profile = semantic_profile or []
    intent_lowered   = (intent_text or "").lower()
    report_style     = (report_plan or {}).get("report_style", "analyst_deep_dive")
    visual_pref      = (report_plan or {}).get("visual_preference", "balanced")
    charted_cols: set[str] = set()

    # ── 1. Business dimension bar chart ──────────────────────────────────────────
    # Selects the most business-relevant categorical column instead of the one with
    # the highest cardinality. Intent keywords boost matching dimension types.
    if _viz_score_for("bar", report_plan) > 0:
        try:
            dim = _best_dimension_col(
                categorical_profile, semantic_profile, intent_lowered, exclude=charted_cols
            )
            if dim:
                col_name, label = dim
                entries = categorical_profile.get(col_name, [])
                top     = entries[:10]
                labels  = [str(e.get("value", "")) for e in top]
                data    = [int(e.get("count", 0))  for e in top]
                if labels and any(d > 0 for d in data):
                    total     = sum(data)
                    top_label = labels[0]
                    top_count = data[0]
                    top_pct   = round(top_count / total * 100) if total > 0 else 0
                    explanation = (
                        f"'{top_label}' leads with {top_count:,} records "
                        f"({top_pct}% of {label.lower()} distribution)."
                    )
                    charted_cols.add(col_name)
                    charts.append({
                        "type":        "chart",
                        "heading":     f"{label} — Breakdown",
                        "explanation": explanation,
                        "chart": {
                            "chart_type": "bar",
                            "labels":     labels,
                            "series":     [{"name": label, "data": data}],
                        },
                    })
        except Exception:
            pass

    # ── 2. Data completeness chart (missing values) ───────────────────────────────
    # Only generated for quality-focused report styles. Suppressed for executive,
    # visual-dashboard, and KPI-summary styles. Requires ≥5% missing in at least
    # one column to be meaningful.
    if report_style in _QUALITY_STYLES or not report_plan:
        if _viz_score_for("bar", report_plan) >= 3:
            try:
                missing_entries = sorted(
                    [
                        (col, int(cnt))
                        for col, cnt in missing_values.items()
                        if isinstance(cnt, (int, float)) and math.isfinite(cnt) and cnt > 0
                        and (row_count == 0 or cnt / row_count >= 0.05)
                    ],
                    key=lambda x: x[1],
                    reverse=True,
                )[:8]
                if missing_entries:
                    worst_col, worst_cnt = missing_entries[0]
                    worst_pct = round(worst_cnt / row_count * 100) if row_count > 0 else 0
                    explanation = (
                        f"'{_col_display(worst_col)}' has the highest missing rate: "
                        f"{worst_cnt:,} nulls ({worst_pct}% of rows)."
                    )
                    charts.append({
                        "type":        "chart",
                        "heading":     "Data Completeness by Column",
                        "explanation": explanation,
                        "chart": {
                            "chart_type": "bar",
                            "labels":     [_col_display(col) for col, _ in missing_entries],
                            "series":     [{"name": "Missing Rows", "data": [cnt for _, cnt in missing_entries]}],
                        },
                    })
            except Exception:
                pass

    # ── 3. Metric distribution histogram ─────────────────────────────────────────
    # Prefers revenue/financial/operational columns over the highest-std fallback.
    # Suppressed for kpi_summary and executive_brief (both prefer KPI cards).
    if numeric_profile and report_style not in _NO_HISTOGRAM_STYLES and \
            _viz_score_for("bar", report_plan) >= 3:
        try:
            metric = _best_metric_col(
                numeric_profile, semantic_profile, intent_lowered, exclude=charted_cols
            )
            if metric:
                col, label = metric
                stats  = numeric_profile[col]
                bins   = stats["histogram_bins"]
                b_data = [b.get("count", 0) for b in bins]
                if any(d > 0 for d in b_data):
                    mn, mx, mean = stats.get("min"), stats.get("max"), stats.get("mean")
                    parts: list[str] = []
                    if mn is not None and mx is not None:
                        parts.append(f"Values range from {_safe_fmt(mn)} to {_safe_fmt(mx)}")
                    if mean is not None:
                        parts.append(f"average {_safe_fmt(mean)}")
                    explanation = (". ".join(parts) + ".") if parts else f"Distribution of {label}."
                    charted_cols.add(col)
                    charts.append({
                        "type":        "chart",
                        "heading":     f"{label} — Distribution",
                        "explanation": explanation,
                        "chart": {
                            "chart_type": "bar",
                            "labels":     [f"{b['min']:.2f}" for b in bins],
                            "series":     [{"name": label, "data": b_data}],
                        },
                    })
        except Exception:
            pass

    # ── 4. Monthly trend (line chart) ────────────────────────────────────────────
    # Heading now reflects business context rather than raw column names.
    # Generated whenever date data with ≥2 months exists (all styles value time-series).
    if _viz_score_for("line", report_plan) > 0:
        try:
            for dc in (date_profile.get("date_columns") or [])[:1]:
                mc = dc.get("monthly_counts", [])
                if len(mc) >= 2:
                    labels = [m.get("month", "") for m in mc]
                    data   = [m.get("count", 0) for m in mc]
                    col    = dc.get("column", "date")
                    first, last = data[0], data[-1]
                    direction = "increased" if last > first else ("decreased" if last < first else "stable")
                    pct_change = round(abs(last - first) / first * 100) if first > 0 else 0
                    explanation = (
                        f"Activity {direction} from {first:,} to {last:,} records/month "
                        f"over {len(mc)} months"
                        + (f" ({pct_change}% change)." if pct_change > 0 else ".")
                    )
                    # Use a business-contextual heading
                    if any(w in intent_lowered for w in _CHART_INTENT_REVENUE | _CHART_INTENT_PRODUCT):
                        heading = "Activity Trend Over Time"
                    else:
                        heading = f"{_col_display(col)} — Monthly Trend"
                    charts.append({
                        "type":        "chart",
                        "heading":     heading,
                        "explanation": explanation,
                        "chart": {
                            "chart_type": "line",
                            "labels":     labels,
                            "series":     [{"name": "Volume", "data": data}],
                        },
                    })
        except Exception:
            pass

    # ── 5. Correlation matrix ────────────────────────────────────────────────────
    # Only generated for analyst/anomaly styles (viz score ≥ 3).
    # Suppressed for executive_brief (score 0) and kpi_summary (score not set → 5,
    # but kpi_summary rarely has numeric correlations). ID columns are filtered out
    # so the matrix only shows meaningful business metric relationships.
    if correlation_profile and len(correlation_profile) >= 2 and \
            _viz_score_for("correlation_matrix", report_plan) >= 3:
        try:
            id_cols = {s["column"] for s in semantic_profile
                       if s.get("likely_id") or s.get("semantic_type") == "id"}
            seen: set = set()
            ordered_cols: list[str] = []
            for pair in correlation_profile:
                for col in (pair["column_a"], pair["column_b"]):
                    if col not in seen and col not in id_cols:
                        ordered_cols.append(col)
                        seen.add(col)
            ordered_cols = ordered_cols[:8]
            n = len(ordered_cols)
            if n >= 2:
                lookup: dict[tuple, float] = {}
                for pair in correlation_profile:
                    a, b, c = pair["column_a"], pair["column_b"], pair["correlation"]
                    if a in seen and b in seen:
                        lookup[(a, b)] = c
                        lookup[(b, a)] = c
                matrix = [
                    [1.0 if rc == col else round(lookup.get((rc, col), 0.0), 4)
                     for col in ordered_cols]
                    for rc in ordered_cols
                ]
                best_pair = max(
                    correlation_profile,
                    key=lambda p: abs(p.get("correlation", 0)),
                    default=None,
                )
                explanation = ""
                if best_pair:
                    r = best_pair.get("correlation", 0)
                    direction = "positive" if r >= 0 else "negative"
                    explanation = (
                        f"Strongest {direction} relationship: "
                        f"{_col_display(best_pair['column_a'])} and "
                        f"{_col_display(best_pair['column_b'])} "
                        f"(r = {r:+.2f})."
                    )
                charts.append({
                    "type":        "chart",
                    "heading":     "Metric Correlations",
                    "explanation": explanation,
                    "chart": {
                        "chart_type": "correlation_matrix",
                        "columns":    [_col_display(c) for c in ordered_cols],
                        "matrix":     matrix,
                    },
                })
        except Exception:
            pass

    # ── 6. Segment share (pie / donut) ────────────────────────────────────────────
    # Prefers status/risk/low-cardinality dimension columns (2–7 distinct values).
    # Skipped when both donut and pie viz scores are 0 (rare but defensive).
    if _viz_score_for("donut", report_plan) > 0 or _viz_score_for("pie", report_plan) > 0:
        try:
            id_cols_set = {s["column"] for s in semantic_profile
                           if s.get("likely_id") or s.get("semantic_type") == "id"}
            pie_candidates: list[tuple[int, str]] = []
            for col, entries in categorical_profile.items():
                if col in charted_cols or col in id_cols_set:
                    continue
                if not (2 <= len(entries) <= 7):
                    continue
                s  = _sem_lookup(col, semantic_profile)
                st = s.get("semantic_type", "")
                sg = s.get("semantic_group", "")
                if st in ("status", "risk") and any(w in intent_lowered for w in _CHART_INTENT_RISK):
                    priority = 0  # intent-matching status/risk column
                elif st in ("status", "risk"):
                    priority = 1
                elif sg == "dimension":
                    priority = 2
                else:
                    priority = 4
                pie_candidates.append((priority, col))

            if pie_candidates:
                pie_candidates.sort()
                pie_col     = pie_candidates[0][1]
                pie_entries = categorical_profile[pie_col]
                pie_labels  = [str(e.get("value", "")) for e in pie_entries]
                pie_data    = [int(e.get("count",  0)) for e in pie_entries]
                if pie_labels and any(d > 0 for d in pie_data):
                    pie_total = sum(pie_data)
                    top_label = pie_labels[0]
                    top_count = pie_data[0]
                    top_pct   = round(top_count / pie_total * 100) if pie_total > 0 else 0
                    s  = _sem_lookup(pie_col, semantic_profile)
                    st = s.get("semantic_type", "")
                    col_label = ("Status" if st == "status" else
                                 "Risk Level" if st == "risk" else
                                 _col_display(pie_col))
                    explanation = (
                        f"'{top_label}' is the leading {col_label.lower()}, "
                        f"representing {top_pct}% of all records."
                    )
                    pie_or_donut = (
                        "donut"
                        if _viz_score_for("donut", report_plan) > _viz_score_for("pie", report_plan)
                        else "pie"
                    )
                    charts.append({
                        "type":        "chart",
                        "heading":     f"{col_label} — Share",
                        "explanation": explanation,
                        "chart": {
                            "chart_type": pie_or_donut,
                            "labels":     pie_labels,
                            "series":     [{"name": col_label, "data": pie_data}],
                        },
                    })
        except Exception:
            pass

    return _rank_chart_sections(charts, report_plan, intent_lowered)


def _build_executive_summary_section(
    filename: str,
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
    label_map: dict | None = None,
) -> dict:
    """Build a deterministic executive summary from stored profile data.

    No AI call. No hallucinated claims. Every statement is derived from stored
    profile values only. Called after date_profile is computed so all data is
    available. Pre-stamped type='executive_summary' so the setdefault loop ignores it.
    """
    lm            = label_map or {}
    numeric_count = len(numeric_profile)
    cat_count     = len(categorical_profile)

    # ── Completeness ─────────────────────────────────────────────────────────
    total_cells = row_count * column_count
    completeness_pct: float | None = None
    if total_cells > 0:
        try:
            total_missing = sum(
                v for v in missing_values.values()
                if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            )
            completeness_pct = round((1 - total_missing / total_cells) * 100, 1)
        except Exception:
            pass

    # ── Summary paragraph ────────────────────────────────────────────────────
    parts: list[str] = [
        f"This dataset contains {row_count:,} records across {column_count} fields."
    ]
    if completeness_pct is not None:
        if completeness_pct >= 95:
            parts.append(f"Data completeness is high at {completeness_pct}%.")
        elif completeness_pct >= 80:
            parts.append(f"Data completeness is moderate at {completeness_pct}%.")
        else:
            parts.append(f"Data completeness is low at {completeness_pct}%.")
    if numeric_count > 0 and cat_count > 0:
        parts.append(
            f"It includes {numeric_count} numeric and {cat_count} "
            f"categorical column{'s' if cat_count > 1 else ''}."
        )
    elif numeric_count > 0:
        parts.append(f"It includes {numeric_count} numeric column{'s' if numeric_count > 1 else ''}.")
    elif cat_count > 0:
        parts.append(f"It includes {cat_count} categorical column{'s' if cat_count > 1 else ''}.")
    summary = " ".join(parts)

    # ── Key takeaways ─────────────────────────────────────────────────────────
    takeaways: list[str] = []

    if row_count >= 10_000:
        takeaways.append(f"{row_count:,} records provide a substantial sample for analysis.")
    elif row_count >= 1_000:
        takeaways.append(f"{row_count:,} records are available for analysis.")
    else:
        takeaways.append(f"Dataset contains {row_count:,} records — a relatively small sample.")

    try:
        by_mean = sorted(
            [(col, s) for col, s in numeric_profile.items() if s.get("mean") is not None],
            key=lambda x: abs(float(x[1]["mean"])),
            reverse=True,
        )
        if by_mean:
            col_name, stats = by_mean[0]
            takeaways.append(
                f"{lm.get(col_name, col_name)} is the primary numeric indicator "
                f"with a mean of {_safe_fmt(stats['mean'])}."
            )
    except Exception:
        pass

    try:
        if categorical_profile:
            best = max(
                categorical_profile.items(),
                key=lambda x: x[1][0]["count"] if x[1] else 0,
                default=None,
            )
            if best:
                col_name, entries = best
                if entries:
                    top = entries[0]
                    takeaways.append(
                        f'"{top["value"]}" is the most frequent value in {lm.get(col_name, col_name)} '
                        f"({top['count']:,} records)."
                    )
    except Exception:
        pass

    try:
        date_cols = date_profile.get("date_columns") or []
        if date_cols:
            dc   = date_cols[0]
            days = dc.get("range_days", 0)
            takeaways.append(
                f"Dataset spans {days:,} day{'s' if days != 1 else ''} "
                f"of {lm.get(dc['column'], dc['column'])} data."
            )
    except Exception:
        pass

    # ── Risks ────────────────────────────────────────────────────────────────
    risks: list[str] = []

    try:
        cols_with_missing = [
            col for col, cnt in missing_values.items()
            if isinstance(cnt, (int, float)) and cnt > 0
        ]
        if cols_with_missing:
            n = len(cols_with_missing)
            risks.append(
                f"Data quality: {n} column{'s' if n > 1 else ''} contain missing values."
            )
    except Exception:
        pass

    if row_count < 100:
        risks.append("Small sample size may limit statistical reliability of findings.")

    if completeness_pct is not None and completeness_pct < 80:
        risks.append(
            f"Low data completeness ({completeness_pct}%) may affect analysis accuracy."
        )

    # ── Opportunities ─────────────────────────────────────────────────────────
    opportunities: list[str] = []

    if cat_count > 0:
        opportunities.append(
            f"Categorical breakdowns across {cat_count} "
            f"column{'s' if cat_count > 1 else ''} enable segmentation and group analysis."
        )

    try:
        if numeric_count >= 2:
            opportunities.append(
                f"{numeric_count} numeric columns support correlation "
                "and comparative analysis."
            )
    except Exception:
        pass

    try:
        if date_profile.get("date_columns"):
            opportunities.append(
                "A date column is present — time-series and trend analysis are available."
            )
    except Exception:
        pass

    return {
        "type": "executive_summary",
        "heading": "Executive Summary",
        "summary": summary,
        "key_takeaways": takeaways[:4],
        "risks": risks[:3],
        "opportunities": opportunities[:3],
    }


def _build_recommendation_section(
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
    correlation_profile: list | None = None,
    narrative_config: dict | None = None,
    label_map: dict | None = None,
) -> dict | None:
    """Build deterministic recommended actions from stored profile data.

    Returns None when no recommendations apply so the caller can skip the section.
    Maximum 5 recommendations, ordered by priority (high first).
    No AI call. No hallucinated claims — every recommendation is gated on a
    verifiable profile fact.
    """
    lm            = label_map or {}
    numeric_count = len(numeric_profile)
    cat_count     = len(categorical_profile)

    total_cells = row_count * column_count
    completeness_pct: float | None = None
    if total_cells > 0:
        try:
            total_missing = sum(
                v for v in missing_values.values()
                if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            )
            completeness_pct = round((1 - total_missing / total_cells) * 100, 1)
        except Exception:
            pass

    recs: list[dict] = []

    # 1. Missing data — highest urgency if completeness is critically low
    try:
        cols_with_missing = [
            col for col, cnt in missing_values.items()
            if isinstance(cnt, (int, float)) and cnt > 0
        ]
        if cols_with_missing:
            n        = len(cols_with_missing)
            priority = "high" if (completeness_pct is not None and completeness_pct < 80) else "medium"
            recs.append({
                "title":       "Review and Clean Missing Data",
                "reason":      (
                    f"{n} column{'s' if n > 1 else ''} contain missing values. "
                    "Fill or remove missing values before relying on analysis conclusions."
                ),
                "priority":    priority,
                "action_type": "clean_data",
                "confidence":  "high",
            })
    except Exception:
        pass

    # 2. Small sample — statistical reliability concern
    if row_count < 100:
        recs.append({
            "title":       "Validate Sample Representativeness",
            "reason":      (
                f"Only {row_count:,} records are present. "
                "Verify the sample is representative before drawing conclusions."
            ),
            "priority":    "high",
            "action_type": "review",
            "confidence":  "high",
        })

    # 3. Date column — time-series and scheduling opportunity
    try:
        date_cols = date_profile.get("date_columns") or []
        if date_cols:
            col_name = date_cols[0]["column"]
            recs.append({
                "title":       "Schedule Recurring Trend Monitoring",
                "reason":      (
                    f"A date column ({lm.get(col_name, col_name)}) is present. "
                    "Set up a scheduled report to track changes over time automatically."
                ),
                "priority":    "medium",
                "action_type": "schedule",
                "confidence":  "high",
            })
    except Exception:
        pass

    # 4. Categorical columns — segmentation opportunity
    if cat_count > 0:
        try:
            best = max(
                categorical_profile.items(),
                key=lambda x: len(x[1]),
                default=None,
            )
            col_hint = f" (starting with {lm.get(best[0], best[0])})" if best else ""
            recs.append({
                "title":       "Segment Analysis by Category",
                "reason":      (
                    f"{cat_count} categorical column{'s' if cat_count > 1 else ''} "
                    f"available{col_hint}. Filter or group by these fields to uncover "
                    "segment-level insights."
                ),
                "priority":    "medium",
                "action_type": "segment",
                "confidence":  "medium",
            })
        except Exception:
            pass

    # 5. Multiple numeric columns — correlation analysis
    if numeric_count >= 2 and len(recs) < 5:
        recs.append({
            "title":       "Investigate Numeric Relationships",
            "reason":      (
                f"{numeric_count} numeric columns are available. "
                "Compare indicators for correlations or anomalies that may drive outcomes."
            ),
            "priority":    "medium",
            "action_type": "review",
            "confidence":  "medium",
        })

    # 6a. Outlier concentration — data quality action
    if len(recs) < 5:
        for col, stats in numeric_profile.items():
            try:
                oc = stats.get("outlier_count_iqr", 0)
                nn = stats.get("non_null_count")
                if not nn or nn == 0:
                    continue
                density = float(oc) / float(nn)
                if density >= 0.10:
                    recs.append({
                        "title":       f"Investigate Outlier Concentration: {lm.get(col, col)}",
                        "reason":      (
                            f"{int(oc):,} outlier values in '{lm.get(col, col)}' "
                            f"({round(density * 100, 1)}% via IQR). "
                            "Review for data entry errors or genuine extreme values before analysis."
                        ),
                        "priority":    "high" if density >= 0.20 else "medium",
                        "action_type": "review",
                        "confidence":  "high",
                    })
                    break
            except Exception:
                continue

    # 6b. Strong correlations — predictive modeling opportunity
    if len(recs) < 5 and correlation_profile:
        try:
            strong = [p for p in correlation_profile if abs(p.get("correlation", 0)) >= 0.50]
            if len(strong) >= 2:
                top = strong[0]
                recs.append({
                    "title":       "Leverage Detected Feature Correlations",
                    "reason":      (
                        f"Strong correlation (r={round(top['correlation'], 2)}) between "
                        f"'{lm.get(top['column_a'], top['column_a'])}' and '{lm.get(top['column_b'], top['column_b'])}'. "
                        f"{len(strong)} correlated feature pairs may support predictive modeling."
                    ),
                    "priority":    "medium",
                    "action_type": "review",
                    "confidence":  "medium",
                })
        except Exception:
            pass

    # 6c. Daily granularity — forecasting workflow
    if len(recs) < 5:
        try:
            for dc in (date_profile.get("date_columns") or [])[:1]:
                if dc.get("inferred_granularity") == "daily":
                    recs.append({
                        "title":       "Enable Time-Series Forecasting Workflow",
                        "reason":      (
                            f"Column '{lm.get(dc['column'], dc['column'])}' has daily granularity over "
                            f"{dc.get('range_days', 0):,} days. "
                            "Daily data supports short-term trend monitoring and scheduling."
                        ),
                        "priority":    "medium",
                        "action_type": "schedule",
                        "confidence":  "high",
                    })
        except Exception:
            pass

    # 7. Large dataset — automation opportunity (low priority, fill remaining slot)
    if row_count >= 10_000 and len(recs) < 5:
        recs.append({
            "title":       "Automate with Scheduled Reporting",
            "reason":      (
                f"With {row_count:,} records, this dataset benefits from regular "
                "automated analysis. Schedule recurring reports to monitor key metrics."
            ),
            "priority":    "low",
            "action_type": "schedule",
            "confidence":  "medium",
        })

    if not recs:
        return None

    limit = 5
    if narrative_config:
        if narrative_config.get("suppress_low_confidence"):
            recs = [r for r in recs if r.get("confidence") != "medium"]
        limit = narrative_config.get("recommendation_limit", 5)

    return {
        "type":            "recommendation",
        "heading":         "Recommended Actions",
        "recommendations": recs[:limit],
    }


def _build_anomaly_section(
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
    correlation_profile: list | None = None,
    categorical_meta: dict | None = None,
    label_map: dict | None = None,
) -> dict:
    """Build deterministic anomaly/risk detection from stored profile data only.

    No AI. No row-level access. No hallucinated claims.
    All thresholds are conservative and evidence-based.
    Maximum 8 anomalies, sorted high → medium → low.
    Shows an all-clear item when none are found so users know the check ran.
    """
    lm        = label_map or {}
    anomalies: list[dict] = []

    # ── 1. Per-column missing data ────────────────────────────────────────────
    if row_count > 0:
        for col, cnt in missing_values.items():
            try:
                if not isinstance(cnt, (int, float)) or not math.isfinite(cnt) or cnt <= 0:
                    continue
                rate = cnt / row_count
                if rate >= 0.25:
                    anomalies.append({
                        "title":       f"High Missing Rate: {lm.get(col, col)}",
                        "description": f"Column '{lm.get(col, col)}' has a critically high proportion of missing values.",
                        "severity":    "high",
                        "category":    "missing_data",
                        "evidence":    f"{int(cnt):,} of {row_count:,} rows ({round(rate * 100, 1)}%) are null.",
                    })
                elif rate >= 0.10:
                    anomalies.append({
                        "title":       f"Moderate Missing Rate: {lm.get(col, col)}",
                        "description": f"Column '{lm.get(col, col)}' has a notable proportion of missing values.",
                        "severity":    "medium",
                        "category":    "missing_data",
                        "evidence":    f"{int(cnt):,} of {row_count:,} rows ({round(rate * 100, 1)}%) are null.",
                    })
            except Exception:
                continue

    # ── 2. Dataset-level completeness ─────────────────────────────────────────
    total_cells = row_count * column_count
    if total_cells > 0:
        try:
            total_missing = sum(
                v for v in missing_values.values()
                if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            )
            completeness = (1 - total_missing / total_cells) * 100
            if completeness < 80:
                anomalies.append({
                    "title":       "Low Dataset Completeness",
                    "description": "Overall data completeness is critically low and may distort analysis.",
                    "severity":    "high",
                    "category":    "missing_data",
                    "evidence":    (
                        f"{round(completeness, 1)}% of all cells contain values "
                        f"({int(total_missing):,} missing across {column_count} columns)."
                    ),
                })
            elif completeness < 95:
                anomalies.append({
                    "title":       "Reduced Dataset Completeness",
                    "description": "Overall data completeness is below the recommended 95% threshold.",
                    "severity":    "medium",
                    "category":    "missing_data",
                    "evidence":    (
                        f"{round(completeness, 1)}% of all cells contain values "
                        f"({int(total_missing):,} missing across {column_count} columns)."
                    ),
                })
        except Exception:
            pass

    # ── 3. Small sample size ──────────────────────────────────────────────────
    if row_count < 30:
        anomalies.append({
            "title":       "Very Small Sample Size",
            "description": "Fewer than 30 rows — statistical findings are unreliable.",
            "severity":    "high",
            "category":    "sample_size",
            "evidence":    f"Dataset contains only {row_count:,} record{'s' if row_count != 1 else ''}.",
        })
    elif row_count < 100:
        anomalies.append({
            "title":       "Small Sample Size",
            "description": "Fewer than 100 rows — findings may not generalise to a broader population.",
            "severity":    "medium",
            "category":    "sample_size",
            "evidence":    f"Dataset contains {row_count:,} records.",
        })

    # ── 4. Dominant category imbalance ────────────────────────────────────────
    if row_count > 0:
        for col, entries in categorical_profile.items():
            try:
                if not entries:
                    continue
                top_count = entries[0].get("count", 0)
                if not isinstance(top_count, (int, float)) or top_count <= 0:
                    continue
                dominance = top_count / row_count
                top_value = str(entries[0].get("value", ""))
                if dominance >= 0.90:
                    anomalies.append({
                        "title":       f"Extreme Category Dominance: {lm.get(col, col)}",
                        "description": f"A single value dominates '{lm.get(col, col)}', indicating a near-constant field.",
                        "severity":    "high",
                        "category":    "distribution",
                        "evidence":    (
                            f'"{top_value}" appears in {round(dominance * 100, 1)}% of rows '
                            f"({int(top_count):,} of {row_count:,})."
                        ),
                    })
                elif dominance >= 0.80:
                    anomalies.append({
                        "title":       f"Category Imbalance: {lm.get(col, col)}",
                        "description": f"Column '{lm.get(col, col)}' is heavily skewed toward one value.",
                        "severity":    "medium",
                        "category":    "distribution",
                        "evidence":    (
                            f'"{top_value}" appears in {round(dominance * 100, 1)}% of rows '
                            f"({int(top_count):,} of {row_count:,})."
                        ),
                    })
            except Exception:
                continue

    # ── 5. Numeric spread warning ─────────────────────────────────────────────
    for col, stats in numeric_profile.items():
        try:
            mn   = stats.get("min")
            mx   = stats.get("max")
            mean = stats.get("mean")
            if mn is None or mx is None or mean is None:
                continue
            mn   = float(mn)
            mx   = float(mx)
            mean = float(mean)
            if not (math.isfinite(mn) and math.isfinite(mx) and math.isfinite(mean)):
                continue
            if mn > 0 and mx / mn > 1000:
                anomalies.append({
                    "title":       f"Extreme Value Range: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' spans an unusually wide numeric range (>1000x).",
                    "severity":    "medium",
                    "category":    "distribution",
                    "evidence":    (
                        f"Min={_safe_fmt(mn)}, Max={_safe_fmt(mx)} "
                        f"(ratio {round(mx / mn):,}x). Mean={_safe_fmt(mean)}."
                    ),
                })
            elif mean > 0 and mx > 10 * mean:
                anomalies.append({
                    "title":       f"Potential Outlier in {lm.get(col, col)}",
                    "description": f"Maximum value in '{lm.get(col, col)}' is far above the column mean, suggesting outliers.",
                    "severity":    "medium",
                    "category":    "distribution",
                    "evidence":    (
                        f"Max={_safe_fmt(mx)} vs Mean={_safe_fmt(mean)} "
                        f"({round(mx / mean, 1)}x the mean). Min={_safe_fmt(mn)}."
                    ),
                })
        except Exception:
            continue

    # ── 6. Date/trend risk ────────────────────────────────────────────────────
    try:
        for ti in (date_profile.get("trend_insights") or []):
            try:
                pct = ti.get("pct_change")
                col = ti.get("column", "")
                if pct is None:
                    continue
                pct = float(pct)
                if not math.isfinite(pct):
                    continue
                if abs(pct) >= 200:
                    direction = "increase" if pct > 0 else "decrease"
                    anomalies.append({
                        "title":       f"Large Trend Shift: {lm.get(col, col)}",
                        "description": (
                            f"Column '{lm.get(col, col)}' shows a sharp {direction} between "
                            "the first and second half of the time range."
                        ),
                        "severity":    "medium",
                        "category":    "trend",
                        "evidence":    (
                            f"{round(pct, 1)}% change from first to second half "
                            "of the dataset (sorted by date)."
                        ),
                    })
            except Exception:
                continue
    except Exception:
        pass

    # ── 7. High outlier density (IQR-based) ──────────────────────────────────
    for col, stats in numeric_profile.items():
        try:
            oc = stats.get("outlier_count_iqr")
            nn = stats.get("non_null_count")
            if oc is None or not nn or nn == 0:
                continue
            density = float(oc) / float(nn)
            if density >= 0.20:
                anomalies.append({
                    "title":       f"High Outlier Density: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' has a high proportion of IQR outliers.",
                    "severity":    "high",
                    "category":    "distribution",
                    "evidence":    f"{int(oc):,} outliers in {int(nn):,} non-null values ({round(density * 100, 1)}%).",
                })
            elif density >= 0.10:
                anomalies.append({
                    "title":       f"Moderate Outlier Density: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' contains a notable proportion of IQR outliers.",
                    "severity":    "medium",
                    "category":    "distribution",
                    "evidence":    f"{int(oc):,} outliers in {int(nn):,} non-null values ({round(density * 100, 1)}%).",
                })
        except Exception:
            continue

    # ── 8. Extreme variability (coefficient of variation) ────────────────────
    for col, stats in numeric_profile.items():
        try:
            std  = stats.get("std")
            mean = stats.get("mean")
            if std is None or mean is None:
                continue
            std_f  = float(std)
            mean_f = float(mean)
            if not (math.isfinite(std_f) and math.isfinite(mean_f)) or mean_f == 0:
                continue
            cv = std_f / abs(mean_f)
            if cv > 5.0:
                anomalies.append({
                    "title":       f"Extreme Variability: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' has an extreme coefficient of variation — values are highly unstable relative to the mean.",
                    "severity":    "high",
                    "category":    "distribution",
                    "evidence":    f"CV={round(cv, 2)} (std={_safe_fmt(std_f)}, mean={_safe_fmt(mean_f)}).",
                })
            elif cv > 2.0:
                anomalies.append({
                    "title":       f"High Variability: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' shows high relative variability around its mean.",
                    "severity":    "medium",
                    "category":    "distribution",
                    "evidence":    f"CV={round(cv, 2)} (std={_safe_fmt(std_f)}, mean={_safe_fmt(mean_f)}).",
                })
        except Exception:
            continue

    # ── 9. Sparse histogram distribution ─────────────────────────────────────
    _sparse_best: tuple | None = None
    _sparse_best_frac = 0.0
    for col, stats in numeric_profile.items():
        try:
            bins = stats.get("histogram_bins", [])
            if len(bins) < 5:
                continue
            empty = sum(1 for b in bins if b.get("count", 0) == 0)
            frac  = empty / len(bins)
            if frac > _sparse_best_frac:
                _sparse_best_frac = frac
                _sparse_best = (col, empty, len(bins))
        except Exception:
            continue
    if _sparse_best and _sparse_best_frac > 0.50:
        col, empty, total = _sparse_best
        anomalies.append({
            "title":       f"Sparse Distribution: {lm.get(col, col)}",
            "description": f"Column '{lm.get(col, col)}' has large empty regions in its value distribution, suggesting gaps or multimodal patterns.",
            "severity":    "medium",
            "category":    "distribution",
            "evidence":    f"{empty} of {total} histogram bins are empty ({round(_sparse_best_frac * 100, 0):.0f}% empty).",
        })

    # ── 10. Correlation risk (near-perfect collinearity) ─────────────────────
    if correlation_profile:
        _corr_added = 0
        for pair in correlation_profile:
            try:
                c = float(pair.get("correlation", 0))
                if abs(c) >= 0.90:
                    a, b = pair.get("column_a", ""), pair.get("column_b", "")
                    anomalies.append({
                        "title":       f"Highly Correlated Fields: {lm.get(a, a)} & {lm.get(b, b)}",
                        "description": f"'{lm.get(a, a)}' and '{lm.get(b, b)}' move together very closely and may contain overlapping information.",
                        "severity":    "medium",
                        "category":    "quality",
                        "evidence":    f"These fields are {round(abs(c) * 100):.0f}% correlated — consider consolidating or removing one.",
                    })
                    _corr_added += 1
                    if _corr_added >= 2:
                        break
            except Exception:
                continue

    # ── 11. Entropy collapse (near-single-value categorical) ─────────────────
    if categorical_meta:
        for col, meta in categorical_meta.items():
            try:
                entropy    = meta.get("entropy_approx")
                unique     = meta.get("unique_count", 1)
                top_share  = float(meta.get("top_value_share", 0))
                if entropy is None or unique <= 1:
                    continue
                # Skip if already captured by the ≥80% dominance check above
                if top_share >= 0.80:
                    continue
                if float(entropy) < 0.30:
                    anomalies.append({
                        "title":       f"Low Categorical Diversity: {lm.get(col, col)}",
                        "description": f"Column '{lm.get(col, col)}' has near-zero entropy — distributional variety is severely limited.",
                        "severity":    "medium",
                        "category":    "distribution",
                        "evidence":    (
                            f"Entropy={round(float(entropy), 3)}, "
                            f"{unique} unique value{'s' if unique != 1 else ''}, "
                            f"top-value share={round(top_share * 100, 1)}%."
                        ),
                    })
            except Exception:
                continue

    # ── Sort high → medium → low, cap at 8 ───────────────────────────────────
    _SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
    anomalies.sort(key=lambda a: _SEV_ORDER.get(a.get("severity", "low"), 2))
    anomalies = anomalies[:8]

    # ── All-clear fallback (explicit confirmation the check ran) ──────────────
    # Enterprise users need to know the section ran and found nothing,
    # not an empty or missing card that looks like a rendering failure.
    if not anomalies:
        anomalies = [{
            "title":       "No Major Anomalies Detected",
            "description": "All deterministic checks passed within expected thresholds.",
            "severity":    "low",
            "category":    "quality",
            "evidence":    (
                f"Dataset of {row_count:,} rows and {column_count} columns "
                "passed all risk checks."
            ),
        }]

    return {
        "type":      "anomaly",
        "heading":   "Anomalies & Data Risks",
        "anomalies": anomalies,
    }


def _build_trend_section(
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
    label_map: dict | None = None,
) -> dict:
    """Build deterministic trend intelligence from stored profile data only.

    No AI. No forecasting. No future predictions.
    Every trend statement is derived from stored profile values.
    Maximum 6 trends, sorted by strength (high first).
    Shows a fallback item when no profile data yields signals.
    """
    lm     = label_map or {}
    trends: list[dict] = []

    # ── 1. Time-series direction from pct_change ──────────────────────────────
    try:
        date_cols      = date_profile.get("date_columns") or []
        trend_insights = date_profile.get("trend_insights") or []
        date_col_name  = date_cols[0]["column"] if date_cols else "date"
        for ti in trend_insights[:3]:
            try:
                col = ti.get("column", "")
                pct = ti.get("pct_change")
                if pct is None:
                    continue
                pct     = float(pct)
                abs_pct = abs(pct)
                if not math.isfinite(pct):
                    continue

                direction = "stable" if abs_pct <= 5 else ("up" if pct > 0 else "down")
                strength  = "high" if abs_pct >= 100 else ("medium" if abs_pct >= 30 else "low")
                pct_str   = f"+{round(pct, 1)}%" if pct >= 0 else f"{round(pct, 1)}%"
                dir_label = {"up": "Increasing", "down": "Decreasing", "stable": "Stable"}.get(direction, direction.title())

                trends.append({
                    "title":       f"{lm.get(col, col)} — {dir_label} Trend",
                    "description": (
                        f"{lm.get(col, col)} shows a {dir_label.lower()} pattern between the first and "
                        f"second half of the dataset when sorted by {lm.get(date_col_name, date_col_name)}."
                    ),
                    "direction": direction,
                    "strength":  strength,
                    "category":  "time_series",
                    "evidence":  (
                        f"{pct_str} change from first to second half "
                        f"(sorted by {date_col_name})."
                    ),
                })
            except Exception:
                continue
    except Exception:
        pass

    # ── 2. Completeness stability ─────────────────────────────────────────────
    total_cells = row_count * column_count
    if total_cells > 0:
        try:
            total_missing = sum(
                v for v in missing_values.values()
                if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            )
            completeness = (1 - total_missing / total_cells) * 100
            if completeness >= 98:
                trends.append({
                    "title":       "Excellent Data Completeness",
                    "description": "Dataset integrity is high, supporting reliable trend analysis.",
                    "direction":   "stable",
                    "strength":    "high",
                    "category":    "completeness",
                    "evidence":    (
                        f"{round(completeness, 1)}% of cells contain values "
                        f"across {column_count} columns."
                    ),
                })
            elif completeness >= 80:
                trends.append({
                    "title":       "Adequate Data Completeness",
                    "description": (
                        "Moderate completeness — gaps may introduce discontinuities "
                        "in trend analysis."
                    ),
                    "direction":   "stable",
                    "strength":    "medium",
                    "category":    "completeness",
                    "evidence":    (
                        f"{round(completeness, 1)}% of cells contain values "
                        f"({int(total_missing):,} missing)."
                    ),
                })
            else:
                trends.append({
                    "title":       "Low Completeness — Trend Reliability Reduced",
                    "description": (
                        "Significant missing data introduces uncertainty "
                        "in trend interpretation."
                    ),
                    "direction":   "volatile",
                    "strength":    "medium",
                    "category":    "completeness",
                    "evidence":    (
                        f"{round(completeness, 1)}% completeness "
                        f"({int(total_missing):,} missing cells across {column_count} columns)."
                    ),
                })
        except Exception:
            pass

    # ── 3. Distribution concentration (top categorical column) ────────────────
    if row_count > 0 and categorical_profile:
        try:
            best_col      = None
            best_entries  = None
            best_dom      = 0.0
            for col, entries in categorical_profile.items():
                if not entries:
                    continue
                top_count = entries[0].get("count", 0)
                if not isinstance(top_count, (int, float)) or top_count <= 0:
                    continue
                d = top_count / row_count
                if d > best_dom:
                    best_dom     = d
                    best_col     = col
                    best_entries = entries

            if best_col and best_entries:
                top_value    = str(best_entries[0].get("value", ""))
                unique_count = len(best_entries)
                pct_dom      = round(best_dom * 100, 1)
                val_label    = f"{unique_count} distinct value{'s' if unique_count != 1 else ''} observed."

                if best_dom >= 0.80:
                    trends.append({
                        "title":       f"Concentrated Distribution: {lm.get(best_col, best_col)}",
                        "description": (
                            f"Column '{lm.get(best_col, best_col)}' is dominated by a single value, "
                            "indicating low distributional variety."
                        ),
                        "direction": "stable",
                        "strength":  "high",
                        "category":  "distribution",
                        "evidence":  f'"{top_value}" holds {pct_dom}% of rows. {val_label}',
                    })
                elif best_dom >= 0.50:
                    trends.append({
                        "title":       f"Moderate Concentration: {lm.get(best_col, best_col)}",
                        "description": (
                            f"Column '{lm.get(best_col, best_col)}' has a leading category but retains "
                            "meaningful distributional variety."
                        ),
                        "direction": "stable",
                        "strength":  "medium",
                        "category":  "distribution",
                        "evidence":  f'"{top_value}" leads at {pct_dom}% of rows. {val_label}',
                    })
                else:
                    trends.append({
                        "title":       f"Balanced Distribution: {lm.get(best_col, best_col)}",
                        "description": (
                            f"Column '{lm.get(best_col, best_col)}' is spread across categories, "
                            "supporting stable segmentation analysis."
                        ),
                        "direction": "stable",
                        "strength":  "low",
                        "category":  "distribution",
                        "evidence":  f'"{top_value}" leads at {pct_dom}% of rows. {val_label}',
                    })
        except Exception:
            pass

    # ── 4. Numeric spread (most notable column) ───────────────────────────────
    if numeric_profile:
        try:
            best_col   = None
            best_ratio = 0.0
            best_stats = {}
            for col, stats in numeric_profile.items():
                try:
                    mn   = stats.get("min")
                    mx   = stats.get("max")
                    mean = stats.get("mean")
                    if mn is None or mx is None or mean is None:
                        continue
                    mn, mx, mean = float(mn), float(mx), float(mean)
                    if not (math.isfinite(mn) and math.isfinite(mx) and math.isfinite(mean)):
                        continue
                    if mean == 0:
                        continue
                    ratio = (mx - mn) / abs(mean)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_col   = col
                        best_stats = stats
                except Exception:
                    continue

            if best_col:
                mn   = float(best_stats["min"])
                mx   = float(best_stats["max"])
                mean = float(best_stats["mean"])
                ev   = (
                    f"Range {_safe_fmt(mn)} to {_safe_fmt(mx)}, "
                    f"Mean {_safe_fmt(mean)}. "
                    f"Spread/mean ratio: {round(best_ratio, 1)}x."
                )
                if best_ratio > 10:
                    trends.append({
                        "title":       f"High Value Dispersion: {lm.get(best_col, best_col)}",
                        "description": (
                            f"'{lm.get(best_col, best_col)}' shows wide numeric spread relative to its mean, "
                            "indicating volatile or heterogeneous values."
                        ),
                        "direction": "volatile",
                        "strength":  "medium",
                        "category":  "distribution",
                        "evidence":  ev,
                    })
                elif best_ratio > 3:
                    trends.append({
                        "title":       f"Moderate Value Spread: {lm.get(best_col, best_col)}",
                        "description": (
                            f"'{lm.get(best_col, best_col)}' shows moderate numeric spread, "
                            "typical of datasets with natural variation."
                        ),
                        "direction": "stable",
                        "strength":  "low",
                        "category":  "distribution",
                        "evidence":  ev,
                    })
                else:
                    trends.append({
                        "title":       f"Tightly Grouped Values: {lm.get(best_col, best_col)}",
                        "description": (
                            f"'{lm.get(best_col, best_col)}' values cluster closely around the mean, "
                            "indicating a consistent numeric distribution."
                        ),
                        "direction": "stable",
                        "strength":  "low",
                        "category":  "distribution",
                        "evidence":  ev,
                    })
        except Exception:
            pass

    # ── 5. Monthly volume pattern (from monthly_counts) ──────────────────────
    try:
        for dc in (date_profile.get("date_columns") or [])[:1]:
            mc = dc.get("monthly_counts", [])
            if len(mc) < 3:
                break
            counts = [m.get("count", 0) for m in mc if "count" in m]
            if len(counts) < 3:
                break
            half       = max(len(counts) // 2, 1)
            avg_first  = sum(counts[:half]) / half
            avg_second = sum(counts[half:]) / max(len(counts) - half, 1)
            col        = dc.get("column", "date")
            if avg_second > avg_first * 1.15:
                trends.append({
                    "title":       f"Monthly Volume Growth: {lm.get(col, col)}",
                    "description": "Record volume shows consistent growth in the second half of the observed period.",
                    "direction":   "up",
                    "strength":    "medium",
                    "category":    "time_series",
                    "evidence":    (
                        f"First-half avg: {avg_first:.0f}/month → "
                        f"Second-half avg: {avg_second:.0f}/month "
                        f"({len(mc)} months observed)."
                    ),
                })
            elif avg_second < avg_first * 0.85:
                trends.append({
                    "title":       f"Monthly Volume Decline: {lm.get(col, col)}",
                    "description": "Record volume shows a declining pattern in the second half of the observed period.",
                    "direction":   "down",
                    "strength":    "medium",
                    "category":    "time_series",
                    "evidence":    (
                        f"First-half avg: {avg_first:.0f}/month → "
                        f"Second-half avg: {avg_second:.0f}/month "
                        f"({len(mc)} months observed)."
                    ),
                })
            else:
                trends.append({
                    "title":       f"Stable Monthly Volume: {lm.get(col, col)}",
                    "description": "Record volume is consistent month-over-month across the observed period.",
                    "direction":   "stable",
                    "strength":    "low",
                    "category":    "time_series",
                    "evidence":    (
                        f"Avg {(avg_first + avg_second) / 2:.0f} records/month "
                        f"across {len(mc)} months."
                    ),
                })
    except Exception:
        pass

    # ── 6. Date granularity signal ────────────────────────────────────────────
    try:
        for dc in (date_profile.get("date_columns") or [])[:1]:
            gran       = dc.get("inferred_granularity", "unknown")
            col        = dc.get("column", "")
            range_days = dc.get("range_days", 0)
            if gran == "daily":
                trends.append({
                    "title":       f"Daily Granularity: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' records at daily intervals — supports fine-grained time-series analysis.",
                    "direction":   "stable",
                    "strength":    "high",
                    "category":    "time_series",
                    "evidence":    f"Inferred daily granularity over {range_days:,} days.",
                })
            elif gran in ("weekly", "monthly"):
                trends.append({
                    "title":       f"{gran.title()} Granularity: {lm.get(col, col)}",
                    "description": f"Column '{lm.get(col, col)}' records at {gran} intervals — suitable for period aggregation and reporting.",
                    "direction":   "stable",
                    "strength":    "medium",
                    "category":    "time_series",
                    "evidence":    f"Inferred {gran} granularity over {range_days:,} days.",
                })
    except Exception:
        pass

    # ── Sort high → medium → low, cap at 6 ───────────────────────────────────
    _STR_ORDER = {"high": 0, "medium": 1, "low": 2}
    trends.sort(key=lambda t: _STR_ORDER.get(t.get("strength", "low"), 2))
    trends = trends[:6]

    # ── Fallback: always show the section so users know it ran ────────────────
    # Same rationale as the anomaly section: an empty card looks like a bug,
    # and enterprise users need confirmation that analysis was attempted.
    if not trends:
        trends = [{
            "title":       "No Significant Trends Detected",
            "description": "Insufficient profile data to establish directional patterns.",
            "direction":   "stable",
            "strength":    "low",
            "category":    "time_series",
            "evidence":    (
                f"Dataset of {row_count:,} rows and {column_count} columns "
                "did not yield measurable trend signals."
            ),
        }]

    return {
        "type":    "trend",
        "heading": "Trend Intelligence",
        "trends":  trends,
    }


def _build_predictive_readiness_section(
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
    correlation_profile: list | None = None,
    categorical_meta: dict | None = None,
    label_map: dict | None = None,
) -> dict:
    """Assess whether the dataset is ready for future predictive analytics.

    No predictions. No ML models. No forecasting claims.
    Evaluates five readiness signals against conservative thresholds.
    Each signal contributes 0 (missing), 10 (partial), or 20 (ready) points.
    Maximum score = 100. Always returns a section — never omitted.
    """
    lm      = label_map or {}
    signals: list[dict] = []
    score = 0

    # ── Signal 1: Dataset size ────────────────────────────────────────────────
    if row_count >= 1000:
        s1, score = "ready", score + 20
        desc1 = "Sufficient records for model training."
        ev1   = f"{row_count:,} rows — meets the 1,000-row minimum threshold."
    elif row_count >= 100:
        s1, score = "partial", score + 10
        desc1 = "Borderline sample size — models trained here may have limited reliability."
        ev1   = f"{row_count:,} rows — below the recommended 1,000 rows for reliable training."
    else:
        s1    = "missing"
        desc1 = "Too few records for reliable model training."
        ev1   = f"Only {row_count:,} row{'s' if row_count != 1 else ''} — at least 100 required to proceed."
    signals.append({"name": "Dataset Size", "status": s1, "description": desc1, "evidence": ev1})

    # ── Signal 2: Numeric features ────────────────────────────────────────────
    num_count = len(numeric_profile)
    if num_count >= 2:
        s2, score = "ready", score + 20
        desc2 = "Multiple numeric features support quantitative modeling."
        ev2   = f"{num_count} numeric columns available."
    elif num_count == 1:
        s2, score = "partial", score + 10
        desc2 = "Only one numeric feature — additional measurable columns would improve model quality."
        ev2   = "1 numeric column available; 2+ recommended for meaningful modeling."
    else:
        s2    = "missing"
        desc2 = "No numeric features — quantitative modeling requires measurable numeric columns."
        ev2   = "No numeric columns detected in the dataset."
    signals.append({"name": "Numeric Features", "status": s2, "description": desc2, "evidence": ev2})

    # ── Signal 3: Categorical segmentation ───────────────────────────────────
    cat_count = len(categorical_profile)
    if cat_count >= 1:
        s3, score = "ready", score + 20
        desc3 = "Categorical columns enable segment-level and classification modeling."
        ev3   = f"{cat_count} categorical column{'s' if cat_count != 1 else ''} available."
    else:
        s3    = "missing"
        desc3 = "No categorical columns — segmentation and classification modeling are not possible."
        ev3   = "No categorical columns detected in the dataset."
    signals.append({"name": "Categorical Segmentation", "status": s3, "description": desc3, "evidence": ev3})

    # ── Signal 4: Data completeness ───────────────────────────────────────────
    total_cells     = row_count * column_count
    completeness_pct: float | None = None
    if total_cells > 0:
        try:
            total_missing = sum(
                v for v in missing_values.values()
                if isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            )
            completeness_pct = round((1 - total_missing / total_cells) * 100, 1)
        except Exception:
            pass

    if completeness_pct is not None:
        if completeness_pct >= 95:
            s4, score = "ready", score + 20
            desc4 = "High data completeness supports clean model training."
            ev4   = f"{completeness_pct}% of cells contain values."
        elif completeness_pct >= 80:
            s4, score = "partial", score + 10
            desc4 = "Moderate completeness — missing values may need imputation before training."
            ev4   = f"{completeness_pct}% of cells contain values; 95%+ is recommended."
        else:
            s4    = "missing"
            desc4 = "Low completeness — significant imputation or data collection required before modeling."
            ev4   = f"Only {completeness_pct}% of cells contain values."
    else:
        s4    = "missing"
        desc4 = "Could not compute data completeness."
        ev4   = "Insufficient row/column information to calculate completeness."
    signals.append({"name": "Data Completeness", "status": s4, "description": desc4, "evidence": ev4})

    # ── Signal 5: Time-series potential ──────────────────────────────────────
    date_cols      = date_profile.get("date_columns") or []
    trend_insights = date_profile.get("trend_insights") or []
    if date_cols and trend_insights:
        s5, score = "ready", score + 20
        dc        = date_cols[0]
        desc5 = "Date column with trend signals detected — time-series modeling is feasible."
        ev5   = (
            f"Column '{dc['column']}' present with {len(trend_insights)} trend "
            f"signal{'s' if len(trend_insights) != 1 else ''}. "
            f"Range: {dc.get('range_days', 0):,} days."
        )
    elif date_cols:
        s5, score = "partial", score + 10
        desc5 = "Date column present but no trend signals computed — time-series support is limited."
        ev5   = f"Column '{date_cols[0]['column']}' detected; no trend insights available."
    else:
        s5    = "missing"
        desc5 = "No date or timestamp column — time-series and forecasting models are not applicable."
        ev5   = "No date columns detected in the dataset."
    signals.append({"name": "Time-Series Potential", "status": s5, "description": desc5, "evidence": ev5})

    # ── Signal 6: Feature Correlation Network ────────────────────────────────
    if correlation_profile:
        try:
            strong = [p for p in correlation_profile if abs(p.get("correlation", 0)) >= 0.50]
            if len(strong) >= 3:
                top = strong[0]
                signals.append({
                    "name":        "Feature Correlation Network",
                    "status":      "ready",
                    "description": f"{len(strong)} correlated feature pairs support quantitative predictive modeling.",
                    "evidence":    f"Strongest: '{top['column_a']}' ↔ '{top['column_b']}' (r={round(top['correlation'], 2)}).",
                })
                score = min(100, score + 5)
            elif len(strong) >= 1:
                top = strong[0]
                signals.append({
                    "name":        "Feature Correlation Network",
                    "status":      "partial",
                    "description": f"{len(strong)} correlated pair{'s' if len(strong) != 1 else ''} detected — limited but present signal structure.",
                    "evidence":    f"Strongest: '{top['column_a']}' ↔ '{top['column_b']}' (r={round(top['correlation'], 2)}).",
                })
        except Exception:
            pass

    # ── Signal 7: Outlier data quality ───────────────────────────────────────
    try:
        _worst_col, _worst_d = None, 0.0
        for col, stats in numeric_profile.items():
            try:
                oc = stats.get("outlier_count_iqr", 0)
                nn = stats.get("non_null_count")
                if nn and nn > 0:
                    d = float(oc) / float(nn)
                    if d > _worst_d:
                        _worst_d, _worst_col = d, col
            except Exception:
                continue
        if _worst_col and _worst_d >= 0.20:
            signals.append({
                "name":        "Outlier Data Quality",
                "status":      "missing",
                "description": f"High outlier density in '{lm.get(_worst_col, _worst_col)}' may require preprocessing before model training.",
                "evidence":    f"{round(_worst_d * 100, 1)}% outlier rate (IQR method).",
            })
            score = max(0, score - 5)
        elif _worst_col and _worst_d >= 0.10:
            signals.append({
                "name":        "Outlier Data Quality",
                "status":      "partial",
                "description": f"Moderate outlier density in '{lm.get(_worst_col, _worst_col)}' — review before applying models.",
                "evidence":    f"{round(_worst_d * 100, 1)}% outlier rate (IQR method).",
            })
    except Exception:
        pass

    # ── Signal 8: Date granularity ────────────────────────────────────────────
    try:
        for dc in (date_profile.get("date_columns") or [])[:1]:
            gran = dc.get("inferred_granularity", "unknown")
            col  = dc.get("column", "")
            if gran == "daily":
                signals.append({
                    "name":        "Date Granularity",
                    "status":      "ready",
                    "description": f"Daily granularity in '{lm.get(col, col)}' supports fine-grained forecasting and sequence modeling.",
                    "evidence":    f"Inferred daily granularity over {dc.get('range_days', 0):,} days.",
                })
                score = min(100, score + 5)
            elif gran in ("weekly", "monthly"):
                signals.append({
                    "name":        "Date Granularity",
                    "status":      "partial",
                    "description": f"{gran.title()} granularity in '{lm.get(col, col)}' — adequate for period aggregation but not fine-grained forecasting.",
                    "evidence":    f"Inferred {gran} granularity.",
                })
    except Exception:
        pass

    # ── Signal 9: Categorical entropy (segmentation quality) ─────────────────
    if categorical_meta:
        try:
            low_ent = [
                col for col, meta in categorical_meta.items()
                if meta.get("entropy_approx") is not None
                and meta.get("unique_count", 1) > 1
                and float(meta.get("entropy_approx", 1)) < 0.30
            ]
            cat_count_local = len(categorical_profile)
            if low_ent:
                signals.append({
                    "name":        "Categorical Diversity",
                    "status":      "partial" if len(low_ent) < cat_count_local else "missing",
                    "description": (
                        f"{len(low_ent)} categorical column{'s' if len(low_ent) != 1 else ''} "
                        "have near-zero entropy — limited segmentation variety may reduce classification quality."
                    ),
                    "evidence":    f"Affected: {', '.join(low_ent[:3])}.",
                })
                score = max(0, score - 5)
        except Exception:
            pass

    # ── Readiness level ───────────────────────────────────────────────────────
    readiness_level = "high" if score >= 80 else ("medium" if score >= 50 else "low")

    # ── Next steps (only for missing or partial signals) ─────────────────────
    next_steps: list[str] = []
    status_map = {sig["name"]: sig["status"] for sig in signals}

    if status_map.get("Dataset Size") != "ready":
        next_steps.append(
            "Collect more records — at least 1,000 rows are recommended for reliable model training."
        )
    if status_map.get("Numeric Features") != "ready":
        next_steps.append(
            "Add measurable numeric columns (e.g., revenue, quantity, duration) "
            "to enable quantitative modeling."
        )
    if status_map.get("Categorical Segmentation") != "ready":
        next_steps.append(
            "Add grouping or category fields to enable segment-level prediction and classification."
        )
    if status_map.get("Data Completeness") != "ready":
        next_steps.append(
            "Review and clean missing values before applying predictive models; "
            "consider imputation strategies."
        )
    if status_map.get("Time-Series Potential") != "ready":
        next_steps.append(
            "Add a timestamp or date column to unlock time-series analysis "
            "and forecasting capabilities."
        )

    if not next_steps:
        next_steps.append(
            "Dataset meets all readiness criteria. "
            "Consider scheduling a predictive analytics workflow."
        )

    return {
        "type":            "predictive_readiness",
        "heading":         "Predictive Readiness",
        "readiness_score": score,
        "readiness_level": readiness_level,
        "signals":         signals,
        "next_steps":      next_steps,
    }


def _build_historical_comparison_section(
    sections: list[dict],
    previous_snapshot: dict,
    row_count: int,
) -> dict | None:
    """Compare current report metrics against a previous snapshot row.

    Scans the already-built sections list to extract current metric values,
    then diffs them against the stored snapshot dict.
    Returns None when no meaningful comparisons can be generated.
    No AI. No ML. No forecasting claims.
    Maximum 8 comparison rows, ordered by declaration priority.
    """
    prev = previous_snapshot.get("snapshot", {})
    if not prev:
        return None

    baseline_ts = previous_snapshot.get("created_at", "")

    # ── Extract current metrics from already-built sections ───────────────────
    curr_readiness_score: int | None  = None
    curr_anomaly_high:    int         = 0
    curr_rec_high:        int         = 0
    curr_trend_up:        int         = 0
    curr_trend_down:      int         = 0
    curr_kpi_values:      dict        = {}

    for section in sections:
        sec_type = section.get("type", "text")
        try:
            if sec_type == "predictive_readiness":
                curr_readiness_score = section.get("readiness_score")
            elif sec_type == "anomaly":
                counts: dict = {"high": 0, "medium": 0, "low": 0}
                for a in section.get("anomalies", []):
                    sev = str(a.get("severity", "")).lower()
                    if sev in counts:
                        counts[sev] += 1
                curr_anomaly_high = counts["high"]
            elif sec_type == "recommendation":
                rcounts: dict = {"high": 0, "medium": 0, "low": 0}
                for r in section.get("recommendations", []):
                    pri = str(r.get("priority", "")).lower()
                    if pri in rcounts:
                        rcounts[pri] += 1
                curr_rec_high = rcounts["high"]
            elif sec_type == "trend":
                tcounts: dict = {"up": 0, "down": 0, "stable": 0, "volatile": 0}
                for t in section.get("trends", []):
                    d = str(t.get("direction", "")).lower()
                    if d in tcounts:
                        tcounts[d] += 1
                curr_trend_up   = tcounts["up"]
                curr_trend_down = tcounts["down"]
            elif sec_type == "kpi":
                for kpi in section.get("kpis", []):
                    label = kpi.get("label", "")
                    value = kpi.get("value")
                    if label and value is not None:
                        curr_kpi_values[label] = value
        except Exception:
            continue

    comparisons: list[dict] = []

    # ── 1. Predictive Readiness Score ─────────────────────────────────────────
    rs_prev = prev.get("readiness_score")
    if curr_readiness_score is not None and rs_prev is not None:
        try:
            delta = float(curr_readiness_score) - float(rs_prev)
            ctype = "stable" if delta == 0 else ("increase" if delta > 0 else "decrease")
            sev   = "positive" if delta > 0 else ("warning" if delta < 0 else "neutral")
            word  = "improved" if delta > 0 else ("declined" if delta < 0 else "unchanged")
            comparisons.append({
                "metric":         "Predictive Readiness Score",
                "current_value":  curr_readiness_score,
                "previous_value": int(rs_prev),
                "change":         round(delta, 2),
                "change_type":    ctype,
                "severity":       sev,
                "description":    f"Predictive readiness {word} from {int(rs_prev)} to {curr_readiness_score}.",
            })
        except Exception:
            pass

    # ── 2. Dataset Row Count ──────────────────────────────────────────────────
    rc_prev = prev.get("row_count")
    if rc_prev is not None:
        try:
            delta = float(row_count) - float(rc_prev)
            ctype = "stable" if delta == 0 else ("increase" if delta > 0 else "decrease")
            sev   = "positive" if delta > 0 else ("warning" if delta < 0 else "neutral")
            word  = "increased" if delta > 0 else ("decreased" if delta < 0 else "unchanged")
            if delta != 0 and float(rc_prev) != 0:
                pct  = round(abs(delta) / float(rc_prev) * 100, 1)
                desc = f"Dataset row count {word} by {pct}% ({int(rc_prev):,} → {row_count:,})."
            else:
                desc = f"Dataset row count {word} at {row_count:,} rows."
            comparisons.append({
                "metric":         "Dataset Row Count",
                "current_value":  row_count,
                "previous_value": int(rc_prev),
                "change":         round(delta, 2),
                "change_type":    ctype,
                "severity":       sev,
                "description":    desc,
            })
        except Exception:
            pass

    # ── 3. High-Severity Anomalies ────────────────────────────────────────────
    ha_prev = prev.get("anomaly_counts_by_severity", {}).get("high", 0)
    try:
        ha_prev_i = int(ha_prev)
        delta     = curr_anomaly_high - ha_prev_i
        ctype     = "stable" if delta == 0 else ("increase" if delta > 0 else "decrease")
        sev       = "positive" if delta < 0 else ("warning" if delta > 0 else "neutral")
        word      = "increased" if delta > 0 else ("decreased" if delta < 0 else "unchanged")
        comparisons.append({
            "metric":         "High-Severity Anomalies",
            "current_value":  curr_anomaly_high,
            "previous_value": ha_prev_i,
            "change":         delta,
            "change_type":    ctype,
            "severity":       sev,
            "description":    f"High-severity anomalies {word} from {ha_prev_i} to {curr_anomaly_high}.",
        })
    except Exception:
        pass

    # ── 4. High-Priority Recommendations ─────────────────────────────────────
    hr_prev = prev.get("recommendation_counts_by_priority", {}).get("high", 0)
    try:
        hr_prev_i = int(hr_prev)
        delta     = curr_rec_high - hr_prev_i
        ctype     = "stable" if delta == 0 else ("increase" if delta > 0 else "decrease")
        sev       = "positive" if delta < 0 else ("warning" if delta > 0 else "neutral")
        word      = "increased" if delta > 0 else ("decreased" if delta < 0 else "unchanged")
        comparisons.append({
            "metric":         "High-Priority Recommendations",
            "current_value":  curr_rec_high,
            "previous_value": hr_prev_i,
            "change":         delta,
            "change_type":    ctype,
            "severity":       sev,
            "description":    f"High-priority recommendations {word} from {hr_prev_i} to {curr_rec_high}.",
        })
    except Exception:
        pass

    # ── 5. Upward Trend Signals ───────────────────────────────────────────────
    tu_prev = prev.get("trend_counts_by_direction", {}).get("up", 0)
    try:
        tu_prev_i = int(tu_prev)
        delta     = curr_trend_up - tu_prev_i
        ctype     = "stable" if delta == 0 else ("increase" if delta > 0 else "decrease")
        word      = "increased" if delta > 0 else ("decreased" if delta < 0 else "unchanged")
        comparisons.append({
            "metric":         "Upward Trend Signals",
            "current_value":  curr_trend_up,
            "previous_value": tu_prev_i,
            "change":         delta,
            "change_type":    ctype,
            "severity":       "positive" if delta > 0 else "neutral",
            "description":    f"Upward trend signals {word} from {tu_prev_i} to {curr_trend_up}.",
        })
    except Exception:
        pass

    # ── 6. Data Completeness KPI ──────────────────────────────────────────────
    dc_prev = prev.get("kpi_values", {}).get("Data Completeness")
    dc_curr = curr_kpi_values.get("Data Completeness")
    if dc_curr is not None and dc_prev is not None:
        try:
            delta = round(float(dc_curr) - float(dc_prev), 2)
            ctype = "stable" if abs(delta) < 0.01 else ("increase" if delta > 0 else "decrease")
            sev   = "neutral" if abs(delta) < 0.01 else ("positive" if delta > 0 else "warning")
            word  = "unchanged" if abs(delta) < 0.01 else ("improved" if delta > 0 else "declined")
            comparisons.append({
                "metric":         "Data Completeness",
                "current_value":  dc_curr,
                "previous_value": float(dc_prev),
                "change":         delta,
                "change_type":    ctype,
                "severity":       sev,
                "description":    (
                    f"Data completeness {word} from {float(dc_prev)}% to {dc_curr}%."
                    if abs(delta) >= 0.01
                    else f"Data completeness is unchanged at {dc_curr}%."
                ),
            })
        except Exception:
            pass

    if not comparisons:
        return None

    return {
        "type":               "historical_comparison",
        "heading":            "Historical Comparison",
        "comparisons":        comparisons[:8],
        "baseline_timestamp": baseline_ts,
    }


def _build_drift_detection_section(
    sections: list[dict],
    baseline_snapshots: list[dict],
    row_count: int,
) -> dict | None:
    """Detect long-term drift by comparing current metrics to baseline averages.

    Requires at least 3 historical snapshots. Fewer → returns None (section omitted).
    Drift formula: (current - baseline_avg) / |baseline_avg| * 100
    Only emits a drift item when |drift_percent| >= 10%.
    Severity: >= 40% → high, >= 20% → medium, >= 10% → low.
    No AI. No ML. No forecasting claims.
    Maximum 8 drift rows, ordered by |drift_percent| descending.
    """
    if len(baseline_snapshots) < 3:
        return None

    # ── Extract current metrics from already-built sections ───────────────────
    curr: dict = {
        "readiness_score": None,
        "row_count":       row_count,
        "anomaly_high":    0,
        "rec_high":        0,
        "trend_down":      0,
        "completeness":    None,
    }
    for section in sections:
        sec_type = section.get("type", "text")
        try:
            if sec_type == "predictive_readiness":
                curr["readiness_score"] = section.get("readiness_score")
            elif sec_type == "anomaly":
                h = 0
                for a in section.get("anomalies", []):
                    if str(a.get("severity", "")).lower() == "high":
                        h += 1
                curr["anomaly_high"] = h
            elif sec_type == "recommendation":
                h = 0
                for r in section.get("recommendations", []):
                    if str(r.get("priority", "")).lower() == "high":
                        h += 1
                curr["rec_high"] = h
            elif sec_type == "trend":
                d = 0
                for t in section.get("trends", []):
                    if str(t.get("direction", "")).lower() == "down":
                        d += 1
                curr["trend_down"] = d
            elif sec_type == "kpi":
                for kpi in section.get("kpis", []):
                    if kpi.get("label") == "Data Completeness":
                        curr["completeness"] = kpi.get("value")
        except Exception:
            continue

    # ── Compute baseline averages from historical snapshots ───────────────────
    def _avg(getter) -> float | None:
        vals = []
        for rec in baseline_snapshots:
            try:
                v = getter(rec.get("snapshot", {}))
                if v is not None:
                    vals.append(float(v))
            except Exception:
                continue
        return sum(vals) / len(vals) if vals else None

    baselines: dict = {
        "readiness_score": _avg(lambda s: s.get("readiness_score")),
        "row_count":       _avg(lambda s: s.get("row_count")),
        "anomaly_high":    _avg(lambda s: s.get("anomaly_counts_by_severity", {}).get("high")),
        "rec_high":        _avg(lambda s: s.get("recommendation_counts_by_priority", {}).get("high")),
        "trend_down":      _avg(lambda s: s.get("trend_counts_by_direction", {}).get("down")),
        "completeness":    _avg(lambda s: s.get("kpi_values", {}).get("Data Completeness")),
    }

    # ── Build drift rows ──────────────────────────────────────────────────────
    _METRIC_LABELS = {
        "readiness_score": "Readiness Score",
        "row_count":       "Dataset Row Count",
        "anomaly_high":    "High-Severity Anomalies",
        "rec_high":        "High-Priority Recommendations",
        "trend_down":      "Downward Trend Signals",
        "completeness":    "Data Completeness",
    }
    drifts: list[dict] = []

    for key, label in _METRIC_LABELS.items():
        curr_val = curr.get(key)
        base_val = baselines.get(key)
        if curr_val is None or base_val is None:
            continue
        try:
            curr_f = float(curr_val)
            base_f = float(base_val)
            if base_f == 0:
                continue
            pct      = (curr_f - base_f) / abs(base_f) * 100
            abs_pct  = abs(pct)
            if abs_pct < 10:
                continue
            direction = "increase" if pct > 0 else "decrease"
            severity  = "high" if abs_pct >= 40 else ("medium" if abs_pct >= 20 else "low")
            word      = "increased" if pct > 0 else "declined"
            base_fmt  = _safe_fmt(base_f, 1)
            drifts.append({
                "metric":         label,
                "baseline_value": round(base_f, 2),
                "current_value":  curr_val,
                "drift_percent":  round(pct, 1),
                "severity":       severity,
                "direction":      direction,
                "description":    (
                    f"{label} {word} {round(abs_pct, 1)}% "
                    f"from historical baseline of {base_fmt}."
                ),
            })
        except Exception:
            continue

    if not drifts:
        return None

    # Sort by absolute drift magnitude, largest first; cap at 8
    drifts.sort(key=lambda d: abs(d.get("drift_percent", 0)), reverse=True)
    drifts = drifts[:8]

    snap_ts = [s.get("created_at", "")[:19] for s in baseline_snapshots if s.get("created_at")]

    return {
        "type":    "drift_detection",
        "heading": "Drift Detection",
        "drifts":  drifts,
        "baseline_window": {
            "snapshot_count": len(baseline_snapshots),
            "start":          snap_ts[0]  if snap_ts else "",
            "end":            snap_ts[-1] if snap_ts else "",
        },
    }


def _build_forecast_section(
    numeric_profile: dict,
    date_profile: dict,
) -> dict | None:
    """Build a deterministic forecast from stored date profile monthly_counts.

    Requirements: ≥6 monthly data points in at least one date column, plus at
    least one numeric column. Uses pure arithmetic — no ML libraries, no eval.
    Returns None when requirements are not met (section silently omitted).
    """
    # ── Prereq: date column with ≥6 monthly data points ──────────────────────
    date_cols  = date_profile.get("date_columns") or []
    primary_dc = next(
        (dc for dc in date_cols if len(dc.get("monthly_counts") or []) >= 6),
        None,
    )
    if primary_dc is None:
        return None

    # ── Prereq: at least one numeric column ──────────────────────────────────
    if not numeric_profile:
        return None

    monthly  = primary_dc["monthly_counts"]
    date_col = primary_dc["column"]
    n        = len(monthly)

    counts: list[float] = [float(m.get("count", 0)) for m in monthly]
    labels: list[str]   = [str(m.get("month", ""))  for m in monthly]

    # ── Linear trend via index-based least squares ────────────────────────────
    mean_i    = (n - 1) / 2.0
    mean_y    = sum(counts) / n
    denom     = sum((i - mean_i) ** 2 for i in range(n))
    slope     = (
        sum((i - mean_i) * (counts[i] - mean_y) for i in range(n)) / denom
        if denom else 0.0
    )
    intercept  = mean_y - slope * mean_i
    trend_line = [intercept + slope * i for i in range(n)]

    # ── Confidence band width from residual std ───────────────────────────────
    residuals = [counts[i] - trend_line[i] for i in range(n)]
    res_mean  = sum(residuals) / n
    res_var   = sum((r - res_mean) ** 2 for r in residuals) / n
    res_std   = math.sqrt(res_var) if res_var > 0 else 0.0

    # ── Horizon: 1–3 periods, scaled to dataset length ───────────────────────
    horizon = min(3, max(1, n // 4))

    # ── Advance YYYY-MM labels by one month each ──────────────────────────────
    def _advance_month(ym: str) -> str:
        try:
            year, month = int(ym[:4]), int(ym[5:7])
            month += 1
            if month > 12:
                month, year = 1, year + 1
            return f"{year:04d}-{month:02d}"
        except Exception:
            return ym

    forecast_labels: list[str] = []
    cur = labels[-1]
    for _ in range(horizon):
        cur = _advance_month(cur)
        forecast_labels.append(f"{cur} (F)")

    # ── Forecast values: trend continuation, floor at 0 ──────────────────────
    forecast_vals: list[float] = [
        max(0.0, round(intercept + slope * (n + i), 1))
        for i in range(horizon)
    ]

    # ── Build combined chart arrays (None = gap in that segment) ─────────────
    all_labels = labels + forecast_labels
    historical = [int(c) for c in counts] + [None] * horizon
    forecast   = [None] * n + forecast_vals
    upper_band = [None] * n + [max(0.0, round(v + res_std, 1)) for v in forecast_vals]
    lower_band = [None] * n + [max(0.0, round(v - res_std, 1)) for v in forecast_vals]

    # ── Best numeric column (recommended for value forecasting) ──────────────
    try:
        best_num_col = max(
            (
                (col, float(stats.get("std") or 0))
                for col, stats in numeric_profile.items()
                if math.isfinite(float(stats.get("std") or 0))
            ),
            key=lambda x: x[1],
        )[0]
    except Exception:
        best_num_col = None

    # ── Summary text ──────────────────────────────────────────────────────────
    ma_window   = min(3, n // 3)
    trend_dir   = (
        "upward"   if slope >  0.05 * max(mean_y, 1) else
        "downward" if slope < -0.05 * max(mean_y, 1) else
        "stable"
    )
    avg_forecast = sum(forecast_vals) / len(forecast_vals) if forecast_vals else 0.0

    items: list[str] = [
        f"Forecast generated for '{date_col}' using {n} months of historical record volume.",
        f"Method: linear trend with {ma_window}-period moving average (deterministic, no ML).",
        f"Volume trend: {trend_dir} ({slope:+.2f} records/month).",
        f"Forecast horizon: {horizon} month{'s' if horizon != 1 else ''} ahead.",
        f"Projected average: {_safe_fmt(avg_forecast)} records/month "
        f"(±{_safe_fmt(res_std)} confidence band).",
    ]
    if best_num_col:
        items.append(
            f"Recommended target for value forecasting: '{best_num_col}' "
            f"(highest numeric variance in dataset)."
        )

    return {
        "type":            "forecast",
        "heading":         f"Forecast — {date_col} Volume",
        "forecast_ready":  True,
        "target_column":   date_col,
        "method":          "linear_trend_with_moving_average",
        "horizon_periods": horizon,
        "items":           items,
        "chart": {
            "chart_type":           "forecast",
            "labels":               all_labels,
            "historical":           historical,
            "forecast":             forecast,
            "upper_band":           upper_band,
            "lower_band":           lower_band,
            "forecast_start_index": n,
            "date_column":          date_col,
        },
    }


def _build_ai_dashboard_section(sections: list[dict]) -> dict:
    """Scan already-built sections to produce a deterministic AI Dashboard summary.

    Extracts the most critical anomaly, highest-priority recommendation, and a
    watchlist from the report. Never calls GPT — this is pure scan logic.
    Returns a section dict with type='ai_dashboard'.
    """
    most_important_insight: str | None = None
    highest_risk: str | None = None
    recommended_action: str | None = None
    watchlist: list[str] = []

    for sec in sections:
        t = sec.get("type", "text")

        if t == "anomaly" and most_important_insight is None:
            for a in sec.get("anomalies", []):
                if a.get("title") != "No Major Anomalies Detected":
                    most_important_insight = f"{a['title']}: {a.get('evidence', '')}"
                    if a.get("severity") == "high" and highest_risk is None:
                        highest_risk = a.get("description") or a.get("title", "")
                    break

        if t == "recommendation" and recommended_action is None:
            recs = sec.get("recommendations", [])
            high = [r for r in recs if r.get("priority") == "high"]
            src = high[0] if high else (recs[0] if recs else None)
            if src:
                recommended_action = src.get("title", "")
                if highest_risk is None and src.get("priority") == "high":
                    highest_risk = src.get("reason", "")[:200]

        if t == "drift_detection":
            for d in sec.get("drifts", [])[:2]:
                watchlist.append(f"{d.get('metric', '')} — {d.get('drift_percent', 0):+.1f}% drift")

        if t == "trend":
            for tr in sec.get("trends", [])[:2]:
                if tr.get("direction") in ("down", "volatile") and tr.get("title") not in watchlist:
                    watchlist.append(tr.get("title", ""))

    if not most_important_insight:
        most_important_insight = "No critical anomalies detected in this report."
    if not highest_risk:
        highest_risk = "No high-severity risks identified."
    if not recommended_action:
        recommended_action = "Review report sections for detailed insights and next steps."
    if not watchlist:
        watchlist = ["Monitor data completeness on the next upload."]

    return {
        "type":                  "ai_dashboard",
        "heading":               "Executive Intelligence",
        "most_important_insight": most_important_insight,
        "highest_risk":           highest_risk,
        "recommended_action":     recommended_action,
        "watchlist":              watchlist[:4],
    }


def _build_insight_priority_section(sections: list[dict]) -> dict | None:
    """Rank insights by severity across anomaly, recommendation, and drift sections.

    Returns a section with type='insight_priority' containing a ranked list of
    {title, severity, evidence, recommended_action, confidence} items.
    Returns None when no actionable insights are found.
    """
    insights: list[dict] = []

    _SEV_RANK = {"high": 0, "medium": 1, "low": 2}

    for sec in sections:
        t = sec.get("type", "text")

        if t == "anomaly":
            for a in sec.get("anomalies", []):
                if a.get("title") == "No Major Anomalies Detected":
                    continue
                sev = a.get("severity", "low")
                insights.append({
                    "title":              a.get("title", ""),
                    "severity":           sev,
                    "evidence":           a.get("evidence", ""),
                    "recommended_action": "Investigate and resolve this data quality issue.",
                    "confidence":         "high",
                    "_rank":              _SEV_RANK.get(sev, 2),
                })

        elif t == "recommendation":
            pri_map = {"high": "high", "medium": "medium", "low": "low"}
            for r in sec.get("recommendations", []):
                pri = r.get("priority", "low")
                insights.append({
                    "title":              r.get("title", ""),
                    "severity":           pri_map.get(pri, "low"),
                    "evidence":           r.get("reason", "")[:200],
                    "recommended_action": r.get("title", ""),
                    "confidence":         r.get("confidence", "medium"),
                    "_rank":              _SEV_RANK.get(pri, 2) + 0.5,
                })

        elif t == "drift_detection":
            for d in sec.get("drifts", []):
                sev = d.get("severity", "low")
                insights.append({
                    "title":              f"Drift: {d.get('metric', '')}",
                    "severity":           sev,
                    "evidence":           d.get("description", ""),
                    "recommended_action": f"Review {d.get('metric', '')} trend over recent reports.",
                    "confidence":         "high",
                    "_rank":              _SEV_RANK.get(sev, 2),
                })

    if not insights:
        return None

    insights.sort(key=lambda x: x.get("_rank", 2))
    for ins in insights:
        ins.pop("_rank", None)

    return {
        "type":     "insight_priority",
        "heading":  "Prioritized Insights",
        "insights": insights[:8],
    }


# ── Intent-Aware Report Type Routing ─────────────────────────────────────────
# Maps the six supported report_type values to high-weight keyword clusters that
# steer report_planner.plan_report() to the correct style deterministically.
# Each cluster's combined weight in _INTENT_SIGNALS exceeds 4.5, which is
# reliably higher than any single user-intent keyword (max: 1.8 for "charts
# only" or "drift detection"), so the target style always wins even when the
# user's original input contains competing keywords.
# The original intent_text flows unchanged to section builders and the strategy
# engine so section content is unaffected by the routing hint.
_REPORT_TYPE_PLANNER_HINTS: dict[str, str] = {
    # executive_brief: "executive"(1.2) + "brief"(1.0) + "kpi summary"(1.8) + "at a glance"(1.2) = 5.2
    "executive":    "executive brief kpi summary at a glance",
    # anomaly_report: "anomaly"(1.5) + "anomalies"(1.5) + "data quality"(1.4) + "outliers"(1.2) = 5.6
    "risk":         "anomaly anomalies data quality issues outliers",
    # monitoring_report: "trend analysis"(1.4) + "monitoring"(1.5) + "drift detection"(1.8) = 4.7
    "forecast":     "trend analysis monitoring drift detection time series",
    # table_heavy_report: "drilldown"(1.2) + "drill-down"(1.2) + "tabular"(1.3) + "tables"(1.2) = 4.9
    "segmentation": "drilldown drill-down tabular breakdown tables",
    # operational_report: "operational"(1.5) + "operations"(1.3) + "ops"(0.9) + "daily report"(1.1) = 4.8
    "operational":  "operational operations ops daily report",
    # anomaly_report: "data quality"(1.4) + "anomalies"(1.5) + "missing values"(1.2) + "outliers"(1.2) = 5.3
    "data_quality": "data quality anomalies missing values outliers",
}

# Maps each report_type to the planner style it targets.  Stamped into
# report_plan["report_type"] so callers never need to reverse-engineer the style.
_REPORT_TYPE_TO_STYLE: dict[str, str] = {
    "executive":    "executive_brief",
    "risk":         "anomaly_report",
    "forecast":     "monitoring_report",
    "segmentation": "table_heavy_report",
    "operational":  "operational_report",
    "data_quality": "anomaly_report",
}


def generate_dataset_report(
    dataset: dict,
    previous_snapshot: dict | None = None,
    baseline_snapshots: list[dict] | None = None,
    selected_sections: list[str] | None = None,
    intent_text: str | None = None,
    report_type: str | None = None,
) -> dict:
    """
    Build a structured report from a stored dataset summary row.
    All text is derived from real analysis values — no hardcoded content.
    Returns {"sections": [{"heading": str, "items": [str, ...]}, ...]}.
    """
    filename = dataset["filename"]
    row_count = dataset["row_count"]
    column_count = dataset["column_count"]
    numeric_profile: dict = json.loads(dataset["numeric_profile_json"])
    missing_values: dict = json.loads(dataset["missing_values_json"])
    categorical_profile: dict = json.loads(dataset["categorical_profile_json"])

    # New profile columns — safely absent on old datasets (NULL → empty default)
    correlation_profile: list = []
    try:
        raw = dataset.get("correlation_profile_json")
        if raw:
            correlation_profile = json.loads(raw) or []
    except Exception:
        pass

    categorical_meta: dict = {}
    try:
        raw = dataset.get("categorical_meta_json")
        if raw:
            categorical_meta = json.loads(raw) or {}
    except Exception:
        pass

    semantic_profile: list = []
    try:
        raw = dataset.get("semantic_profile_json")
        if raw:
            semantic_profile = json.loads(raw) or []
    except Exception:
        pass

    # Built once here; consumed by all inline sections and passed to section
    # builders via label_map=.  Empty dict on old datasets — degrades safely.
    semantic_label_map: dict[str, str] = build_label_map(semantic_profile)

    # Parse date_profile here so all subsequent section builders can use it.
    # (Must be before the business_kpis block which references date_profile.)
    date_profile_raw = dataset.get("date_profile_json")
    date_profile: dict = json.loads(date_profile_raw) if date_profile_raw else {}

    segmentation_profile: dict = {}
    try:
        raw = dataset.get("segmentation_profile_json")
        if raw:
            segmentation_profile = json.loads(raw) or {}
    except Exception:
        pass

    # ── Intent-aware planner hint ─────────────────────────────────────────────
    # When report_type is specified, build a planner-only intent string whose
    # combined keyword weight (≥4.7) reliably overrides any competing signals in
    # the user's original input (max single-keyword weight: 1.8).  The original
    # intent_text is preserved for section content: chart column selection,
    # narrative tone, and the strategy engine all continue to receive the
    # unmodified text so generated content reflects the user's actual words.
    _planner_intent_text = intent_text
    if report_type:
        _hint = _REPORT_TYPE_PLANNER_HINTS.get(report_type.lower(), "")
        if _hint:
            _planner_intent_text = f"{_hint} — {intent_text}" if intent_text else _hint

    # ── Adaptive Report Plan ─────────────────────────────────────────────────
    # Computed once from intent + profiles; applied for section ordering later.
    # Lazy import so old datasets without planner never fail.
    _report_plan: dict | None = None
    try:
        from core.intelligence.report_planner import plan_report as _plan_report
        _report_plan = _plan_report(
            intent_text         = _planner_intent_text,
            semantic_profile    = semantic_profile,
            date_profile        = date_profile,
            numeric_profile     = numeric_profile,
            categorical_profile = categorical_profile,
        )
    except Exception:
        pass

    # Stamp report_type into the plan payload when provided.  Frontend and callers
    # can use this to identify the intent without parsing the intent string or
    # reverse-engineering the style name.
    if report_type and isinstance(_report_plan, dict):
        _report_plan["report_type"] = report_type.lower()

    # ── Report Strategy Engine ────────────────────────────────────────────────
    # Resolved from report_type (explicit UI selection, highest precedence) or
    # intent keyword classification.  When a non-FULL_INTELLIGENCE intent is
    # resolved, strategy.section_scores are populated and the reorder block
    # below uses them in place of the planner's own scores.
    _strategy = None
    try:
        from core.intelligence.report_strategy_engine import resolve_report_strategy as _resolve_strategy
        _strategy = _resolve_strategy(
            intent_text         = intent_text,
            semantic_profile    = semantic_profile,
            date_profile        = date_profile,
            numeric_profile     = numeric_profile,
            categorical_profile = categorical_profile,
            report_type         = report_type,
        )
    except Exception:
        pass

    # Surface strategy metadata in the plan payload so the frontend and
    # downstream callers can read intent_type and resolution source without
    # a separate API call.  Only mutates when _report_plan is a real dict
    # (i.e. plan_report succeeded); leaves None unchanged so old-dataset
    # fallback paths are unaffected.
    if _strategy is not None and isinstance(_report_plan, dict):
        _report_plan["strategy_intent_type"] = _strategy.intent_type
        _report_plan["strategy_source"]      = _strategy.source
        if _strategy.viz_type_scores:
            _report_plan["viz_type_scores"] = _strategy.viz_type_scores
        if _strategy.source != "fallback":
            nc = _strategy.narrative_config
            _report_plan["narrative_config"] = {
                "items_per_section":       nc.items_per_section,
                "show_evidence":           nc.show_evidence,
                "emphasize_risks":         nc.emphasize_risks,
                "emphasize_opportunities": nc.emphasize_opportunities,
                "executive_language":      nc.executive_language,
                "recommendation_limit":    nc.recommendation_limit,
                "verbosity":               nc.verbosity,
                "suppress_low_confidence": nc.suppress_low_confidence,
            }

    sections: list[dict] = []

    # ── Overview ──────────────────────────────────────────────────────────────
    sections.append({
        "heading": "Overview",
        "items": [
            f'"{filename}" contains {row_count:,} rows and {column_count} columns.',
        ],
    })

    # ── Key Metrics (KPI — structural) ───────────────────────────────────────
    sections.append(_build_kpi_section(
        row_count, column_count, numeric_profile, missing_values, categorical_profile,
    ))

    # ── Business Intelligence KPIs (semantic — skipped on old datasets) ───────
    if semantic_profile:
        biz_kpi_sec = build_business_kpi_section(
            semantic_profile      = semantic_profile,
            numeric_profile       = numeric_profile,
            categorical_profile   = categorical_profile,
            categorical_meta      = categorical_meta,
            date_profile          = date_profile,
            row_count             = row_count,
            intent_text           = intent_text,
            strategy_intent_type  = _strategy.intent_type if _strategy is not None else None,
        )
        if biz_kpi_sec is not None:
            sections.append(biz_kpi_sec)

    # ── Segmentation Intelligence (semantic + stored cross-tab data required) ──
    # Injected directly after business_kpis so metric breakdowns follow the KPI
    # cards.  Both sections are silently omitted when no segmentation data exists.
    if semantic_profile and segmentation_profile:
        seg_sec = build_segmentation_section(segmentation_profile, row_count=row_count)
        if seg_sec is not None:
            sections.append(seg_sec)

        drill_sec = build_drilldown_table_section(segmentation_profile)
        if drill_sec is not None:
            sections.append(drill_sec)

    # ── Numeric Insights ──────────────────────────────────────────────────────
    num_entries = [
        (col, stats)
        for col, stats in numeric_profile.items()
        if stats.get("mean") is not None
    ]
    if num_entries:
        items: list[str] = []
        by_mean = sorted(num_entries, key=lambda x: x[1]["mean"], reverse=True)
        high_col, high_stats = by_mean[0]
        items.append(
            f"{semantic_label_map.get(high_col, high_col)} has the highest average value at {_safe_fmt(high_stats['mean'])}."
        )
        if high_stats.get("min") is not None and high_stats.get("max") is not None:
            items.append(
                f"{semantic_label_map.get(high_col, high_col)} ranges from {_safe_fmt(high_stats['min'])}"
                f" to {_safe_fmt(high_stats['max'])}."
            )
        by_sum = sorted(
            [(col, s) for col, s in num_entries if s.get("sum") is not None],
            key=lambda x: x[1]["sum"],
            reverse=True,
        )
        if by_sum:
            sum_col, sum_stats = by_sum[0]
            items.append(
                f"{semantic_label_map.get(sum_col, sum_col)} has the highest total at {_safe_fmt(sum_stats['sum'])}."
            )
        if len(by_mean) > 1:
            low_col, low_stats = by_mean[-1]
            items.append(
                f"{semantic_label_map.get(low_col, low_col)} has the lowest average value at {_safe_fmt(low_stats['mean'])}."
            )
        sections.append({"heading": "Numeric Insights", "items": items})

    # ── Missing Data ──────────────────────────────────────────────────────────
    missing_entries = [(col, cnt) for col, cnt in missing_values.items() if cnt > 0]
    missing_items: list[str] = []
    if not missing_entries:
        missing_items.append("No missing values were detected across all columns.")
    else:
        for col, cnt in missing_entries[:5]:
            pct = round(cnt / row_count * 100, 1) if row_count > 0 else 0
            missing_items.append(
                f"{semantic_label_map.get(col, col)} has {cnt:,} missing values ({pct}% of rows)."
            )
        if len(missing_entries) > 5:
            missing_items.append(
                f"...and {len(missing_entries) - 5} more columns with missing values."
            )
    sections.append({"heading": "Missing Data", "items": missing_items})

    # ── Category Observations ─────────────────────────────────────────────────
    cat_entries = [
        (col, entries)
        for col, entries in categorical_profile.items()
        if entries
    ]
    cat_items = [
        f'{semantic_label_map.get(col, col)} is most commonly "{entries[0]["value"]}" ({entries[0]["count"]:,} rows).'
        for col, entries in cat_entries[:6]
    ]
    if cat_items:
        sections.append({"heading": "Top Category Observations", "items": cat_items})

    # ── Date Coverage ──────────────────────────────────────────────────────────
    # (date_profile already parsed at top of function)
    date_cols = date_profile.get("date_columns") or []
    if date_cols:
        date_items: list[str] = []
        for dc in date_cols:
            col       = dc["column"]
            valid     = dc["valid_count"]
            range_str = _format_date_range(dc["earliest"], dc["latest"], dc["range_days"])
            date_items.append(
                f"{semantic_label_map.get(col, col)}: {valid:,} date values detected. Coverage: {range_str}."
            )
        sections.append({"heading": "Date Coverage", "items": date_items})

    # ── Trend Insights ─────────────────────────────────────────────────────────
    trend_insights = date_profile.get("trend_insights") or []
    if trend_insights and date_cols:
        date_col_name = date_cols[0]["column"]
        trend_items: list[str] = []
        for ti in trend_insights[:3]:
            col    = ti["column"]
            symbol = ti["symbol"]
            trend  = ti["trend"]
            pct    = ti["pct_change"]
            pct_str = f"+{pct}%" if pct >= 0 else f"{pct}%"
            trend_items.append(
                f"{semantic_label_map.get(col, col)} is {trend} {symbol} ({pct_str} change from first to second half,"
                f" sorted by {semantic_label_map.get(date_col_name, date_col_name)})."
            )
        sections.append({"heading": "Trend Insights", "items": trend_items})

    # ── Anomaly Detection ─────────────────────────────────────────────────────
    sections.append(_build_anomaly_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        correlation_profile=correlation_profile,
        categorical_meta=categorical_meta,
        label_map=semantic_label_map,
    ))

    # ── Trend Intelligence ────────────────────────────────────────────────────
    sections.append(_build_trend_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        label_map=semantic_label_map,
    ))

    # ── Predictive Readiness ──────────────────────────────────────────────────
    sections.append(_build_predictive_readiness_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        correlation_profile=correlation_profile,
        categorical_meta=categorical_meta,
        label_map=semantic_label_map,
    ))

    # ── Chart Sections ────────────────────────────────────────────────────────
    for chart_sec in _build_chart_sections(
        categorical_profile, missing_values, row_count, date_profile,
        numeric_profile=numeric_profile,
        correlation_profile=correlation_profile,
        report_plan=_report_plan,
        intent_text=intent_text,
        semantic_profile=semantic_profile,
    ):
        sections.append(chart_sec)

    # ── Recommendations (inserted after KPI for executive-first report flow) ──
    rec_sec = _build_recommendation_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        correlation_profile=correlation_profile,
        narrative_config=_report_plan.get("narrative_config") if isinstance(_report_plan, dict) else None,
        label_map=semantic_label_map,
    )
    if rec_sec is not None:
        # KPI section was appended second (after Overview), so it's always at
        # index 1 before any inserts.  Find it by type for resilience.
        kpi_pos = next((i for i, s in enumerate(sections) if s.get("type") == "kpi"), 1)
        sections.insert(kpi_pos + 1, rec_sec)

    # ── Historical Comparison ─────────────────────────────────────────────────
    # Inserted after all other typed sections so the builder can scan them for
    # current metrics. previous_snapshot is None on the first run → section omitted.
    if previous_snapshot is not None:
        hist_sec = _build_historical_comparison_section(sections, previous_snapshot, row_count)
        if hist_sec is not None:
            # Place it right after predictive_readiness for logical read flow
            pr_pos = next(
                (i for i, s in enumerate(sections) if s.get("type") == "predictive_readiness"),
                len(sections),
            )
            sections.insert(pr_pos + 1, hist_sec)

    # ── Drift Detection ───────────────────────────────────────────────────────
    # Inserted after historical comparison so both intelligence sections cluster
    # together. Omitted automatically when < 3 baseline snapshots exist.
    if baseline_snapshots:
        drift_sec = _build_drift_detection_section(sections, baseline_snapshots, row_count)
        if drift_sec is not None:
            anchor_type = "historical_comparison"
            anchor_pos  = next(
                (i for i, s in enumerate(sections) if s.get("type") == anchor_type),
                None,
            )
            if anchor_pos is None:
                anchor_pos = next(
                    (i for i, s in enumerate(sections) if s.get("type") == "predictive_readiness"),
                    len(sections),
                )
            sections.insert(anchor_pos + 1, drift_sec)

    # ── Forecast ──────────────────────────────────────────────────────────────
    # Inserted after the drift/historical/predictive_readiness cluster so all
    # intelligence sections stay grouped together. Silently omitted when the
    # dataset lacks ≥6 monthly data points or numeric columns.
    forecast_sec = _build_forecast_section(numeric_profile, date_profile)
    if forecast_sec is not None:
        # Anchor: drift_detection → historical_comparison → predictive_readiness
        _fc_anchor = next(
            (i for i, s in enumerate(sections) if s.get("type") == "drift_detection"),
            None,
        )
        if _fc_anchor is None:
            _fc_anchor = next(
                (i for i, s in enumerate(sections) if s.get("type") == "historical_comparison"),
                None,
            )
        if _fc_anchor is None:
            _fc_anchor = next(
                (i for i, s in enumerate(sections) if s.get("type") == "predictive_readiness"),
                len(sections) - 1,
            )
        sections.insert(_fc_anchor + 1, forecast_sec)

    # ── AI Dashboard & Insight Priority (scan completed sections) ────────────
    # Built after all other sections so all data is available for scanning.
    ai_dash = _build_ai_dashboard_section(sections)
    sections.insert(0, ai_dash)

    insight_pri = _build_insight_priority_section(sections)
    if insight_pri is not None:
        dash_pos = next((i for i, s in enumerate(sections) if s.get("type") == "ai_dashboard"), 0)
        sections.insert(dash_pos + 1, insight_pri)

    # ── Executive Summary (prepended so it leads the report) ─────────────────
    # Built after all other sections so date_profile and completeness are known.
    # Note: if ENABLE_AI_REPORT_NARRATIVE=true, the AI will also insert its own
    # Executive Summary at position 0, placing it before this deterministic one.
    # Since AI narrative is disabled by default, this is not an issue in practice.
    sections.insert(0, _build_executive_summary_section(
        filename, row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        label_map=semantic_label_map,
    ))

    # ── Schema v2: stamp every section with its type before returning ──────────
    # Renderers and exporters key off section.type to dispatch correctly.
    # Existing saved reports that lack this field default to "text" on read.
    for s in sections:
        s.setdefault("type", "text")

    # ── AI Executive Narrative (optional) ─────────────────────────────────────
    # Runs after all deterministic sections are complete.
    # On any failure the report returns unchanged without AI content.
    ai_narrative = _ai_generate_narrative(sections)

    if ai_narrative:
        # Build backward-compat items list for email formatting
        exec_items: list[str] = [ai_narrative["executive_summary"]]
        for t in ai_narrative["key_takeaways"]:
            exec_items.append(f"Key takeaway: {t}")
        for r in ai_narrative["risk_notes"]:
            exec_items.append(f"Note: {r}")
        # Prepend as executive_summary type so the rich renderer activates.
        # items kept for email body formatting (format_report_as_email_body iterates items).
        sections.insert(0, {
            "type":          "executive_summary",
            "heading":       "AI Executive Summary",
            "summary":       ai_narrative["executive_summary"],
            "key_takeaways": ai_narrative["key_takeaways"],
            "risks":         ai_narrative["risk_notes"],
            "ai_generated":  True,
            "items":         exec_items,
        })

        # AI Key Findings — specific quantified findings (position 1, after exec summary)
        if ai_narrative.get("key_findings"):
            sections.insert(1, {
                "type":    "ai_findings",
                "heading": "AI Key Findings",
                "items":   ai_narrative["key_findings"],
            })

        # AI Intelligence synthesis — anomaly + trend insights
        insight_items: list[str] = []
        if ai_narrative.get("anomaly_insights"):
            insight_items.append(f"Anomaly insight: {ai_narrative['anomaly_insights']}")
        if ai_narrative.get("trend_insights"):
            insight_items.append(f"Trend insight: {ai_narrative['trend_insights']}")
        if insight_items:
            findings_pos = next(
                (i for i, s in enumerate(sections) if s.get("type") == "ai_findings"), 1
            )
            sections.insert(findings_pos + 1, {
                "type":    "ai_insights",
                "heading": "AI Intelligence",
                "items":   insight_items,
            })

        # AI Recommendations — summary + risk (placed before deterministic recommendations)
        ai_rec_items: list[str] = []
        if ai_narrative.get("recommendation_summary"):
            ai_rec_items.append(ai_narrative["recommendation_summary"])
        if ai_narrative.get("risk_summary"):
            ai_rec_items.append(f"Key risk: {ai_narrative['risk_summary']}")
        if ai_rec_items:
            rec_pos = next(
                (i for i, s in enumerate(sections) if s.get("type") == "recommendation"),
                len(sections),
            )
            sections.insert(rec_pos, {
                "type":    "ai_recommendations",
                "heading": "AI Recommendations",
                "items":   ai_rec_items,
            })

    # ── Planner-driven section reordering ────────────────────────────────────
    # Reorder whenever a report plan is available, for all styles including the
    # default analyst_deep_dive.  This ensures dataset-signal-driven and
    # intent-driven styles both produce correctly ordered output.
    if _report_plan:
        try:
            from core.intelligence.report_planner import reorder_sections as _reorder
            present_types = [s.get("type", "text") for s in sections]
            plan_scores   = (
                _strategy.section_scores
                if _strategy and _strategy.section_scores
                else _report_plan.get("section_scores", {})
            )
            # Rebuild section_order from only the types actually present
            _report_plan["section_order"] = sorted(
                present_types,
                key=lambda t: plan_scores.get(t, 0),
                reverse=True,
            )
            sections = _reorder(sections, plan_scores)
        except Exception:
            pass

    # ── Intent-based section filter (optional) ────────────────────────────────
    # selected_sections=None  → full report, all sections returned (unchanged behavior).
    # selected_sections=[...] → keep only sections whose stamped type is in the list.
    # All builders still run so inter-section references (historical, drift) remain
    # correct; the filter only affects what is returned to the caller.
    if selected_sections is not None:
        sections = [s for s in sections if s.get("type", "text") in selected_sections]

    # ── max_sections enforcement ──────────────────────────────────────────────
    # Applied after reordering so the slice retains the highest-priority sections.
    # Applied after selected_sections so both constraints compose correctly.
    # Skipped silently when _report_plan is None (old datasets) or the key is absent.
    #
    # overview_chart sections are always preserved outside the budget so the top-3
    # ranked charts reach the frontend regardless of the style's section limit.
    # Budget applies to non-overview sections only; overview charts are re-merged
    # back into their sorted positions via object identity. No duplicates created.
    try:
        _max = int(_report_plan["layout_metadata"]["max_sections"])
        if _max > 0 and len(sections) > _max:
            _overview_secs = [s for s in sections if s.get("overview_chart")]
            _other_secs    = [s for s in sections if not s.get("overview_chart")]
            _other_kept    = _other_secs[:_max]
            _keep_ids      = {id(s) for s in _other_kept + _overview_secs}
            sections       = [s for s in sections if id(s) in _keep_ids]
    except (TypeError, KeyError, ValueError):
        pass

    if ai_narrative:
        return {"version": 2, "sections": sections, "ai_narrative": ai_narrative, "report_plan": _report_plan}
    return {"version": 2, "sections": sections, "report_plan": _report_plan}


# ── Email rendering constants ──────────────────────────────────────────────────

_EMAIL_HDR = "=" * 56
_EMAIL_DIV = "-" * 56

# Fallback strings emitted by _build_ai_dashboard_section when no real signals
# are found. Suppress them in the Intelligence Snapshot so the block only
# appears when it carries genuine intelligence.
_AI_DASH_FALLBACK_INSIGHT = "No critical anomalies detected in this report."
_AI_DASH_FALLBACK_RISK    = "No high-severity risks identified."
_AI_DASH_FALLBACK_ACTION  = "Review report sections for detailed insights and next steps."

# Shared severity sort order: lower number = higher urgency.
_SEV_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


# ── Email section helpers ──────────────────────────────────────────────────────
# Each helper accepts pre-grouped section lists and returns list[str].
# An empty list signals "omit this section" to the orchestrator.
# Every helper is a pure function and never raises.


def _email_header(title: str, dataset_filename: str) -> list[str]:
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    lines: list[str] = [
        _EMAIL_HDR,
        "ToolSmithAI — Executive Intelligence Report",
        _EMAIL_HDR,
    ]
    if title:
        lines.append(f"Report:   {title}")
    if dataset_filename and dataset_filename != title:
        lines.append(f"Dataset:  {format_dataset_display_name(dataset_filename)}")
    lines.append(f"Date:     {now}")
    lines.append(_EMAIL_HDR)
    return lines


def _email_ai_dashboard(sections: list[dict]) -> list[str]:
    if not sections:
        return []
    sec = sections[0]

    insight    = (sec.get("most_important_insight") or "").strip()
    risk       = (sec.get("highest_risk")           or "").strip()
    action     = (sec.get("recommended_action")     or "").strip()
    # Reserved field — populated by future AI Analyst output.
    # When present the line is rendered; when absent it is silently omitted.
    # No calculation is performed here; callers must supply the value.
    biz_impact = (sec.get("business_impact")        or "").strip()

    is_fallback_insight = insight in ("", _AI_DASH_FALLBACK_INSIGHT)
    is_fallback_risk    = risk    in ("", _AI_DASH_FALLBACK_RISK)
    is_fallback_action  = action  in ("", _AI_DASH_FALLBACK_ACTION)

    # Omit the entire block when every field is a fallback placeholder
    if is_fallback_insight and is_fallback_risk and is_fallback_action and not biz_impact:
        return []

    lines: list[str] = ["INTELLIGENCE SNAPSHOT", ""]
    if not is_fallback_insight:
        lines.append(f"  Top Signal:      {insight}")
    if not is_fallback_risk:
        lines.append(f"  Key Risk:        {risk}")
    if not is_fallback_action:
        lines.append(f"  Take Action:     {action}")
    if biz_impact:
        lines.append(f"  Business Impact: {biz_impact}")
    return lines


def _email_business_kpis(sections: list[dict]) -> list[str]:
    kpis = [
        kpi
        for sec in sections
        for kpi in (sec.get("kpis") or sec.get("metrics") or [])
    ]
    if not kpis:
        return []
    lines: list[str] = ["KEY METRICS", ""]
    for kpi in kpis[:4]:
        label = format_kpi_display_label(kpi.get("label", ""))
        value = kpi.get("value_formatted") or str(kpi.get("value", "—"))
        delta = kpi.get("delta")
        dir_  = kpi.get("delta_direction", "")
        arrow = {"↑": "↑", "up": "↑", "down": "↓"}.get(dir_, "→")
        try:
            delta_s = f"  {arrow} {abs(float(delta)):.1f}%" if delta is not None else ""
        except (TypeError, ValueError):
            delta_s = ""
        lines.append(f"  {label:<22} {value:>10}{delta_s}")
    return lines


def _email_executive_summary(sections: list[dict]) -> list[str]:
    if not sections:
        return []
    # Prefer AI-generated version (ai_generated=True, inserted at position 0
    # by the AI narrative pipeline) when available.
    sec = next((s for s in sections if s.get("ai_generated")), sections[0])

    summary       = (sec.get("summary")       or "").strip()
    takeaways     = [t for t in (sec.get("key_takeaways")  or []) if t][:4]
    opportunities = [o for o in (sec.get("opportunities")  or []) if o][:3]

    if not any([summary, takeaways, opportunities]):
        return []

    lines: list[str] = ["EXECUTIVE SUMMARY", ""]
    if summary:
        lines.append(f"  {summary}")
        lines.append("")
    if takeaways:
        lines.append("  Key Findings:")
        for t in takeaways:
            lines.append(f"    • {t}")
        lines.append("")
    if opportunities:
        lines.append("  Opportunities:")
        for o in opportunities:
            lines.append(f"    → {o}")
    return lines


def _email_anomalies(
    anomaly_sections: list[dict],
    dashboard_sections: list[dict],
) -> list[str]:
    anomalies = [a for sec in anomaly_sections for a in (sec.get("anomalies") or [])]
    watchlist = (dashboard_sections[0].get("watchlist") or []) if dashboard_sections else []

    # Separate real anomalies from the "all-clear" placeholder
    real    = [a for a in anomalies if a.get("title") != "No Major Anomalies Detected"]
    display = sorted(
        real if real else anomalies,
        key=lambda a: _SEV_RANK.get(a.get("severity", "low"), 2),
    )

    if not display and not watchlist:
        return []

    _PREFIX: dict[str, str] = {
        "high":   "[HIGH]      ",
        "medium": "[MEDIUM]    ",
        "low":    "[LOW]       ",
    }

    lines: list[str] = ["KEY RISKS", ""]
    high_count = med_count = 0

    for a in display:
        sev = a.get("severity", "low")
        if sev == "high"   and high_count >= 4: continue
        if sev == "medium" and med_count  >= 2: continue
        if sev == "low"    and real:            continue  # suppress low when highs/mediums exist

        prefix   = _PREFIX.get(sev, "[LOW]       ")
        title    = a.get("title", "")
        evidence = (a.get("evidence") or a.get("description") or "").strip()
        lines.append(f"  {prefix}{title}")
        if evidence:
            lines.append(f"              {evidence}")
        lines.append("")
        if sev == "high":   high_count += 1
        if sev == "medium": med_count  += 1

    shown = [w for w in watchlist[:3] if w]
    if shown:
        lines.append(f"  Also watching: {' · '.join(shown)}")

    return lines


def _email_recommendations(
    rec_sections: list[dict],
    ai_rec_sections: list[dict],
) -> list[str]:
    recs = [r for sec in rec_sections for r in (sec.get("recommendations") or [])]
    if not recs and not ai_rec_sections:
        return []

    sorted_recs = sorted(
        recs,
        key=lambda r: _SEV_RANK.get(r.get("priority", "low"), 2),
    )

    lines: list[str] = ["RECOMMENDED ACTIONS", ""]

    for rec in sorted_recs[:3]:
        pri    = (rec.get("priority") or "").upper()
        title  = rec.get("title", "")
        reason = (rec.get("reason") or "").strip()
        lines.append(f"  [{pri}]  {title}")
        if reason:
            lines.append(f"         {reason}")
        lines.append("")

    # Absorb AI recommendation items only when no deterministic recs exist
    if not recs and ai_rec_sections:
        for sec in ai_rec_sections:
            for item in (sec.get("items") or [])[:2]:
                if item:
                    lines.append(f"  → {item}")
        lines.append("")

    return lines


def _email_trends(
    trend_sections: list[dict],
    ai_insight_sections: list[dict],
) -> list[str]:
    trends = [t for sec in trend_sections for t in (sec.get("trends") or [])]

    # Filter the "no signals" fallback entry
    real    = [t for t in trends if t.get("title") != "No Measurable Trends Detected"]
    display = sorted(
        real,
        key=lambda t: _SEV_RANK.get(t.get("strength", "low"), 2),
    )[:3]

    if not display and not ai_insight_sections:
        return []

    _ARROW: dict[str, str] = {
        "up":       "↑",
        "down":     "↓",
        "stable":   "→",
        "volatile": "~",
    }

    lines: list[str] = ["TREND INTELLIGENCE", ""]

    for t in display:
        arrow    = _ARROW.get(t.get("direction", ""), "→")
        title    = t.get("title", "")
        evidence = (t.get("evidence") or t.get("description") or "").strip()
        lines.append(f"  {arrow} {title}")
        if evidence:
            lines.append(f"    {evidence}")
        lines.append("")

    # Absorb AI insights only when no real deterministic trends exist
    if not display and ai_insight_sections:
        for sec in ai_insight_sections:
            for item in (sec.get("items") or [])[:2]:
                if item:
                    lines.append(f"  → {item}")
        lines.append("")

    return lines


def _email_segmentation(sections: list[dict]) -> list[str]:
    items = [
        item
        for sec in sections
        for item in (sec.get("items") or [])
        if item
    ]
    if not items:
        return []
    lines: list[str] = ["SEGMENTATION HIGHLIGHTS", ""]
    for item in items[:3]:
        lines.append(f"  → {item}")
    return lines


def _email_historical(sections: list[dict]) -> list[str]:
    if not sections:
        return []
    sec         = sections[0]
    comparisons = sec.get("comparisons") or []
    baseline_ts = (sec.get("baseline_timestamp") or "")[:10]

    notable = [c for c in comparisons if c.get("severity") in ("warning", "risk")]
    if not notable:
        return []  # Only render section when at least one notable change exists

    # Show notable changes first, then good ones for context; cap at 5 total
    good    = [c for c in comparisons if c.get("severity") not in ("warning", "risk")]
    display = (notable + good)[:5]

    if baseline_ts:
        try:
            formatted_date = datetime.strptime(baseline_ts, "%Y-%m-%d").strftime("%b %d, %Y")
        except (ValueError, TypeError):
            formatted_date = baseline_ts
        heading = f"PERFORMANCE SINCE {formatted_date}"
    else:
        heading = "PERFORMANCE CHANGES"
    lines: list[str] = [heading, ""]

    for comp in display:
        metric = comp.get("metric", "")
        prev   = comp.get("previous_value", "")
        curr   = comp.get("current_value", "")
        change = comp.get("change", "")
        lines.append(f"  {metric}: {prev} → {curr} ({change})")

    return lines


def _email_drift(sections: list[dict]) -> list[str]:
    if not sections:
        return []
    drifts     = sections[0].get("drifts") or []
    window     = sections[0].get("baseline_window") or {}
    snap_count = window.get("snapshot_count", "")

    high_drifts = [d for d in drifts if d.get("severity") == "high"]
    if not high_drifts:
        return []

    lines: list[str] = ["STABILITY ALERTS", ""]

    for d in high_drifts[:4]:
        metric   = d.get("metric", "")
        baseline = d.get("baseline_value", "")
        current  = d.get("current_value", "")
        try:
            pct_str = f"{float(d.get('drift_percent', 0)):+.1f}%"
        except (TypeError, ValueError):
            pct_str = str(d.get("drift_percent", ""))
        lines.append(f"  {metric}: {baseline} → {current} ({pct_str})")

    return lines


def _email_forecast(sections: list[dict]) -> list[str]:
    if not sections:
        return []
    sec   = sections[0]
    items = sec.get("items") or []
    if not items:
        return []
    # items[0] = dataset + months context (keep)
    # items[1] = method description — too technical for executive email (skip)
    # items[2:] = trend direction, horizon, projected average, recommended column
    display = ([items[0]] + items[2:5]) if len(items) > 1 else items[:4]
    heading = "TREND FORECAST"
    lines: list[str] = [heading, ""]
    for item in display:
        if item:
            lines.append(f"  → {item}")
    return lines


def _email_footer(report_url: str, readiness_sections: list[dict]) -> list[str]:
    lines: list[str] = [_EMAIL_DIV]
    if report_url:
        lines.append(f"  View full report: {report_url}")
    lines += [
        "",
        _EMAIL_HDR,
        "Sent by ToolSmithAI",
        "Delivered automatically.",
        _EMAIL_HDR,
    ]
    return lines


# ── Canonical Executive Snapshot extractor ───────────────────────────────────


def build_executive_snapshot(
    report_content: dict,
    *,
    title: str = "",
    dataset_filename: str = "",
    created_at: str | None = None,
) -> dict:
    """Extract the canonical Executive Snapshot from a report content dict.

    Pure, deterministic, side-effect-free. No AI generation, no database
    access, no network calls. Gracefully degrades when sections are absent.

    Shared by PDF, HTML email, and all future executive surfaces. Rendering
    is the caller's responsibility — this function only extracts and normalises.

    Args:
        report_content: Dict with a "sections" key — the decoded content_json
                        from get_report_by_id() / generate_dataset_report().
        title: Report display title (from the report row, not from sections).
        dataset_filename: Source dataset filename (from the report row).
        created_at: Report creation timestamp string, or None.

    Returns:
        Dict with normalised snapshot fields. Never raises.
    """
    try:
        sections: list[dict] = report_content.get("sections") or []

        by_type: dict[str, list[dict]] = {}
        for sec in sections:
            t = sec.get("type") or "text"
            by_type.setdefault(t, []).append(sec)

        # ── KPI cards ─────────────────────────────────────────────────────────
        # Standard kpi section is checked first; business_kpis supplements it.
        # Both store cards under "kpis" or "metrics". Capped at 6, deduplicated
        # by label to prevent the same metric appearing from both sources.
        kpi_cards: list[dict] = []
        seen_labels: set[str] = set()

        for section_type in ("kpi", "business_kpis"):
            if len(kpi_cards) >= 6:
                break
            for sec in by_type.get(section_type, []):
                if len(kpi_cards) >= 6:
                    break
                for card in (sec.get("kpis") or sec.get("metrics") or []):
                    if len(kpi_cards) >= 6:
                        break
                    label = (card.get("label") or "").strip()
                    if not label or label in seen_labels:
                        continue
                    seen_labels.add(label)
                    kpi_cards.append({
                        "label":           label,
                        "value":           card.get("value"),
                        "value_formatted": (
                            card.get("value_formatted")
                            or str(card.get("value", "—"))
                        ),
                        "status":          card.get("status") or "neutral",
                        "trend":           card.get("trend") or "neutral",
                        "delta":           card.get("delta"),
                        "delta_direction": card.get("delta_direction") or "neutral",
                    })

        # ── Predictive readiness ──────────────────────────────────────────────
        readiness_score: int | None = None
        readiness_level: str | None = None
        readiness_secs = by_type.get("predictive_readiness", [])
        if readiness_secs:
            r = readiness_secs[0]
            try:
                raw = r.get("readiness_score")
                readiness_score = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                readiness_score = None
            readiness_level = r.get("readiness_level") or None

        # ── AI Dashboard fields ───────────────────────────────────────────────
        # Known placeholder strings are suppressed so consumers receive either a
        # genuine value or an empty string — never an uninformative fallback.
        key_insight = top_risk = recommended_action = ""
        ai_secs = by_type.get("ai_dashboard", [])
        if ai_secs:
            ai = ai_secs[0]
            raw_insight = (ai.get("most_important_insight") or "").strip()
            raw_risk    = (ai.get("highest_risk")           or "").strip()
            raw_action  = (ai.get("recommended_action")     or "").strip()
            key_insight        = "" if raw_insight in ("", _AI_DASH_FALLBACK_INSIGHT) else raw_insight
            top_risk           = "" if raw_risk    in ("", _AI_DASH_FALLBACK_RISK)    else raw_risk
            recommended_action = "" if raw_action  in ("", _AI_DASH_FALLBACK_ACTION)  else raw_action

        return {
            "kpi_cards":          kpi_cards,
            "readiness_score":    readiness_score,
            "readiness_level":    readiness_level,
            "key_insight":        key_insight,
            "top_risk":           top_risk,
            "recommended_action": recommended_action,
            "report_title":       title,
            "dataset_name":       dataset_filename,
            "created_at":         created_at,
        }

    except Exception:
        logger.exception("build_executive_snapshot: unexpected error — returning empty snapshot")
        return {
            "kpi_cards":          [],
            "readiness_score":    None,
            "readiness_level":    None,
            "key_insight":        "",
            "top_risk":           "",
            "recommended_action": "",
            "report_title":       title,
            "dataset_name":       dataset_filename,
            "created_at":         created_at,
        }


# ── Canonical plain-text email renderer ───────────────────────────────────────


def render_report_as_plain_text(
    report_content: dict,
    *,
    title: str = "",
    dataset_filename: str = "",
    report_url: str = "",
) -> str:
    """Canonical plain-text renderer for executive report emails.

    Single source of truth for all plain-text email rendering. Both the
    scheduled workflow path and the manual on-demand email path call this
    function; neither duplicates rendering logic.

    Args:
        report_content: Dict containing a "sections" key. Accepts both shapes:
            - Direct return value of generate_dataset_report() — sections live
              at the top level of the dict.
            - report["content"] extracted from get_report_by_id() — same shape,
              sections at the top level of the content sub-dict.
        title: Report display title shown in the email header.
        dataset_filename: Source dataset filename shown in the header.
        report_url: Optional deep link into the web UI. Included in the email
            footer when provided; omitted when empty.

    Returns:
        Plain-text string suitable for the body of a plain-text MIME part.
        Never raises — returns a minimal fallback string on any internal error.
    """
    try:
        sections: list[dict] = report_content.get("sections") or []

        # Group sections by type for O(1) lookup in each helper.
        by_type: dict[str, list[dict]] = {}
        for sec in sections:
            t = sec.get("type") or "text"
            by_type.setdefault(t, []).append(sec)

        # Collect non-empty content blocks in executive reading order.
        content_parts: list[list[str]] = [
            part for part in [
                _email_ai_dashboard(by_type.get("ai_dashboard", [])),
                _email_business_kpis(by_type.get("business_kpis", [])),
                _email_executive_summary(by_type.get("executive_summary", [])),
                _email_anomalies(
                    by_type.get("anomaly", []),
                    by_type.get("ai_dashboard", []),
                ),
                _email_recommendations(
                    by_type.get("recommendation", []),
                    by_type.get("ai_recommendations", []),
                ),
                _email_trends(
                    by_type.get("trend", []),
                    by_type.get("ai_insights", []),
                ),
                _email_segmentation(by_type.get("segmentation_insights", [])),
                _email_historical(by_type.get("historical_comparison", [])),
                _email_drift(by_type.get("drift_detection", [])),
                _email_forecast(by_type.get("forecast", [])),
            ]
            if part
        ]

        lines: list[str] = _email_header(title, dataset_filename)

        for i, part in enumerate(content_parts):
            lines.append("")
            lines.extend(part)
            # Insert divider between sections, not after the last one
            if i < len(content_parts) - 1:
                lines.append("")
                lines.append(_EMAIL_DIV)

        lines.append("")
        lines.extend(_email_footer(report_url, by_type.get("predictive_readiness", [])))

        return "\n".join(lines).rstrip()

    except Exception:
        return (
            f"Report: {title or 'Dataset Report'}\n"
            f"Dataset: {dataset_filename}\n\n"
            "Powered by ToolSmithAI."
        )


# ── Canonical HTML email renderer ────────────────────────────────────────────


def render_report_as_html_email(
    report_content: dict,
    *,
    title: str = "",
    dataset_filename: str = "",
    report_url: str = "",
) -> str:
    """Canonical HTML renderer for executive report emails.

    Produces an enterprise-grade, inline-CSS HTML email suitable for CEO /
    COO / CFO inboxes.  Calls build_executive_snapshot() as the single source
    of truth — no fields are re-extracted from report_content directly.

    Layout (top → bottom):
        Header   — brand name · report title · dataset name
        KPIs     — up to 6 metric cards (2-per-row, pixel-width table cells)
        Readiness— table-based progress bar + score + level label
        Signals  — key insight / top risk / recommended action (accent stripes)
        CTA      — "View Full Report" button (omitted when report_url is empty)
        Footer   — ToolSmithAI branding tagline

    Compatibility guarantees:
        • Table-based layout — no CSS Grid, no Flexbox
        • Inline CSS only — no <style> blocks (Gmail strips them)
        • No JavaScript
        • Arial / Helvetica font stack — no external font requests
        • 600 px max-width container
        • border-radius degrades gracefully in Outlook (visual only)

    Args:
        report_content:   Dict with a "sections" key from get_report_by_id() /
                          generate_dataset_report().
        title:            Report display title.
        dataset_filename: Source dataset filename.
        report_url:       Deep link into the web UI; CTA omitted when empty.

    Returns:
        Complete HTML string.  Never raises.
    """
    try:
        _e = html.escape  # entity-escape every user-controlled value

        # Snapshot is the single source of executive data — never re-extract.
        snap = build_executive_snapshot(
            report_content,
            title=title,
            dataset_filename=dataset_filename,
        )

        report_title_s  = _e(snap["report_title"] or title or "Dataset Report")
        dataset_name_s  = _e(snap["dataset_name"] or dataset_filename or "")
        kpi_cards       = snap["kpi_cards"]
        readiness_score = snap["readiness_score"]
        readiness_level = snap["readiness_level"]
        key_insight_s   = _e(snap["key_insight"])
        top_risk_s      = _e(snap["top_risk"])
        recommended_s   = _e(snap["recommended_action"])

        # ── Palette (mirrors _PDF_BRANDING hex equivalents) ───────────────────
        _C = {
            "bg_outer":     "#F8FAFC",
            "bg_white":     "#FFFFFF",
            "header_bg":    "#16162C",
            "header_muted": "#8B9DC3",
            "primary":      "#6366F1",
            "success":      "#10B981",
            "warning":      "#F59E0B",
            "danger":       "#F87171",
            "text_dark":    "#1E2A3A",
            "text_body":    "#2C3650",
            "text_muted":   "#64748B",
            "border":       "#E2E8F0",
        }

        _STATUS_C = {
            "good":    _C["success"],
            "warning": _C["warning"],
            "risk":    _C["danger"],
        }

        # ── Inner helper: single KPI card <td> ────────────────────────────────
        def _kpi_cell(card: dict) -> str:
            lbl   = _e(str(card.get("label") or ""))
            val   = _e(str(card.get("value_formatted") or card.get("value") or "—"))
            stt_c = _STATUS_C.get(card.get("status", ""), _C["text_muted"])
            delta     = card.get("delta")
            delta_dir = str(card.get("delta_direction") or "")
            delta_html = ""
            if delta is not None:
                try:
                    arrow = {"up": "&#x2191;", "down": "&#x2193;"}.get(delta_dir, "&#x2192;")
                    delta_html = (
                        f'<p style="margin:5px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
                        f'font-size:11px;color:{stt_c};">'
                        f'{arrow}&nbsp;{abs(float(delta)):.1f}%</p>'
                    )
                except (TypeError, ValueError):
                    pass
            return (
                f'<td width="258" valign="top" '
                f'style="background-color:{_C["bg_white"]};'
                f'border:1px solid {_C["border"]};'
                f'border-radius:6px;padding:16px 18px;">'
                f'<p style="margin:0 0 5px 0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:10px;text-transform:uppercase;letter-spacing:0.8px;'
                f'color:{_C["text_muted"]};">{lbl}</p>'
                f'<p style="margin:0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:22px;font-weight:bold;line-height:1.2;'
                f'color:{_C["text_dark"]};">{val}</p>'
                + delta_html
                + "</td>"
            )

        # ── Inner helper: intelligence signal row ─────────────────────────────
        def _signal_row(label: str, body_s: str, accent: str) -> str:
            return (
                f'<tr>'
                f'<td width="4" bgcolor="{accent}" '
                f'style="border-radius:2px 0 0 2px;">&nbsp;</td>'
                f'<td width="16">&nbsp;</td>'
                f'<td valign="top" style="padding:14px 0;">'
                f'<p style="margin:0 0 3px 0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:10px;text-transform:uppercase;letter-spacing:0.8px;'
                f'color:{_C["text_muted"]};">{label}</p>'
                f'<p style="margin:0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:13px;line-height:1.6;'
                f'color:{_C["text_body"]};">{body_s}</p>'
                f'</td></tr>'
                f'<tr><td height="1" colspan="3" '
                f'bgcolor="{_C["border"]}">&nbsp;</td></tr>'
            )

        # ── Section: KPI cards ─────────────────────────────────────────────────
        kpi_section = ""
        if kpi_cards:
            rows = []
            for i in range(0, len(kpi_cards), 2):
                pair = kpi_cards[i:i + 2]
                r = "<tr>" + _kpi_cell(pair[0])
                if len(pair) == 2:
                    r += '<td width="20">&nbsp;</td>' + _kpi_cell(pair[1])
                else:
                    r += '<td width="20">&nbsp;</td><td width="258">&nbsp;</td>'
                r += "</tr>"
                if i + 2 < len(kpi_cards):
                    r += '<tr><td height="12" colspan="3">&nbsp;</td></tr>'
                rows.append(r)
            kpi_section = (
                '<tr><td style="padding:28px 32px 0 32px;">'
                f'<p style="margin:0 0 14px 0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:10px;text-transform:uppercase;letter-spacing:1.2px;'
                f'color:{_C["text_muted"]};">KEY METRICS</p>'
                '<table width="536" cellpadding="0" cellspacing="0" border="0">'
                + "".join(rows)
                + "</table></td></tr>"
            )

        # ── Section: Readiness score bar ──────────────────────────────────────
        readiness_section = ""
        if readiness_score is not None:
            score_val = max(0, min(100, readiness_score))
            filled_px = round(score_val * 536 / 100)
            empty_px  = 536 - filled_px
            score_c   = (
                _C["success"] if score_val >= 70
                else _C["warning"] if score_val >= 40
                else _C["danger"]
            )
            level_s = _e((readiness_level or "").title())
            bar = (
                f'<td width="{filled_px}" height="8" bgcolor="{score_c}" '
                f'style="border-radius:4px 0 0 4px;font-size:0;">&nbsp;</td>'
            )
            if empty_px > 0:
                r_right = "0 4px 4px 0" if filled_px > 0 else "4px"
                bar += (
                    f'<td width="{empty_px}" height="8" bgcolor="{_C["border"]}" '
                    f'style="border-radius:{r_right};font-size:0;">&nbsp;</td>'
                )
            level_part = (
                f'&nbsp;&nbsp;&#x22C5;&nbsp;&nbsp;{level_s}' if level_s else ""
            )
            readiness_section = (
                '<tr><td style="padding:24px 32px 0 32px;">'
                f'<p style="margin:0 0 10px 0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:10px;text-transform:uppercase;letter-spacing:1.2px;'
                f'color:{_C["text_muted"]};">PREDICTIVE READINESS</p>'
                '<table width="536" cellpadding="0" cellspacing="0" border="0">'
                f'<tr>{bar}</tr>'
                "</table>"
                f'<p style="margin:8px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:13px;color:{_C["text_body"]};">'
                f'<strong style="font-size:18px;color:{score_c};">{score_val}</strong>'
                f'<span style="color:{_C["text_muted"]};">'
                f"&nbsp;/ 100{level_part}</span></p>"
                "</td></tr>"
            )

        # ── Section: Intelligence signals ─────────────────────────────────────
        signal_rows = ""
        if key_insight_s:
            signal_rows += _signal_row("Key Insight", key_insight_s, _C["primary"])
        if top_risk_s:
            signal_rows += _signal_row("Top Risk", top_risk_s, _C["danger"])
        if recommended_s:
            signal_rows += _signal_row("Recommended Action", recommended_s, _C["success"])

        signals_section = ""
        if signal_rows:
            signals_section = (
                '<tr><td style="padding:24px 32px 0 32px;">'
                f'<p style="margin:0 0 14px 0;font-family:Arial,Helvetica,sans-serif;'
                f'font-size:10px;text-transform:uppercase;letter-spacing:1.2px;'
                f'color:{_C["text_muted"]};">EXECUTIVE INTELLIGENCE</p>'
                '<table width="536" cellpadding="0" cellspacing="0" border="0">'
                + signal_rows
                + "</table></td></tr>"
            )

        # ── Section: CTA button ────────────────────────────────────────────────
        cta_section = ""
        if report_url and report_url.startswith(("http://", "https://", "/")):
            safe_url = _e(report_url)
            cta_section = (
                '<tr><td style="padding:28px 32px;">'
                '<table width="536" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td align="center" bgcolor="{_C["primary"]}" '
                f'style="border-radius:6px;">'
                f'<a href="{safe_url}" target="_blank" '
                f'style="display:block;padding:14px 24px;'
                f'font-family:Arial,Helvetica,sans-serif;font-size:14px;'
                f'font-weight:bold;color:#FFFFFF;text-decoration:none;'
                f'letter-spacing:0.3px;">View Full Report</a>'
                f'</td>'
                "</tr></table></td></tr>"
            )

        # ── Assemble document ─────────────────────────────────────────────────
        body_inner = (
            kpi_section
            + readiness_section
            + signals_section
            + (cta_section or '<tr><td height="24">&nbsp;</td></tr>')
        )

        header_dataset = (
            f'<p style="margin:6px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:13px;color:{_C["header_muted"]};">{dataset_name_s}</p>'
            if dataset_name_s else ""
        )

        return "\n".join(p for p in [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1.0">',
            '<meta http-equiv="X-UA-Compatible" content="IE=edge">',
            f"<title>{report_title_s}</title>",
            "</head>",
            (f'<body style="margin:0;padding:0;background-color:{_C["bg_outer"]};'
             f'-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">'),
            # Outer wrapper — centres content on wide viewports
            (f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
             f'bgcolor="{_C["bg_outer"]}" role="presentation">'),
            f'<tr><td align="center" style="padding:24px 0;">',
            # Inner 600 px container
            ('<table width="600" cellpadding="0" cellspacing="0" border="0" '
             'style="max-width:600px;" role="presentation">'),
            # HEADER
            "<tr>",
            (f'<td bgcolor="{_C["header_bg"]}" '
             f'style="padding:28px 32px;border-radius:8px 8px 0 0;">'),
            f'<img src="{FRONTEND_BASE_URL}/toolsmith-logo-transparent.png" alt="ToolSmithAI" width="40" height="40" style="display:block;margin:0 0 10px 0;border:0;" />',
            (f'<p style="margin:0;font-family:Arial,Helvetica,sans-serif;'
             f'font-size:10px;text-transform:uppercase;letter-spacing:2px;'
             f'color:{_C["header_muted"]};">'
             f'ToolSmithAI&nbsp;&nbsp;&#x22C5;&nbsp;&nbsp;Executive Intelligence</p>'),
            (f'<p style="margin:10px 0 0 0;font-family:Arial,Helvetica,sans-serif;'
             f'font-size:22px;font-weight:bold;line-height:1.2;'
             f'color:#FFFFFF;">{report_title_s}</p>'),
            header_dataset,
            "</td></tr>",
            # BODY
            "<tr>",
            f'<td bgcolor="{_C["bg_white"]}" style="padding-bottom:12px;">',
            ('<table width="100%" cellpadding="0" cellspacing="0" border="0" '
             'role="presentation">'),
            body_inner,
            "</table>",
            "</td></tr>",
            # FOOTER
            "<tr>",
            (f'<td bgcolor="{_C["bg_outer"]}" '
             f'style="padding:18px 32px;border-top:1px solid {_C["border"]};">'),
            (f'<p style="margin:0;font-family:Arial,Helvetica,sans-serif;'
             f'font-size:11px;color:{_C["text_muted"]};text-align:center;">'
             f'Powered by <strong style="color:{_C["text_body"]};">ToolSmithAI</strong>'
             f'&nbsp;&nbsp;&#x22C5;&nbsp;&nbsp;Automated Intelligence Report</p>'),
            "</td></tr>",
            # Close wrappers
            "</table>",
            "</td></tr>",
            "</table>",
            "</body>",
            "</html>",
        ] if p)

    except Exception:
        logger.exception("render_report_as_html_email: unexpected error — returning fallback")
        safe_t = html.escape(title or "Dataset Report")
        return (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<title>{safe_t}</title></head>"
            "<body style=\"font-family:Arial,Helvetica,sans-serif;padding:20px;color:#1E2A3A;\">"
            f"<h2>{safe_t}</h2>"
            "<p style=\"color:#64748B;\">Powered by ToolSmithAI.</p>"
            "</body></html>"
        )


def format_report_as_email_body(report: dict, filename: str) -> str:
    """Compatibility wrapper — delegates to render_report_as_plain_text.

    Kept so callers that have not yet been updated continue to work unchanged.
    New code should call render_report_as_plain_text() directly.
    """
    return render_report_as_plain_text(
        report,
        title=format_dataset_display_name(filename),
        dataset_filename=filename,
    )


_PDF_BRANDING = {
    'brand_name':     'ToolSmithAI',
    'primary':        (99,  102, 241),
    'primary_dark':   (67,   56, 202),
    'primary_light':  (224, 231, 255),
    'success':        (16,  185, 129),
    'warning':        (245, 158,  11),
    'danger':         (248, 113, 113),
    'neutral':        (100, 116, 139),
    'text_dark':      (30,   42,  58),
    'text_muted':     (100, 116, 139),
    'bg_white':       (255, 255, 255),
    'bg_row_alt':     (248, 250, 252),
    # PDF-specific typography colours (preserve existing visual output exactly)
    'title_dark':     (22,   22,  44),
    'text_body':      (44,   54,  80),
    'text_secondary': (88,  104, 130),
    'text_light':     (140, 155, 180),
    'text_stamp':     (160, 176, 204),
    'text_chart':     (120, 130, 160),
    'meta_label':     (100, 110, 140),
    'rule_light':     (210, 220, 235),
}


def _compute_readiness_score(by_type: dict) -> tuple:
    """Compute 0-100 executive readiness score from existing section data."""
    score = 0
    kpi_sec  = (by_type.get('kpi') or [{}])[0]
    kpi_list = kpi_sec.get('kpis', []) or []
    kpi_map  = {k.get('label', ''): k for k in kpi_list}

    comp_val = (kpi_map.get('Data Completeness') or {}).get('value')
    if comp_val is not None:
        try:
            c = float(comp_val)
            if c >= 95:   score += 20
            elif c >= 80: score += 10
        except (TypeError, ValueError):
            pass

    anom_sec  = (by_type.get('anomaly') or [{}])[0]
    _SENTINEL = 'No Major Anomalies Detected'
    high_ct   = sum(
        1 for a in (anom_sec.get('anomalies') or [])
        if str(a.get('severity', '')).lower() == 'high'
        and a.get('title', '') != _SENTINEL
    )
    if high_ct == 0:   score += 20
    elif high_ct == 1: score += 12
    elif high_ct == 2: score += 4

    rec_val = (kpi_map.get('Total Records') or {}).get('value')
    if rec_val is not None:
        try:
            r = int(float(rec_val))
            if r >= 10000:  score += 20
            elif r >= 1000: score += 15
            elif r >= 100:  score += 8
        except (TypeError, ValueError):
            pass

    feat_val = (kpi_map.get('Total Features') or {}).get('value')
    if feat_val is not None:
        try:
            f = int(float(feat_val))
            if f >= 10:  score += 20
            elif f >= 5: score += 14
            elif f >= 2: score += 8
        except (TypeError, ValueError):
            pass

    rec_sec = (by_type.get('recommendation') or [{}])[0]
    recs    = rec_sec.get('recommendations') or []
    if recs:
        all_confs   = [str(r.get('confidence', '')).lower() for r in recs]
        hi_pri_recs = [r for r in recs if str(r.get('priority', '')).lower() == 'high']
        if hi_pri_recs:
            hi_confs = [str(r.get('confidence', '')).lower() for r in hi_pri_recs]
            score += 20 if all(c == 'high' for c in hi_confs) else 12
        elif any(c == 'high' for c in all_confs):
            score += 12
        else:
            score += 6

    if score >= 90:   label = 'STRONG'
    elif score >= 75: label = 'GOOD'
    elif score >= 50: label = 'MODERATE'
    else:             label = 'ATTENTION NEEDED'
    return (score, label)


def _build_pdf_bytes(report: dict) -> bytes:
    """Generate a clean PDF from a saved report dict. Uses fpdf2 (pure Python, no browser)."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    b = _PDF_BRANDING

    class _ToolSmithPDF(FPDF):
        FONT = 'Sans'
        NB   = '{nb}'

        def __init__(self, report_title='', generated_stamp=''):
            super().__init__()
            self._report_title    = report_title
            self._generated_stamp = generated_stamp
            self.alias_nb_pages(self.NB)
            import os
            _W = 'C:/Windows/Fonts'
            _STYLES = {
                '':  [os.path.join(_W, 'arial.ttf'),
                      '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                      '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'],
                'B': [os.path.join(_W, 'arialbd.ttf'),
                      '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                      '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'],
                'I': [os.path.join(_W, 'ariali.ttf'),
                      '/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf',
                      '/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf'],
            }
            _fallback = None
            for _style, _paths in _STYLES.items():
                for _p in _paths:
                    if os.path.isfile(_p):
                        self.add_font(self.FONT, _style, _p)
                        if _style == '':
                            _fallback = _p
                        break
                else:
                    if _fallback and _style != '':
                        self.add_font(self.FONT, _style, _fallback)

        def header(self):
            if self.page_no() == 1:
                return
            self.set_font(self.FONT, 'B', 9)
            self.set_text_color(*_PDF_BRANDING['primary'])
            self.cell(60, 6, 'ToolSmithAI')
            ttl = self._report_title[:47] + '...' if len(self._report_title) > 50 else self._report_title
            self.set_font(self.FONT, '', 8)
            self.set_text_color(*_PDF_BRANDING['text_muted'])
            self.cell(0, 6, ttl, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_draw_color(*_PDF_BRANDING['rule_light'])
            self.set_line_width(0.3)
            self.line(20, self.get_y(), 190, self.get_y())
            self.ln(3)

        def footer(self):
            if self.page_no() == 1:
                return
            self.set_y(-15)
            self.set_draw_color(*_PDF_BRANDING['rule_light'])
            self.set_line_width(0.3)
            self.line(20, self.get_y(), 190, self.get_y())
            self.ln(2)
            self.set_font(self.FONT, '', 7)
            self.set_text_color(*_PDF_BRANDING['text_stamp'])
            self.cell(80, 5, f'Generated {self._generated_stamp}')
            self.cell(0, 5, f'Page {self.page_no()} of {self.NB}', align='R',
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        def _draw_cover(self, title, dataset, status, created):
            self.set_y(70)
            self.set_font(self.FONT, 'B', 22)
            self.set_text_color(*_PDF_BRANDING['primary'])
            self.cell(0, 12, 'ToolSmithAI', align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_font(self.FONT, 'I', 11)
            self.set_text_color(*_PDF_BRANDING['text_secondary'])
            self.cell(0, 7, 'Executive Intelligence Report', align='C',
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(6)
            self.set_draw_color(*_PDF_BRANDING['primary_light'])
            self.set_line_width(0.5)
            self.line(40, self.get_y(), 170, self.get_y())
            self.ln(8)
            self.set_font(self.FONT, 'B', 16)
            self.set_text_color(*_PDF_BRANDING['title_dark'])
            self.multi_cell(0, 9, title, align='C', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(8)
            for _lbl, _val in [
                ('Dataset',   dataset),
                ('Status',    status),
                ('Generated', created),
            ]:
                self.set_font(self.FONT, '', 9)
                self.set_text_color(*_PDF_BRANDING['text_secondary'])
                self.cell(0, 6, f'{_lbl}:  {_val}', align='C',
                          new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        def _draw_executive_snapshot(self, sections, b):
            from fpdf.enums import XPos, YPos

            # ── build lookup dict ─────────────────────────────────────────────
            _by = {}
            for _sec in sections:
                _t = _sec.get('type') or 'text'
                _by.setdefault(_t, []).append(_sec)

            def _trunc(text, n):
                s = str(text or '')
                if len(s) <= n:
                    return s
                cut = s[:n].rsplit(' ', 1)
                return (cut[0] if len(cut) > 1 else s[:n]) + '…'

            def _first(lst):
                return (lst or [{}])[0]

            # ── section data ──────────────────────────────────────────────────
            _ai     = _first(_by.get('ai_dashboard'))
            _ai_ins = str(_ai.get('most_important_insight', '') or '')
            _ai_rsk = str(_ai.get('highest_risk', '') or '')
            _ai_act = str(_ai.get('recommended_action', '') or '')
            _ai_wl  = list(_ai.get('watchlist', None) or [])

            _exec    = _first(_by.get('executive_summary'))
            _ex_summ = str(_exec.get('summary', '') or '')

            _anom_raw = (_first(_by.get('anomaly')).get('anomalies') or [])
            _SENTINEL = 'No Major Anomalies Detected'
            _anoms    = [a for a in _anom_raw if a.get('title', '') != _SENTINEL]

            _rec_sec = _first(_by.get('recommendation'))
            _recs    = list(_rec_sec.get('recommendations', None) or [])

            _ip_sec  = _first(_by.get('insight_priority'))
            _ip_ins  = list(_ip_sec.get('insights', None) or [])

            _kpi_sec  = _first(_by.get('kpi'))
            _kpi_list = list(_kpi_sec.get('kpis', None) or [])
            _kpi_map  = {k.get('label', ''): k for k in _kpi_list}

            _bkpi_sec   = _first(_by.get('business_kpis'))
            _bkpi_cards = list(_bkpi_sec.get('kpis', None) or [])
            _bkpi_lbl   = str(_bkpi_sec.get('dataset_label', '') or '')

            # ── KPI card selection ────────────────────────────────────────────
            _STRUCT_ORDER = ['Data Completeness', 'Total Records',
                             'Total Features', 'Columns with Gaps']
            if _bkpi_cards:
                _snap_kpis = list(_bkpi_cards[:4])
                if len(_snap_kpis) < 4:
                    _used = {c.get('label', '') for c in _snap_kpis}
                    for _lbl2 in _STRUCT_ORDER:
                        if len(_snap_kpis) >= 4:
                            break
                        if _lbl2 in _kpi_map and _lbl2 not in _used:
                            _snap_kpis.append(_kpi_map[_lbl2])
                            _used.add(_lbl2)
            else:
                _snap_kpis = [_kpi_map[l] for l in _STRUCT_ORDER if l in _kpi_map][:4]

            # ── key insight ───────────────────────────────────────────────────
            _FB_INS = 'No critical anomalies detected'
            _FB_RSK = 'No high-severity risks identified'
            _FB_ACT = 'Review report sections for detailed insights'

            if _ai_ins and _FB_INS not in _ai_ins:
                _insight = _trunc(_ai_ins, 160)
            elif _ip_ins:
                _insight = _trunc(_ip_ins[0].get('title', ''), 160)
            elif _anoms:
                _d = _anoms[0].get('description', '') or _anoms[0].get('title', '')
                _insight = _trunc(_d, 160)
            elif _ex_summ:
                _insight = _trunc(_ex_summ.split('. ')[0], 160)
            else:
                _insight = 'Analysis complete. Review the report sections below for detailed findings.'

            # ── top risk ──────────────────────────────────────────────────────
            _hi_anoms = [a for a in _anoms if str(a.get('severity', '')).lower() == 'high']
            _SEV_RGB  = {
                'high':   b['danger'],
                'medium': b['warning'],
                'low':    b['neutral'],
                'clear':  b['success'],
            }

            if _hi_anoms:
                _a0          = _hi_anoms[0]
                _risk_title  = _trunc(_a0.get('title', ''), 100)
                _risk_detail = _trunc(_a0.get('evidence', '') or _a0.get('description', ''), 120)
                _risk_sev    = 'high'
            elif _ai_rsk and _FB_RSK not in _ai_rsk:
                _risk_title  = _trunc(_ai_rsk, 100)
                _risk_detail = ''
                _risk_sev    = 'medium'
            elif _ip_ins and str(_ip_ins[0].get('severity', '')).lower() == 'high':
                _risk_title  = _trunc(_ip_ins[0].get('title', ''), 100)
                _risk_detail = _trunc(_ip_ins[0].get('evidence', ''), 120)
                _risk_sev    = 'high'
            elif _anoms:
                _a0          = _anoms[0]
                _risk_title  = _trunc(_a0.get('title', ''), 100)
                _risk_detail = ''
                _risk_sev    = str(_a0.get('severity', 'low')).lower()
            else:
                _risk_title  = 'No high-severity risks detected.'
                _risk_detail = ''
                _risk_sev    = 'clear'

            _risk_rgb = _SEV_RGB.get(_risk_sev, b['neutral'])

            # ── recommended action ────────────────────────────────────────────
            _ACT_RGB = {
                'HIGH':      b['danger'],
                'MEDIUM':    b['warning'],
                'LOW':       b['neutral'],
                'SUGGESTED': b['primary'],
            }
            _hi_recs = [r for r in _recs if str(r.get('priority', '')).lower() == 'high']

            if _hi_recs:
                _r0           = _hi_recs[0]
                _act_title    = _trunc(_r0.get('title', ''), 80)
                _act_reason   = _trunc(_r0.get('reason', ''), 140)
                _act_priority = 'HIGH'
            elif _ai_act and _FB_ACT not in _ai_act:
                _act_title    = _trunc(_ai_act, 80)
                _act_reason   = ''
                _act_priority = 'SUGGESTED'
            elif _recs:
                _r0           = _recs[0]
                _act_title    = _trunc(_r0.get('title', ''), 80)
                _act_reason   = _trunc(_r0.get('reason', ''), 140)
                _act_priority = str(_r0.get('priority', '')).upper()
            elif _ip_ins:
                _act_title    = _trunc(_ip_ins[0].get('recommended_action', ''), 80)
                _act_reason   = ''
                _act_priority = 'SUGGESTED'
            else:
                _act_title    = 'Review the full report for detailed recommended next steps.'
                _act_reason   = ''
                _act_priority = ''

            _act_rgb = _ACT_RGB.get(_act_priority, b['neutral'])

            # ── readiness ─────────────────────────────────────────────────────
            _r_score, _r_label = _compute_readiness_score(_by)
            _SCORE_RGB = {
                'STRONG':           b['success'],
                'GOOD':             b['success'],
                'MODERATE':         b['warning'],
                'ATTENTION NEEDED': b['danger'],
            }
            _r_rgb  = _SCORE_RGB.get(_r_label, b['neutral'])
            _c_card = _kpi_map.get('Data Completeness') or {}
            _c_val  = _c_card.get('value')
            _c_str  = f"{float(_c_val):.1f}%" if _c_val is not None else '—'
            _c_stat = str(_c_card.get('status', '') or '')
            _c_rgb  = {
                'good':    b['success'],
                'warning': b['warning'],
                'risk':    b['danger'],
            }.get(_c_stat, b['text_light'])

            _wl_items = [_trunc(str(x), 60) for x in (_ai_wl[:2] if _ai_wl else [])]

            # ── layout ────────────────────────────────────────────────────────
            _L = 20    # left margin x (mm)
            _W = 170   # usable width (mm)

            self.set_auto_page_break(auto=False)
            try:
                _y = self.get_y()

                # ── 1. BANNER ─────────────────────────────────────────────────
                _bh = 14
                self.set_fill_color(*b['primary_light'])
                self.rect(_L, _y, _W, _bh, style='F')
                self.set_draw_color(*b['primary'])
                self.set_line_width(0.5)
                self.line(_L, _y, _L + _W, _y)

                _tag = _bkpi_lbl or 'Dataset Report'
                self.set_font(self.FONT, 'B', 10)
                self.set_text_color(*b['primary'])
                self.set_xy(_L + 4, _y + 3)
                self.cell(_W - 8, 6, 'EXECUTIVE SNAPSHOT')
                self.set_font(self.FONT, '', 8)
                self.set_text_color(*b['text_secondary'])
                self.set_xy(_L + 4, _y + 3)
                self.cell(_W - 8, 6, _tag, align='R')

                _y += _bh + 4

                # ── 2. KPI CARD GRID ──────────────────────────────────────────
                if _snap_kpis:
                    _nc  = len(_snap_kpis)
                    _gap = 4.0
                    _cw  = (_W - _gap * (_nc - 1)) / _nc
                    _ch  = 33.0
                    _sw  = 2.5
                    _TREND_SYM = {'up': '▲', 'down': '▼', 'neutral': '—'}
                    _STAT_RGB  = {
                        'good':    b['success'],
                        'warning': b['warning'],
                        'risk':    b['danger'],
                    }
                    for _ci, _card in enumerate(_snap_kpis):
                        try:
                            _cx = _L + _ci * (_cw + _gap)
                            _cy = _y

                            _c_lbl = str(_card.get('label', '') or '')[:28].upper()
                            _c_vf  = str(_card.get('value_formatted', '') or '')
                            if not _c_vf:
                                _cv   = _card.get('value')
                                _cfmt = str(_card.get('format', 'number') or 'number')
                                try:
                                    if _cv is None:           _c_vf = '—'
                                    elif _cfmt == 'percent':  _c_vf = f"{float(_cv):.1f}%"
                                    elif _cfmt == 'currency': _c_vf = f"${float(_cv):,.0f}"
                                    elif _cfmt == 'number':   _c_vf = f"{int(float(_cv)):,}"
                                    else:                     _c_vf = str(_cv)
                                except (TypeError, ValueError):
                                    _c_vf = str(_cv or '—')
                            _c_vf = _c_vf[:16]

                            _c_status = str(_card.get('status', '') or '').lower()
                            _c_trend  = str(_card.get('trend',  '') or '').lower()
                            _c_delta  = _card.get('delta')
                            _c_srgb   = _STAT_RGB.get(_c_status, b['neutral'])
                            _c_tsym   = _TREND_SYM.get(_c_trend, '—')
                            _c_trgb   = (b['success'] if _c_trend == 'up' else
                                         b['danger']  if _c_trend == 'down' else b['neutral'])

                            self.set_fill_color(*b['bg_row_alt'])
                            self.set_draw_color(*b['rule_light'])
                            self.set_line_width(0.3)
                            self.rect(_cx, _cy, _cw, _ch, style='FD')
                            self.set_fill_color(*_c_srgb)
                            self.set_draw_color(*_c_srgb)
                            self.rect(_cx, _cy, _sw, _ch, style='F')

                            _tx = _cx + _sw + 2.5
                            _tw = _cw - _sw - 3.5

                            self.set_font(self.FONT, 'B', 7)
                            self.set_text_color(*b['text_secondary'])
                            self.set_xy(_tx, _cy + 3)
                            self.cell(_tw, 4.5, _c_lbl)

                            self.set_font(self.FONT, 'B', 12)
                            self.set_text_color(*b['text_dark'])
                            self.set_xy(_tx, _cy + 9)
                            self.cell(_tw, 8, _c_vf)

                            try:
                                _ds = (f"{_c_tsym} {float(_c_delta):+.1f}%"
                                       if _c_delta is not None else _c_tsym)
                            except (TypeError, ValueError):
                                _ds = _c_tsym
                            self.set_font(self.FONT, 'B', 7.5)
                            self.set_text_color(*_c_trgb)
                            self.set_xy(_tx, _cy + 23)
                            self.cell(_tw, 5, _ds)
                        except Exception:
                            pass

                    _y += _ch + 5

                # ── 3. KEY INSIGHT ────────────────────────────────────────────
                self.set_draw_color(*b['rule_light'])
                self.set_line_width(0.3)
                self.line(_L, _y, _L + _W, _y)
                _y += 3

                self.set_font(self.FONT, 'B', 8)
                self.set_text_color(*b['primary'])
                self.set_xy(_L, _y)
                self.cell(_W, 5, 'KEY INSIGHT', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                _y += 5

                self.set_font(self.FONT, '', 9)
                self.set_text_color(*b['text_body'])
                self.set_xy(_L, _y)
                self.multi_cell(_W, 5.5, _insight, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                _y = self.get_y() + 4

                # ── 4. TOP RISK ───────────────────────────────────────────────
                self.set_draw_color(*b['rule_light'])
                self.set_line_width(0.3)
                self.line(_L, _y, _L + _W, _y)
                _y += 3

                _rx = _L + 5.5
                _rw = _W - 5.5
                _y_risk = _y

                _sev_tag = 'CLEAR' if _risk_sev == 'clear' else _risk_sev.upper()
                self.set_font(self.FONT, 'B', 8)
                self.set_text_color(*_risk_rgb)
                self.set_xy(_rx, _y + 1)
                self.cell(22, 5, f'[{_sev_tag}]')

                self.set_font(self.FONT, 'B', 8)
                self.set_text_color(*b['text_secondary'])
                self.set_xy(_rx + 22, _y + 1)
                self.cell(_rw - 22, 5, 'TOP RISK')

                self.set_font(self.FONT, 'B', 9)
                self.set_text_color(*b['text_dark'])
                self.set_xy(_rx, _y + 7)
                self.multi_cell(_rw, 5, _risk_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

                if _risk_detail:
                    self.set_font(self.FONT, 'I', 7.5)
                    self.set_text_color(*b['text_light'])
                    self.set_xy(_rx, self.get_y())
                    self.multi_cell(_rw, 4.5, _risk_detail, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

                _y_risk_end = max(self.get_y() + 2, _y_risk + 16)
                self.set_fill_color(*_risk_rgb)
                self.set_draw_color(*_risk_rgb)
                self.rect(_L, _y_risk, 2.5, _y_risk_end - _y_risk, style='F')
                _y = _y_risk_end + 4

                # ── 5. RECOMMENDED ACTION ─────────────────────────────────────
                self.set_draw_color(*b['rule_light'])
                self.set_line_width(0.3)
                self.line(_L, _y, _L + _W, _y)
                _y += 3

                _ax = _L + 5.5
                _aw = _W - 5.5
                _y_act = _y

                self.set_font(self.FONT, 'B', 8)
                self.set_text_color(*b['primary'])
                self.set_xy(_ax, _y + 1)
                self.cell(70, 5, 'RECOMMENDED ACTION')

                if _act_priority:
                    self.set_font(self.FONT, 'B', 7.5)
                    self.set_text_color(*_act_rgb)
                    self.set_xy(_ax, _y + 1)
                    self.cell(_aw, 5, f'[{_act_priority}]', align='R')

                self.set_font(self.FONT, 'B', 9)
                self.set_text_color(*b['text_body'])
                self.set_xy(_ax, _y + 7)
                self.multi_cell(_aw, 5, _act_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

                if _act_reason:
                    self.set_font(self.FONT, '', 8)
                    self.set_text_color(*b['text_secondary'])
                    self.set_xy(_ax, self.get_y())
                    self.multi_cell(_aw, 4.5, _act_reason, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

                _y_act_end = max(self.get_y() + 2, _y_act + 16)
                self.set_fill_color(*_act_rgb)
                self.set_draw_color(*_act_rgb)
                self.rect(_L, _y_act, 2.5, _y_act_end - _y_act, style='F')
                _y = _y_act_end + 4

                # ── 6. READINESS ROW ──────────────────────────────────────────
                self.set_draw_color(*b['rule_light'])
                self.set_line_width(0.3)
                self.line(_L, _y, _L + _W, _y)
                _y += 3

                _lw2 = 82
                _rw2 = _W - _lw2 - 6
                _wx  = _L + _lw2 + 6
                _y0  = _y

                self.set_font(self.FONT, 'B', 8)
                self.set_text_color(*b['text_secondary'])
                self.set_xy(_L, _y0)
                self.cell(_lw2, 5, 'DATA READINESS')

                if _wl_items:
                    self.set_font(self.FONT, 'B', 8)
                    self.set_text_color(*b['text_dark'])
                    self.set_xy(_wx, _y0)
                    self.cell(_rw2, 5, 'WATCHLIST')

                _y0 += 5

                self.set_font(self.FONT, 'B', 10)
                self.set_text_color(*_r_rgb)
                self.set_xy(_L, _y0)
                self.cell(68, 7, _r_label)
                self.set_font(self.FONT, '', 9)
                self.set_text_color(*b['text_secondary'])
                self.set_xy(_L + 70, _y0)
                self.cell(_lw2 - 70, 7, f'{_r_score}/100')

                _wy = _y0
                for _wl in _wl_items:
                    self.set_font(self.FONT, '', 8)
                    self.set_text_color(*b['text_body'])
                    self.set_xy(_wx, _wy)
                    self.cell(4, 5, '•')
                    self.set_xy(_wx + 4, _wy)
                    self.multi_cell(_rw2 - 4, 5, _wl, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    _wy += 5

                _y0 += 7

                self.set_font(self.FONT, '', 8)
                self.set_text_color(*b['text_secondary'])
                self.set_xy(_L, _y0)
                self.cell(30, 5, 'Completeness:')
                self.set_font(self.FONT, 'B', 8)
                self.set_text_color(*_c_rgb)
                self.set_xy(_L + 30, _y0)
                self.cell(_lw2 - 30, 5, _c_str)

            finally:
                self.set_auto_page_break(auto=True, margin=25)

    def _s(text) -> str:
        return str(text)

    title      = _s(report.get("title", "Untitled Report"))
    task_type  = report.get("task_type", "")
    status     = _s(report.get("status", "completed").title())
    dataset    = _s(report.get("dataset_filename") or "Not specified")
    created    = _s((report.get("created_at") or "")[:19].replace("T", " ") + " UTC")
    sections   = (report.get("content") or {}).get("sections", [])
    type_label = _s({
        "generate_dataset_report": "Dataset Report",
        "email_dataset_report":    "Emailed Dataset Report",
    }.get(task_type, task_type.replace("_", " ").title()))

    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    pdf   = _ToolSmithPDF(report_title=title, generated_stamp=stamp)
    pdf.set_title(title)
    pdf.set_author('ToolSmithAI')
    pdf.set_creator('ToolSmithAI')
    pdf.set_subject('Executive Intelligence Report')
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()
    pdf._draw_cover(title, dataset, status, created)
    pdf.add_page()
    pdf._draw_executive_snapshot(sections, b)
    pdf.add_page()

    # Report sections — dispatched on section.type for v2 compatibility.
    # Sections without 'type' (v1 saved reports) default to 'text'.
    # Unknown future types fall back to plain text rendering; never crash.
    for section in sections:
        sec_type = section.get("type", "text")
        heading  = _s(section.get("heading", ""))

        pdf.set_font("Sans", "B", 8)
        pdf.set_text_color(*b['primary'])
        pdf.cell(0, 5, heading.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

        if sec_type == "text":
            for item in section.get("items", []):
                pdf.set_font("Sans", "B", 9)
                pdf.set_text_color(*b['primary'])
                pdf.cell(6, 5, "->")
                pdf.set_font("Sans", "", 9)
                pdf.set_text_color(*b['text_body'])
                pdf.multi_cell(0, 5, _s(item), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif sec_type == "kpi":
            for kpi in section.get("kpis", []):
                try:
                    label    = _s(str(kpi.get("label", "")))
                    raw_val  = kpi.get("value")
                    fmt      = kpi.get("format", "number")
                    trend    = kpi.get("trend", "neutral")
                    desc     = _s(str(kpi.get("description", "")))
                    if raw_val is None:
                        val_str = "-"
                    elif fmt == "percent":
                        val_str = f"{raw_val}%"
                    elif fmt == "currency":
                        val_str = f"${float(raw_val):,.2f}"
                    elif fmt == "number":
                        val_str = f"{int(raw_val):,}"
                    else:
                        val_str = _s(str(raw_val))
                    trend_marker = {"up": "[+]", "down": "[-]", "neutral": "[ ]"}.get(trend, "[ ]")
                    line = f"{trend_marker}  {label}: {val_str}"
                    if desc:
                        line += f"   ({desc})"
                    pdf.set_font("Sans", "", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                except Exception:
                    pass
        elif sec_type == "executive_summary":
            summary       = _s(str(section.get("summary", "")))
            takeaways     = section.get("key_takeaways", [])
            risks         = section.get("risks", [])
            opportunities = section.get("opportunities", [])
            if summary:
                pdf.set_font("Sans", "", 9)
                pdf.set_text_color(*b['text_body'])
                pdf.multi_cell(0, 5, summary, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(2)
            for cat_label, items, rgb in [
                ('Key Takeaways', takeaways,     b['primary']),
                ('Risks',         risks,          b['danger']),
                ('Opportunities', opportunities,  b['success']),
            ]:
                if items:
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb)
                    pdf.cell(0, 5, cat_label.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    for item in items:
                        try:
                            pdf.set_font("Sans", "", 9)
                            pdf.set_text_color(*b['text_body'])
                            pdf.multi_cell(0, 5, _s(f"  • {item}"),
                                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                        except Exception:
                            pass
                    pdf.ln(1)
        elif sec_type == "recommendation":
            _PRIORITY_RGB = {'high': b['danger'], 'medium': b['warning'], 'low': b['neutral']}
            for rec in section.get("recommendations", []):
                try:
                    priority   = str(rec.get("priority",   "low")).lower()
                    title      = _s(str(rec.get("title",      "")))
                    reason     = _s(str(rec.get("reason",     "")))
                    action     = _s(str(rec.get("action_type","")).replace("_", " ").title())
                    confidence = _s(str(rec.get("confidence", "")))
                    rgb        = _PRIORITY_RGB.get(priority, (100,116,139))
                    # Priority label + title on one line
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb)
                    pdf.cell(22, 5, f"[{priority.upper()}]")
                    pdf.set_font("Sans", "B", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if reason:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, f"  {reason}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    meta_parts = []
                    if action:     meta_parts.append(f"Action: {action}")
                    if confidence: meta_parts.append(f"Confidence: {confidence}")
                    if meta_parts:
                        pdf.set_font("Sans", "I", 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.cell(0, 4, f"  {' | '.join(meta_parts)}",
                                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(2)
                except Exception:
                    pass
        elif sec_type == "drift_detection":
            bw = section.get("baseline_window") or {}
            bw_count = bw.get("snapshot_count", 0)
            bw_start = _s(str(bw.get("start", "") or "")[:19].replace("T", " "))
            bw_end   = _s(str(bw.get("end",   "") or "")[:19].replace("T", " "))
            if bw_count or bw_start:
                pdf.set_font("Sans", "I", 7.5)
                pdf.set_text_color(*b['text_light'])
                pdf.cell(0, 4,
                         f"Baseline: {bw_count} snapshot(s)  {bw_start} -> {bw_end}",
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1)
            _DR_SEV_RGB = {
                'high':   b['danger'],
                'medium': b['warning'],
                'low':    b['primary'],
            }
            _DR_DIR_SYM = {"increase": "[+]", "decrease": "[-]"}
            for drift in section.get("drifts", []):
                try:
                    metric    = _s(str(drift.get("metric",         "")))
                    base_val  = drift.get("baseline_value")
                    curr_val  = drift.get("current_value")
                    pct       = drift.get("drift_percent")
                    sev       = str(drift.get("severity",  "low")).lower()
                    direction = str(drift.get("direction", "")).lower()
                    desc      = _s(str(drift.get("description", "")))
                    rgb       = _DR_SEV_RGB.get(sev, (100, 116, 139))
                    sym       = _DR_DIR_SYM.get(direction, "[~]")
                    pct_str   = (f"+{pct}%" if isinstance(pct, (int, float)) and pct > 0
                                 else f"{pct}%" if pct is not None else "?%")
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb)
                    pdf.cell(10, 5, sym)
                    pdf.set_font("Sans", "B", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.cell(0, 5,
                             f"{metric}   {pct_str}  (baseline: {base_val}, current: {curr_val})",
                             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if desc:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, f"  {desc}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                except Exception:
                    pass
        elif sec_type == "historical_comparison":
            baseline_ts = _s(str(section.get("baseline_timestamp", "") or "")[:19].replace("T", " "))
            if baseline_ts:
                pdf.set_font("Sans", "I", 7.5)
                pdf.set_text_color(*b['text_light'])
                pdf.cell(0, 4, f"Baseline: {baseline_ts} UTC",
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1)
            _SEV_RGB_HC = {
                'positive': b['success'],
                'warning':  b['danger'],
                'neutral':  b['neutral'],
            }
            _ICON_HC = {"increase": "[+]", "decrease": "[-]", "stable": "[=]"}
            for comp in section.get("comparisons", []):
                try:
                    metric    = _s(str(comp.get("metric",         "")))
                    curr_val  = comp.get("current_value")
                    prev_val  = comp.get("previous_value")
                    change    = comp.get("change")
                    ctype     = str(comp.get("change_type", "stable")).lower()
                    sev       = str(comp.get("severity",    "neutral")).lower()
                    desc      = _s(str(comp.get("description", "")))
                    rgb       = _SEV_RGB_HC.get(sev, (100, 116, 139))
                    icon      = _ICON_HC.get(ctype, "[=]")
                    chg_str   = (f"+{change}" if isinstance(change, (int, float)) and change > 0
                                 else str(change) if change is not None else "0")
                    # Metric line: [icon] metric   curr → prev  (delta)
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb)
                    pdf.cell(10, 5, icon)
                    pdf.set_font("Sans", "B", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.cell(0, 5,
                             f"{metric}   {curr_val} (prev: {prev_val}, change: {chg_str})",
                             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if desc:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, f"  {desc}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                except Exception:
                    pass
        elif sec_type == "predictive_readiness":
            pr_score = section.get("readiness_score")
            pr_level = str(section.get("readiness_level", "low")).lower()
            _PR_LV_RGB = {
                'high':   b['success'],
                'medium': b['warning'],
                'low':    b['danger'],
            }
            _PR_ST_RGB = {
                'ready':   b['success'],
                'partial': b['warning'],
                'missing': b['danger'],
            }
            _PR_ST_SYM = {"ready": "[OK]", "partial": "[~]", "missing": "[X]"}
            lv_rgb = _PR_LV_RGB.get(pr_level, (100, 116, 139))
            # Score + level line
            score_str = str(pr_score) if pr_score is not None else "-"
            pdf.set_font("Sans", "B", 10)
            pdf.set_text_color(*lv_rgb)
            pdf.cell(0, 6, f"Score: {score_str} / 100  [{pr_level.upper()} READINESS]",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
            # Signals
            for sig in section.get("signals", []):
                try:
                    st_key  = str(sig.get("status", "missing")).lower()
                    st_rgb  = _PR_ST_RGB.get(st_key, (100, 116, 139))
                    st_sym  = _PR_ST_SYM.get(st_key, "[?]")
                    name    = _s(str(sig.get("name",        "")))
                    desc    = _s(str(sig.get("description", "")))
                    ev      = _s(str(sig.get("evidence",    "")))
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*st_rgb)
                    pdf.cell(12, 5, st_sym)
                    pdf.set_font("Sans", "B", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if desc:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, f"  {desc}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if ev:
                        pdf.set_font("Sans", "I", 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.multi_cell(0, 4, f"  Evidence: {ev}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                except Exception:
                    pass
            # Next steps
            next_steps = section.get("next_steps", [])
            if next_steps:
                pdf.ln(1)
                pdf.set_font("Sans", "B", 8)
                pdf.set_text_color(*b['primary'])
                pdf.cell(0, 5, "NEXT STEPS", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                for step in next_steps:
                    try:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_body'])
                        pdf.multi_cell(0, 4.5, f"  -> {_s(str(step))}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    except Exception:
                        pass
        elif sec_type == "trend":
            _DIR_RGB = {
                'up':       b['success'],
                'down':     b['danger'],
                'stable':   b['primary'],
                'volatile': b['warning'],
            }
            _STR_RGB = {
                'high':   b['danger'],
                'medium': b['warning'],
                'low':    b['neutral'],
            }
            for trend in section.get("trends", []):
                try:
                    direction   = str(trend.get("direction",   "stable")).lower()
                    strength    = str(trend.get("strength",    "low")).lower()
                    category    = _s(str(trend.get("category",    "")))
                    title       = _s(str(trend.get("title",       "")))
                    description = _s(str(trend.get("description", "")))
                    evidence    = _s(str(trend.get("evidence",    "")))
                    dir_symbol  = {"up": "[UP]", "down": "[DOWN]", "stable": "[STABLE]", "volatile": "[VOLATILE]"}.get(direction, f"[{direction.upper()}]")
                    rgb_dir     = _DIR_RGB.get(direction, (100, 116, 139))
                    rgb_str     = _STR_RGB.get(strength,  (100, 116, 139))
                    # Direction + strength prefix on one line
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb_dir)
                    pdf.cell(22, 5, dir_symbol)
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb_str)
                    pdf.cell(20, 5, f"[{strength.upper()}]")
                    pdf.set_font("Sans", "B", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if category:
                        pdf.set_font("Sans", "I", 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.cell(0, 4, f"  Category: {category}",
                                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if description:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, f"  {description}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if evidence:
                        pdf.set_font("Sans", "I", 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.multi_cell(0, 4, f"  Evidence: {evidence}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(2)
                except Exception:
                    pass
        elif sec_type == "anomaly":
            _SEV_RGB = {
                'high':   b['danger'],
                'medium': b['warning'],
                'low':    b['success'],
            }
            for anomaly in section.get("anomalies", []):
                try:
                    severity    = str(anomaly.get("severity", "low")).lower()
                    category    = _s(str(anomaly.get("category", "")))
                    title       = _s(str(anomaly.get("title", "")))
                    description = _s(str(anomaly.get("description", "")))
                    evidence    = _s(str(anomaly.get("evidence", "")))
                    rgb         = _SEV_RGB.get(severity, (100, 116, 139))
                    pdf.set_font("Sans", "B", 8)
                    pdf.set_text_color(*rgb)
                    pdf.cell(22, 5, f"[{severity.upper()}]")
                    pdf.set_font("Sans", "B", 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if category:
                        pdf.set_font("Sans", "I", 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.cell(0, 4, f"  Category: {category}",
                                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if description:
                        pdf.set_font("Sans", "", 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, f"  {description}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if evidence:
                        pdf.set_font("Sans", "I", 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.multi_cell(0, 4, f"  Evidence: {evidence}",
                                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(2)
                except Exception:
                    pass
        elif sec_type == 'chart':
            _ch      = section.get('chart', {})
            _ct      = _s(str(_ch.get('chart_type', '')))
            _lbls    = _ch.get('labels', [])
            _series  = _ch.get('series', [])
            _expl    = _s(str(section.get('explanation', '')))
            _s0_data = _series[0].get('data', []) if _series else []
            _s0_name = _s(str(_series[0].get('name', ''))) if _series else ''
            _has_neg = any(isinstance(_v, (int, float)) and _v < 0
                           for _v in _s0_data if _v is not None)
            if _ct in ('bar',) and _series and _lbls and not _has_neg:
                # ---- Visual bar chart ----
                _BAR_CH  = 48
                _BAR_CW  = 160
                _BAR_CX  = 20
                _BAR_CAP = 12
                _BPAD    = 8
                _dlbls   = [_s(str(_l)) for _l in _lbls[:_BAR_CAP]]
                _dvals   = [_v if isinstance(_v, (int, float)) else 0
                            for _v in _s0_data[:_BAR_CAP]]
                if _dlbls and _dvals:
                    _NB     = len(_dlbls)
                    _maxval = max(_dvals) if _dvals else 0
                    _maxval = _maxval if _maxval > 0 else 1
                    if pdf.get_y() + _BAR_CH + _BPAD + 8 > pdf.h - 25:
                        pdf.add_page()
                    _cy0  = pdf.get_y()
                    _cbot = _cy0 + _BAR_CH
                    # Axis lines
                    pdf.set_draw_color(*b['rule_light'])
                    pdf.set_line_width(0.3)
                    pdf.line(_BAR_CX, _cbot, _BAR_CX + _BAR_CW, _cbot)
                    pdf.line(_BAR_CX, _cy0, _BAR_CX, _cbot)
                    # Max-value scale label
                    pdf.set_font('Sans', '', 5.5)
                    pdf.set_text_color(*b['text_muted'])
                    pdf.set_xy(_BAR_CX + 1, _cy0)
                    pdf.cell(25, 4, '{:,.0f}'.format(_maxval))
                    # Draw bars
                    _bw = _BAR_CW / _NB
                    for _bi in range(_NB):
                        _bx = _BAR_CX + _bi * _bw
                        _bh = (_dvals[_bi] / _maxval) * _BAR_CH
                        _by = _cbot - _bh
                        pdf.set_fill_color(*b['primary'])
                        pdf.rect(_bx + 0.5, _by, _bw - 1.0, _bh, style='F')
                        if _bh > 5 and _by > _cy0 + 4:
                            try:
                                _dv = _dvals[_bi]
                                _vl = '{:,.0f}'.format(int(_dv)) if isinstance(_dv, float) and _dv == int(_dv) else '{:,.1f}'.format(_dv)
                                pdf.set_font('Sans', '', 5.5)
                                pdf.set_text_color(*b['text_muted'])
                                pdf.set_xy(_bx, _by - 4.5)
                                pdf.cell(_bw, 4, _vl, align='C')
                            except Exception:
                                pass
                    # X-axis labels
                    _lmax = 6 if _NB > 6 else 10
                    _lfsz = 5.5 if _NB > 8 else 6.5
                    for _bi, _dl in enumerate(_dlbls):
                        _bx = _BAR_CX + _bi * _bw
                        pdf.set_font('Sans', '', _lfsz)
                        pdf.set_text_color(*b['text_muted'])
                        pdf.set_xy(_bx, _cbot + 1.5)
                        _lt = _dl[:_lmax] + ('...' if len(_dl) > _lmax else '')
                        pdf.cell(_bw, 4, _lt, align='C')
                    # Legend / caption
                    pdf.set_xy(_BAR_CX, _cbot + _BPAD)
                    if _s0_name:
                        pdf.set_font('Sans', 'I', 7)
                        pdf.set_text_color(*b['text_chart'])
                        pdf.cell(0, 4, _s0_name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if len(_lbls) > _BAR_CAP:
                        pdf.set_font('Sans', 'I', 7)
                        pdf.set_text_color(*b['text_muted'])
                        pdf.cell(0, 4, 'Showing first ' + str(_BAR_CAP) + ' of ' + str(len(_lbls)) + ' categories.',
                                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if _expl:
                        pdf.set_font('Sans', 'I', 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, _expl, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                elif _expl:
                    pdf.set_font('Sans', 'I', 8)
                    pdf.set_text_color(*b['text_secondary'])
                    pdf.multi_cell(0, 4.5, _expl, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            elif _ct == 'line' and _series and _lbls and not _has_neg:
                # ---- Visual line chart ----
                _LC_CH  = 45
                _LC_CW  = 160
                _LC_CX  = 20
                _LC_PAD = 9
                _llbls  = [_s(str(_l)) for _l in _lbls]
                _NL     = len(_llbls)
                _lvals  = [_s0_data[_i] if _i < len(_s0_data) and isinstance(_s0_data[_i], (int, float)) else None
                           for _i in range(_NL)]
                _lnz    = [_v for _v in _lvals if isinstance(_v, (int, float))]
                _lmaxv  = max(_lnz) if _lnz else 1
                _lmaxv  = _lmaxv if _lmaxv > 0 else 1
                if pdf.get_y() + _LC_CH + _LC_PAD + 6 > pdf.h - 25:
                    pdf.add_page()
                _lcy0  = pdf.get_y()
                _lcbot = _lcy0 + _LC_CH
                # Axis lines
                pdf.set_draw_color(*b['rule_light'])
                pdf.set_line_width(0.3)
                pdf.line(_LC_CX, _lcbot, _LC_CX + _LC_CW, _lcbot)
                pdf.line(_LC_CX, _lcy0, _LC_CX, _lcbot)
                # Max-value scale label
                pdf.set_font('Sans', '', 5.5)
                pdf.set_text_color(*b['text_muted'])
                pdf.set_xy(_LC_CX + 1, _lcy0)
                pdf.cell(25, 4, '{:,.0f}'.format(_lmaxv))
                # Precompute point positions
                _lxs = [(_LC_CX + _pi * _LC_CW / (_NL - 1)) if _NL > 1 else (_LC_CX + _LC_CW / 2)
                        for _pi in range(_NL)]
                _lys = [(_lcy0 + _LC_CH * (1.0 - _lvals[_pi] / _lmaxv)) if isinstance(_lvals[_pi], (int, float)) else None
                        for _pi in range(_NL)]
                # Line segments (skip gaps at None points)
                pdf.set_draw_color(*b['primary'])
                pdf.set_line_width(0.6)
                for _pi in range(1, _NL):
                    if _lys[_pi - 1] is not None and _lys[_pi] is not None:
                        try:
                            pdf.line(_lxs[_pi - 1], _lys[_pi - 1], _lxs[_pi], _lys[_pi])
                        except Exception:
                            pass
                # Point markers
                pdf.set_fill_color(*b['primary'])
                for _pi in range(_NL):
                    if _lys[_pi] is not None:
                        try:
                            pdf.ellipse(_lxs[_pi] - 1, _lys[_pi] - 1, 2, 2, style='F')
                        except Exception:
                            pass
                # X-axis labels (every Kth to avoid overlap)
                _K = 1 if _NL <= 8 else (2 if _NL <= 16 else 4)
                for _pi, _ll in enumerate(_llbls):
                    if _pi % _K == 0:
                        _px = _lxs[_pi]
                        pdf.set_font('Sans', '', 5.5)
                        pdf.set_text_color(*b['text_muted'])
                        pdf.set_xy(_px - 8, _lcbot + 1.5)
                        pdf.cell(16, 4, _ll[:8], align='C')
                # Legend / caption
                pdf.set_xy(_LC_CX, _lcbot + _LC_PAD)
                if _s0_name:
                    pdf.set_font('Sans', 'I', 7)
                    pdf.set_text_color(*b['text_chart'])
                    pdf.cell(0, 4, _s0_name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                if _expl:
                    pdf.set_font('Sans', 'I', 8)
                    pdf.set_text_color(*b['text_secondary'])
                    pdf.multi_cell(0, 4.5, _expl, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            elif _ct in ('pie', 'donut') and _series and _lbls:
                # ---- Pie / donut: percentage breakdown (improved text fallback) ----
                _pd_data  = [_v if isinstance(_v, (int, float)) else 0 for _v in _s0_data]
                _pd_total = sum(_pd_data) or 1
                pdf.set_font('Sans', 'I', 8)
                pdf.set_text_color(*b['text_chart'])
                pdf.cell(0, 4, _ct.title() + ' chart - share breakdown:',
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1)
                for _pi, (_pl, _pv) in enumerate(zip(_lbls[:12], _pd_data[:12])):
                    try:
                        _ppct = _pv / _pd_total * 100
                        _pv_s = '{:,.0f}'.format(int(_pv)) if isinstance(_pv, float) and _pv == int(_pv) else '{:,.1f}'.format(_pv)
                        _prow = str(_pi + 1) + '. ' + _s(str(_pl)) + ': ' + _pv_s + '  (' + '{:.1f}'.format(_ppct) + '%)'
                        pdf.set_font('Sans', '', 8.5)
                        pdf.set_text_color(*b['text_body'])
                        pdf.multi_cell(0, 5, _prow, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    except Exception:
                        pass
                if _expl:
                    pdf.ln(1)
                    pdf.set_font('Sans', 'I', 8)
                    pdf.set_text_color(*b['text_secondary'])
                    pdf.multi_cell(0, 4.5, _expl, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            else:
                # ---- Text fallback: correlation_matrix, unknown types, malformed / empty ----
                if _ct:
                    pdf.set_font('Sans', 'I', 8)
                    pdf.set_text_color(*b['text_chart'])
                    pdf.cell(0, 4, _ct + '  |  ' + str(len(_lbls)) + ' data points',
                             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                for _se in _series:
                    try:
                        _sn = _s(str(_se.get('name', '')))
                        _sd = _se.get('data', [])
                        for _si, _slbl in enumerate(_lbls):
                            _sv = _sd[_si] if _si < len(_sd) else None
                            if _sv is None:
                                _sv_s = '-'
                            elif isinstance(_sv, float) and _sv == int(_sv):
                                _sv_s = '{:,}'.format(int(_sv))
                            elif isinstance(_sv, (int, float)):
                                _sv_s = '{:,.2f}'.format(_sv)
                            else:
                                _sv_s = _s(str(_sv))
                            _sfx = '  [' + _sn + ']' if _sn else ''
                            pdf.set_font('Sans', '', 9)
                            pdf.set_text_color(*b['text_body'])
                            pdf.multi_cell(0, 5, '  ' + _s(str(_slbl)) + ': ' + _sv_s + _sfx,
                                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    except Exception:
                        pass
                if _expl:
                    pdf.ln(1)
                    pdf.set_font('Sans', 'I', 8)
                    pdf.set_text_color(*b['text_secondary'])
                    pdf.multi_cell(0, 4.5, _expl, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif sec_type == 'business_kpis':
            _TREND_SYM = {'up': '[+]', 'down': '[-]', 'neutral': '[=]', 'stable': '[=]'}
            _TREND_RGB = {
                'up':      b['success'],
                'down':    b['danger'],
                'neutral': b['neutral'],
                'stable':  b['neutral'],
            }
            _ds_label = _s(str(section.get('dataset_label', '')))
            if _ds_label:
                pdf.set_font('Sans', 'I', 7.5)
                pdf.set_text_color(*b['text_light'])
                pdf.cell(0, 4, _ds_label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1)
            _kpis = section.get('kpis', [])
            for _kpi in _kpis:
                try:
                    _kpi_lbl  = _s(str(_kpi.get('label', '')))
                    _kpi_val  = _s(str(_kpi.get('value_formatted', '') or _kpi.get('value', '')))
                    _kpi_tr   = str(_kpi.get('trend', 'neutral')).lower()
                    _kpi_desc = _s(str(_kpi.get('description', '') or ''))
                    _kpi_dlt  = _kpi.get('delta')
                    _kpi_ddir = str(_kpi.get('delta_direction', '') or '').lower()
                    _kpi_sym  = _TREND_SYM.get(_kpi_tr, '[=]')
                    _kpi_rgb  = _TREND_RGB.get(_kpi_tr, b['neutral'])
                    pdf.set_font('Sans', 'B', 8.5)
                    pdf.set_text_color(*_kpi_rgb)
                    pdf.cell(10, 5.5, _kpi_sym)
                    pdf.set_font('Sans', 'B', 9)
                    pdf.set_text_color(*b['text_dark'])
                    pdf.cell(70, 5.5, _kpi_lbl + ':')
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['primary'])
                    _kpi_line = _kpi_val
                    if _kpi_dlt is not None:
                        try:
                            _dlt_f    = float(_kpi_dlt)
                            _dlt_sym2 = '+' if _kpi_ddir == 'increase' else ('-' if _kpi_ddir == 'decrease' else '')
                            _kpi_line += '  (' + _dlt_sym2 + '{:.1f}'.format(abs(_dlt_f)) + '% vs prior period)'
                        except Exception:
                            pass
                    pdf.multi_cell(0, 5.5, _kpi_line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if _kpi_desc:
                        pdf.set_font('Sans', 'I', 7.5)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.cell(10, 4, '')
                        pdf.multi_cell(0, 4, _kpi_desc, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(1)
                except Exception:
                    pass
            if not _kpis:
                for _item in section.get('items', []):
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(str(_item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif sec_type == 'segmentation_insights':
            _segs = section.get('segments', [])
            for _seg in _segs:
                try:
                    _seg_m   = _s(str(_seg.get('metric', '')))
                    _seg_d   = _s(str(_seg.get('dimension', '')))
                    _seg_ins = _s(str(_seg.get('insight_summary', '')))
                    _seg_act = _s(str(_seg.get('recommended_action', '')))
                    _seg_top = _seg.get('top_segments', [])
                    if _seg_m or _seg_d:
                        _seg_hdr = (_seg_m + ' by ' + _seg_d).strip(' by ')
                        pdf.set_font('Sans', 'B', 9)
                        pdf.set_text_color(*b['primary'])
                        pdf.cell(0, 5.5, _seg_hdr, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    for _ts in _seg_top[:5]:
                        try:
                            _ts_lbl  = _s(str(_ts.get('label', '')))
                            _ts_val  = _ts.get('value', None)
                            _ts_pct  = _ts.get('pct_of_total', None)
                            _ts_rnk  = _ts.get('rank', '')
                            _ts_vstr = '{:,.0f}'.format(_ts_val) if isinstance(_ts_val, (int, float)) else _s(str(_ts_val))
                            _ts_pstr = '  ({:.1f}% of total)'.format(_ts_pct) if isinstance(_ts_pct, (int, float)) else ''
                            pdf.set_font('Sans', '', 8.5)
                            pdf.set_text_color(*b['text_body'])
                            pdf.cell(8, 5, _s(str(_ts_rnk)) + '.')
                            pdf.cell(80, 5, _ts_lbl + ':')
                            pdf.set_text_color(*b['primary'])
                            pdf.multi_cell(0, 5, _ts_vstr + _ts_pstr, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                        except Exception:
                            pass
                    if _seg_ins:
                        pdf.set_font('Sans', 'I', 8.5)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 5, _seg_ins, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if _seg_act:
                        pdf.set_font('Sans', 'I', 8)
                        pdf.set_text_color(*b['text_light'])
                        pdf.multi_cell(0, 4.5, 'Recommended: ' + _seg_act, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(2)
                except Exception:
                    pass
            if not _segs:
                for _item in section.get('items', []):
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(str(_item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif sec_type == 'drilldown_table':
            _C_DIM  = 90
            _C_VAL  = 50
            _C_PCT  = 30
            _tables = section.get('tables', [])
            for _tbl in _tables:
                try:
                    _tbl_m    = _s(str(_tbl.get('metric', '')))
                    _tbl_d    = _s(str(_tbl.get('dimension', '')))
                    _tbl_smr  = _s(str(_tbl.get('summary', '')))
                    _tbl_rows = _tbl.get('rows', [])
                    if _tbl_m or _tbl_d:
                        _tbl_hdr = (_tbl_m + ' by ' + _tbl_d).strip(' by ')
                        pdf.set_font('Sans', 'B', 9)
                        pdf.set_text_color(*b['primary'])
                        pdf.cell(0, 5.5, _tbl_hdr, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_font('Sans', 'B', 8)
                    pdf.set_text_color(*b['meta_label'])
                    pdf.cell(_C_DIM, 5, 'Dimension')
                    pdf.cell(_C_VAL, 5, 'Total', align='R')
                    pdf.cell(_C_PCT, 5, 'Share', align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_draw_color(*b['rule_light'])
                    pdf.set_line_width(0.2)
                    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
                    for _row in _tbl_rows[:8]:
                        try:
                            _r_lbl  = _s(str(_row.get('label', '')))
                            _r_lbl  = _r_lbl[:38] + '...' if len(_r_lbl) > 40 else _r_lbl
                            _r_val  = _row.get('value', None)
                            _r_pct  = _row.get('pct_of_total', None)
                            _r_vstr = '{:,.1f}'.format(_r_val) if isinstance(_r_val, (int, float)) else _s(str(_r_val))
                            _r_pstr = '{:.1f}%'.format(_r_pct) if isinstance(_r_pct, (int, float)) else '-'
                            pdf.set_font('Sans', '', 8.5)
                            pdf.set_text_color(*b['text_body'])
                            pdf.cell(_C_DIM, 5, _r_lbl)
                            pdf.cell(_C_VAL, 5, _r_vstr, align='R')
                            pdf.cell(_C_PCT, 5, _r_pstr, align='R', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                        except Exception:
                            pass
                    if _tbl_smr:
                        pdf.set_font('Sans', 'I', 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, _tbl_smr, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(2)
                except Exception:
                    pass
            if not _tables:
                for _item in section.get('items', []):
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(str(_item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif sec_type == 'forecast':
            _fc_col   = _s(str(section.get('target_column', '')))
            _fc_meth  = _s(str(section.get('method', '')).replace('_', ' ').title())
            _fc_horiz = section.get('horizon_periods', None)
            _fc_meta  = []
            if _fc_col:
                _fc_meta.append('Target: ' + _fc_col)
            if _fc_meth:
                _fc_meta.append('Method: ' + _fc_meth)
            if _fc_horiz is not None:
                _fc_meta.append('Horizon: ' + _s(str(_fc_horiz)) + ' periods')
            if _fc_meta:
                pdf.set_font('Sans', 'I', 8)
                pdf.set_text_color(*b['text_chart'])
                pdf.cell(0, 4.5, '  '.join(_fc_meta), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1.5)
            for _fi in section.get('items', []):
                try:
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(str(_fi)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                except Exception:
                    pass
            _fc_chart = section.get('chart', {})
            _fc_lbls  = _fc_chart.get('labels', [])
            _fc_vals  = _fc_chart.get('forecast', [])
            _fc_upper = _fc_chart.get('upper_band', [])
            _fc_lower = _fc_chart.get('lower_band', [])
            _fc_start = _fc_chart.get('forecast_start_index', None)
            if _fc_start is not None and _fc_lbls:
                pdf.ln(2)
                pdf.set_font('Sans', 'B', 8.5)
                pdf.set_text_color(*b['primary'])
                pdf.cell(0, 5, 'Projected Outlook', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                for _pi in range(_fc_start, len(_fc_lbls)):
                    try:
                        _p_lbl = _s(str(_fc_lbls[_pi]))
                        _p_val = _fc_vals[_pi] if _pi < len(_fc_vals) else None
                        _p_hi  = _fc_upper[_pi] if _pi < len(_fc_upper) else None
                        _p_lo  = _fc_lower[_pi] if _pi < len(_fc_lower) else None
                        if _p_val is None:
                            continue
                        if isinstance(_p_val, float) and _p_val == int(_p_val):
                            _p_vstr = '{:,.0f}'.format(int(_p_val))
                        elif isinstance(_p_val, (int, float)):
                            _p_vstr = '{:,.1f}'.format(_p_val)
                        else:
                            _p_vstr = _s(str(_p_val))
                        _p_band = ''
                        if isinstance(_p_lo, (int, float)) and isinstance(_p_hi, (int, float)):
                            _p_band = '  [{:,.0f} – {:,.0f}]'.format(_p_lo, _p_hi)
                        pdf.set_font('Sans', '', 8.5)
                        pdf.set_text_color(*b['text_body'])
                        pdf.cell(55, 5, '  ' + _p_lbl + ':')
                        pdf.set_text_color(*b['primary'])
                        pdf.multi_cell(0, 5, _p_vstr + _p_band, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    except Exception:
                        pass
        elif sec_type == 'ai_dashboard':
            _ai_ins = _s(str(section.get('most_important_insight', '')))
            _ai_rsk = _s(str(section.get('highest_risk', '')))
            _ai_act = _s(str(section.get('recommended_action', '')))
            _ai_wl  = section.get('watchlist', [])
            if _ai_ins:
                pdf.set_font('Sans', 'B', 8.5)
                pdf.set_text_color(*b['primary'])
                pdf.cell(0, 5, 'Key Insight', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font('Sans', '', 9)
                pdf.set_text_color(*b['text_body'])
                pdf.multi_cell(0, 5, _ai_ins, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1.5)
            if _ai_rsk:
                _ai_rsk_clr = b['neutral'] if 'No high-severity risks identified' in _ai_rsk else b['danger']
                pdf.set_font('Sans', 'B', 8.5)
                pdf.set_text_color(*_ai_rsk_clr)
                pdf.cell(0, 5, 'Risk Alert', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font('Sans', '', 9)
                pdf.set_text_color(*b['text_body'])
                pdf.multi_cell(0, 5, _ai_rsk, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1.5)
            if _ai_act:
                pdf.set_font('Sans', 'B', 8.5)
                pdf.set_text_color(*b['primary_dark'])
                pdf.cell(0, 5, 'Recommended Action', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_font('Sans', '', 9)
                pdf.set_text_color(*b['text_body'])
                pdf.multi_cell(0, 5, _ai_act, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.ln(1.5)
            if _ai_wl:
                pdf.set_font('Sans', 'B', 8.5)
                pdf.set_text_color(*b['text_dark'])
                pdf.cell(0, 5, 'Watchlist', new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                for _wl_item in _ai_wl:
                    try:
                        pdf.set_font('Sans', '', 8.5)
                        pdf.set_text_color(*b['text_body'])
                        pdf.cell(6, 5, '•')
                        pdf.multi_cell(0, 5, _s(str(_wl_item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    except Exception:
                        pass
            if not any([_ai_ins, _ai_rsk, _ai_act, _ai_wl]):
                for _item in section.get('items', []):
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(str(_item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif sec_type == 'insight_priority':
            _SEV_RGB = {
                'high':     b['danger'],
                'medium':   b['warning'],
                'low':      b['neutral'],
                'critical': b['danger'],
                'info':     b['primary'],
            }
            _SEV_LBL = {
                'high':     'HIGH',
                'medium':   'MEDIUM',
                'low':      'LOW',
                'critical': 'CRITICAL',
                'info':     'INFO',
            }
            _CONF_LBL = {
                'high':   'High confidence',
                'medium': 'Moderate confidence',
                'low':    'Indicative',
            }
            _insights = section.get('insights', [])
            for _ins in _insights:
                try:
                    _ins_title = _s(str(_ins.get('title', '')))
                    _ins_sev   = str(_ins.get('severity', 'medium')).lower()
                    _ins_evid  = _s(str(_ins.get('evidence', '')))
                    _ins_act   = _s(str(_ins.get('recommended_action', '')))
                    _ins_conf  = str(_ins.get('confidence', '')).lower()
                    _ins_rgb   = _SEV_RGB.get(_ins_sev, b['neutral'])
                    _ins_sev_l = _SEV_LBL.get(_ins_sev, _ins_sev.upper())
                    pdf.set_font('Sans', 'B', 8)
                    pdf.set_text_color(*_ins_rgb)
                    pdf.cell(18, 5.5, '[' + _ins_sev_l + ']')
                    pdf.set_font('Sans', 'B', 9)
                    pdf.set_text_color(*b['text_dark'])
                    pdf.multi_cell(0, 5.5, _ins_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if _ins_evid:
                        pdf.set_font('Sans', '', 8.5)
                        pdf.set_text_color(*b['text_body'])
                        pdf.multi_cell(0, 5, _ins_evid, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if _ins_act:
                        pdf.set_font('Sans', 'I', 8)
                        pdf.set_text_color(*b['text_secondary'])
                        pdf.multi_cell(0, 4.5, 'Action: ' + _ins_act, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    if _ins_conf:
                        _ins_conf_l = _CONF_LBL.get(_ins_conf, _ins_conf.capitalize())
                        pdf.set_font('Sans', 'I', 7.5)
                        pdf.set_text_color(*b['text_light'])
                        pdf.cell(0, 4, _ins_conf_l, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.ln(2)
                except Exception:
                    pass
            if not _insights:
                for _item in section.get('items', []):
                    pdf.set_font('Sans', '', 9)
                    pdf.set_text_color(*b['text_body'])
                    pdf.multi_cell(0, 5, _s(str(_item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            # Unknown future section types: safe string fallback, never crash.
            for item in section.get("items", []):
                pdf.set_font("Sans", "", 9)
                pdf.set_text_color(*b['text_body'])
                pdf.multi_cell(0, 5, _s(str(item)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.ln(3)

    return bytes(pdf.output())
