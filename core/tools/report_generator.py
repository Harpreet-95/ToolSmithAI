import json
import logging
import math
from datetime import datetime, timezone

from core.intelligence.business_kpi_engine import build_business_kpi_section
from core.intelligence.segmentation_engine import (
    build_segmentation_section,
    build_drilldown_table_section,
)
from core.output.kpi_formatter import format_kpi_display_label, format_dataset_display_name

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
) -> dict:
    """Build a deterministic executive summary from stored profile data.

    No AI call. No hallucinated claims. Every statement is derived from stored
    profile values only. Called after date_profile is computed so all data is
    available. Pre-stamped type='executive_summary' so the setdefault loop ignores it.
    """
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
                f"{col_name} is the primary numeric indicator "
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
                        f'"{top["value"]}" is the most frequent value in {col_name} '
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
                f"of {dc['column']} data."
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
) -> dict | None:
    """Build deterministic recommended actions from stored profile data.

    Returns None when no recommendations apply so the caller can skip the section.
    Maximum 5 recommendations, ordered by priority (high first).
    No AI call. No hallucinated claims — every recommendation is gated on a
    verifiable profile fact.
    """
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
                    f"A date column ({col_name}) is present. "
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
            col_hint = f" (starting with {best[0]})" if best else ""
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
                        "title":       f"Investigate Outlier Concentration: {col}",
                        "reason":      (
                            f"{int(oc):,} outlier values in '{col}' "
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
                        f"'{top['column_a']}' and '{top['column_b']}'. "
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
                            f"Column '{dc['column']}' has daily granularity over "
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
) -> dict:
    """Build deterministic anomaly/risk detection from stored profile data only.

    No AI. No row-level access. No hallucinated claims.
    All thresholds are conservative and evidence-based.
    Maximum 8 anomalies, sorted high → medium → low.
    Shows an all-clear item when none are found so users know the check ran.
    """
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
                        "title":       f"High Missing Rate: {col}",
                        "description": f"Column '{col}' has a critically high proportion of missing values.",
                        "severity":    "high",
                        "category":    "missing_data",
                        "evidence":    f"{int(cnt):,} of {row_count:,} rows ({round(rate * 100, 1)}%) are null.",
                    })
                elif rate >= 0.10:
                    anomalies.append({
                        "title":       f"Moderate Missing Rate: {col}",
                        "description": f"Column '{col}' has a notable proportion of missing values.",
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
                        "title":       f"Extreme Category Dominance: {col}",
                        "description": f"A single value dominates '{col}', indicating a near-constant field.",
                        "severity":    "high",
                        "category":    "distribution",
                        "evidence":    (
                            f'"{top_value}" appears in {round(dominance * 100, 1)}% of rows '
                            f"({int(top_count):,} of {row_count:,})."
                        ),
                    })
                elif dominance >= 0.80:
                    anomalies.append({
                        "title":       f"Category Imbalance: {col}",
                        "description": f"Column '{col}' is heavily skewed toward one value.",
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
                    "title":       f"Extreme Value Range: {col}",
                    "description": f"Column '{col}' spans an unusually wide numeric range (>1000x).",
                    "severity":    "medium",
                    "category":    "distribution",
                    "evidence":    (
                        f"Min={_safe_fmt(mn)}, Max={_safe_fmt(mx)} "
                        f"(ratio {round(mx / mn):,}x). Mean={_safe_fmt(mean)}."
                    ),
                })
            elif mean > 0 and mx > 10 * mean:
                anomalies.append({
                    "title":       f"Potential Outlier in {col}",
                    "description": f"Maximum value in '{col}' is far above the column mean, suggesting outliers.",
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
                        "title":       f"Large Trend Shift: {col}",
                        "description": (
                            f"Column '{col}' shows a sharp {direction} between "
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
                    "title":       f"High Outlier Density: {col}",
                    "description": f"Column '{col}' has a high proportion of IQR outliers.",
                    "severity":    "high",
                    "category":    "distribution",
                    "evidence":    f"{int(oc):,} outliers in {int(nn):,} non-null values ({round(density * 100, 1)}%).",
                })
            elif density >= 0.10:
                anomalies.append({
                    "title":       f"Moderate Outlier Density: {col}",
                    "description": f"Column '{col}' contains a notable proportion of IQR outliers.",
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
                    "title":       f"Extreme Variability: {col}",
                    "description": f"Column '{col}' has an extreme coefficient of variation — values are highly unstable relative to the mean.",
                    "severity":    "high",
                    "category":    "distribution",
                    "evidence":    f"CV={round(cv, 2)} (std={_safe_fmt(std_f)}, mean={_safe_fmt(mean_f)}).",
                })
            elif cv > 2.0:
                anomalies.append({
                    "title":       f"High Variability: {col}",
                    "description": f"Column '{col}' shows high relative variability around its mean.",
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
            "title":       f"Sparse Distribution: {col}",
            "description": f"Column '{col}' has large empty regions in its value distribution, suggesting gaps or multimodal patterns.",
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
                        "title":       f"Highly Correlated Fields: {a} & {b}",
                        "description": f"'{a}' and '{b}' move together very closely and may contain overlapping information.",
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
                        "title":       f"Low Categorical Diversity: {col}",
                        "description": f"Column '{col}' has near-zero entropy — distributional variety is severely limited.",
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
) -> dict:
    """Build deterministic trend intelligence from stored profile data only.

    No AI. No forecasting. No future predictions.
    Every trend statement is derived from stored profile values.
    Maximum 6 trends, sorted by strength (high first).
    Shows a fallback item when no profile data yields signals.
    """
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
                    "title":       f"{col} — {dir_label} Trend",
                    "description": (
                        f"{col} shows a {dir_label.lower()} pattern between the first and "
                        f"second half of the dataset when sorted by {date_col_name}."
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
                        "title":       f"Concentrated Distribution: {best_col}",
                        "description": (
                            f"Column '{best_col}' is dominated by a single value, "
                            "indicating low distributional variety."
                        ),
                        "direction": "stable",
                        "strength":  "high",
                        "category":  "distribution",
                        "evidence":  f'"{top_value}" holds {pct_dom}% of rows. {val_label}',
                    })
                elif best_dom >= 0.50:
                    trends.append({
                        "title":       f"Moderate Concentration: {best_col}",
                        "description": (
                            f"Column '{best_col}' has a leading category but retains "
                            "meaningful distributional variety."
                        ),
                        "direction": "stable",
                        "strength":  "medium",
                        "category":  "distribution",
                        "evidence":  f'"{top_value}" leads at {pct_dom}% of rows. {val_label}',
                    })
                else:
                    trends.append({
                        "title":       f"Balanced Distribution: {best_col}",
                        "description": (
                            f"Column '{best_col}' is spread across categories, "
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
                        "title":       f"High Value Dispersion: {best_col}",
                        "description": (
                            f"'{best_col}' shows wide numeric spread relative to its mean, "
                            "indicating volatile or heterogeneous values."
                        ),
                        "direction": "volatile",
                        "strength":  "medium",
                        "category":  "distribution",
                        "evidence":  ev,
                    })
                elif best_ratio > 3:
                    trends.append({
                        "title":       f"Moderate Value Spread: {best_col}",
                        "description": (
                            f"'{best_col}' shows moderate numeric spread, "
                            "typical of datasets with natural variation."
                        ),
                        "direction": "stable",
                        "strength":  "low",
                        "category":  "distribution",
                        "evidence":  ev,
                    })
                else:
                    trends.append({
                        "title":       f"Tightly Grouped Values: {best_col}",
                        "description": (
                            f"'{best_col}' values cluster closely around the mean, "
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
                    "title":       f"Monthly Volume Growth: {col}",
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
                    "title":       f"Monthly Volume Decline: {col}",
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
                    "title":       f"Stable Monthly Volume: {col}",
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
                    "title":       f"Daily Granularity: {col}",
                    "description": f"Column '{col}' records at daily intervals — supports fine-grained time-series analysis.",
                    "direction":   "stable",
                    "strength":    "high",
                    "category":    "time_series",
                    "evidence":    f"Inferred daily granularity over {range_days:,} days.",
                })
            elif gran in ("weekly", "monthly"):
                trends.append({
                    "title":       f"{gran.title()} Granularity: {col}",
                    "description": f"Column '{col}' records at {gran} intervals — suitable for period aggregation and reporting.",
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
) -> dict:
    """Assess whether the dataset is ready for future predictive analytics.

    No predictions. No ML models. No forecasting claims.
    Evaluates five readiness signals against conservative thresholds.
    Each signal contributes 0 (missing), 10 (partial), or 20 (ready) points.
    Maximum score = 100. Always returns a section — never omitted.
    """
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
                "description": f"High outlier density in '{_worst_col}' may require preprocessing before model training.",
                "evidence":    f"{round(_worst_d * 100, 1)}% outlier rate (IQR method).",
            })
            score = max(0, score - 5)
        elif _worst_col and _worst_d >= 0.10:
            signals.append({
                "name":        "Outlier Data Quality",
                "status":      "partial",
                "description": f"Moderate outlier density in '{_worst_col}' — review before applying models.",
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
                    "description": f"Daily granularity in '{col}' supports fine-grained forecasting and sequence modeling.",
                    "evidence":    f"Inferred daily granularity over {dc.get('range_days', 0):,} days.",
                })
                score = min(100, score + 5)
            elif gran in ("weekly", "monthly"):
                signals.append({
                    "name":        "Date Granularity",
                    "status":      "partial",
                    "description": f"{gran.title()} granularity in '{col}' — adequate for period aggregation but not fine-grained forecasting.",
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


def generate_dataset_report(
    dataset: dict,
    previous_snapshot: dict | None = None,
    baseline_snapshots: list[dict] | None = None,
    selected_sections: list[str] | None = None,
    intent_text: str | None = None,
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

    # ── Adaptive Report Plan ─────────────────────────────────────────────────
    # Computed once from intent + profiles; applied for section ordering later.
    # Lazy import so old datasets without planner never fail.
    _report_plan: dict | None = None
    try:
        from core.intelligence.report_planner import plan_report as _plan_report
        _report_plan = _plan_report(
            intent_text         = intent_text,
            semantic_profile    = semantic_profile,
            date_profile        = date_profile,
            numeric_profile     = numeric_profile,
            categorical_profile = categorical_profile,
        )
    except Exception:
        pass

    # ── Report Strategy Engine ────────────────────────────────────────────────
    # Resolved alongside the adaptive plan. In Phase 3 the strategy always
    # returns section_scores={} so the reorder block falls back to plan_report
    # scores — zero behaviour change.  Phase 4 will populate section_scores to
    # activate intent-driven ordering.
    _strategy = None
    try:
        from core.intelligence.report_strategy_engine import resolve_report_strategy as _resolve_strategy
        _strategy = _resolve_strategy(
            intent_text         = intent_text,
            semantic_profile    = semantic_profile,
            date_profile        = date_profile,
            numeric_profile     = numeric_profile,
            categorical_profile = categorical_profile,
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
            f"{high_col} has the highest average value at {_safe_fmt(high_stats['mean'])}."
        )
        if high_stats.get("min") is not None and high_stats.get("max") is not None:
            items.append(
                f"{high_col} ranges from {_safe_fmt(high_stats['min'])}"
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
                f"{sum_col} has the highest total at {_safe_fmt(sum_stats['sum'])}."
            )
        if len(by_mean) > 1:
            low_col, low_stats = by_mean[-1]
            items.append(
                f"{low_col} has the lowest average value at {_safe_fmt(low_stats['mean'])}."
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
                f"{col} has {cnt:,} missing values ({pct}% of rows)."
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
        f'{col} is most commonly "{entries[0]["value"]}" ({entries[0]["count"]:,} rows).'
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
                f"{col}: {valid:,} date values detected. Coverage: {range_str}."
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
                f"{col} is {trend} {symbol} ({pct_str} change from first to second half,"
                f" sorted by {date_col_name})."
            )
        sections.append({"heading": "Trend Insights", "items": trend_items})

    # ── Anomaly Detection ─────────────────────────────────────────────────────
    sections.append(_build_anomaly_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        correlation_profile=correlation_profile,
        categorical_meta=categorical_meta,
    ))

    # ── Trend Intelligence ────────────────────────────────────────────────────
    sections.append(_build_trend_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
    ))

    # ── Predictive Readiness ──────────────────────────────────────────────────
    sections.append(_build_predictive_readiness_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
        correlation_profile=correlation_profile,
        categorical_meta=categorical_meta,
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
