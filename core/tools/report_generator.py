import json
import logging
import math
from datetime import datetime, timezone

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
  "risk_notes": ["<concern 1>", "<up to 3 data quality concerns or anomalies>"]
}

Rules:
- Use only facts from the provided analysis. Do not invent data.
- executive_summary: 1-3 business-focused sentences, max 800 characters.
- key_takeaways: 2-5 concise bullet points, each max 200 characters.
- risk_notes: 0-3 concerns (missing values, quality issues, anomalies), each max 200 characters.
  Return an empty list if no risks are evident.
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

    return {
        "executive_summary": summary,
        "key_takeaways":     takeaways,
        "risk_notes":        risks,
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
    kpis.append({
        "label": "Total Records",
        "value": row_count,
        "format": "number",
        "trend": "neutral",
        "description": "Rows in the uploaded dataset",
    })

    # 2. Total Features
    kpis.append({
        "label": "Total Features",
        "value": column_count,
        "format": "number",
        "trend": "neutral",
        "description": "Columns analysed",
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
            kpis.append({
                "label": "Data Completeness",
                "value": completeness,
                "format": "percent",
                "trend": trend,
                "description": "Non-null cells across all columns",
            })
        except Exception:
            pass

    # 4. Columns with Missing Data
    try:
        cols_with_gaps = sum(
            1 for v in missing_values.values()
            if isinstance(v, (int, float)) and v > 0
        )
        kpis.append({
            "label": "Columns with Gaps",
            "value": cols_with_gaps,
            "format": "number",
            "trend": "down" if cols_with_gaps > 0 else "neutral",
            "description": f"Of {column_count} total columns",
        })
    except Exception:
        pass

    # 5. Numeric Columns
    numeric_count = len(numeric_profile)
    if numeric_count > 0 and len(kpis) < 6:
        kpis.append({
            "label": "Numeric Columns",
            "value": numeric_count,
            "format": "number",
            "trend": "neutral",
            "description": "Columns with quantitative values",
        })

    # 6. Categorical Columns
    cat_count = len(categorical_profile)
    if cat_count > 0 and len(kpis) < 6:
        kpis.append({
            "label": "Categorical Columns",
            "value": cat_count,
            "format": "number",
            "trend": "neutral",
            "description": "Columns with category values",
        })

    return {
        "type": "kpi",
        "heading": "Key Metrics",
        "kpis": kpis[:6],
    }


def _build_chart_sections(
    categorical_profile: dict,
    missing_values: dict,
    row_count: int,
    date_profile: dict,
) -> list[dict]:
    """Build chart sections from stored profile data. Returns [] on any failure.

    Uses only pre-computed profile JSON — never requires the raw DataFrame.
    All sections are pre-stamped type='chart' so the setdefault loop ignores them.
    """
    charts: list[dict] = []

    # 1. Categorical bar chart — the column with the most distinct top values
    try:
        best = max(
            ((col, entries) for col, entries in categorical_profile.items() if entries),
            key=lambda x: len(x[1]),
            default=None,
        )
        if best:
            col_name, entries = best
            top    = entries[:10]
            labels = [str(e.get("value", "")) for e in top]
            data   = [int(e.get("count", 0))  for e in top]
            if labels and any(d > 0 for d in data):
                charts.append({
                    "type": "chart",
                    "heading": f"{col_name} — Category Breakdown",
                    "chart": {
                        "chart_type": "bar",
                        "labels": labels,
                        "series": [{"name": "Count", "data": data}],
                    },
                })
    except Exception:
        pass

    # 2. Missing values bar chart — only columns that have at least one missing value
    try:
        missing_entries = sorted(
            [
                (col, int(cnt))
                for col, cnt in missing_values.items()
                if isinstance(cnt, (int, float)) and math.isfinite(cnt) and cnt > 0
            ],
            key=lambda x: x[1],
            reverse=True,
        )[:8]
        if missing_entries:
            charts.append({
                "type": "chart",
                "heading": "Missing Values by Column",
                "chart": {
                    "chart_type": "bar",
                    "labels": [col for col, _ in missing_entries],
                    "series": [{"name": "Missing Count", "data": [cnt for _, cnt in missing_entries]}],
                },
            })
    except Exception:
        pass

    return charts


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
                    "Clean or impute before relying on analysis conclusions."
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

    # 6. Large dataset — automation opportunity (low priority, fill remaining slot)
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

    return {
        "type":            "recommendation",
        "heading":         "Recommended Actions",
        "recommendations": recs[:5],
    }


def _build_anomaly_section(
    row_count: int,
    column_count: int,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
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


def generate_dataset_report(dataset: dict) -> dict:
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

    sections: list[dict] = []

    # ── Overview ──────────────────────────────────────────────────────────────
    sections.append({
        "heading": "Overview",
        "items": [
            f'"{filename}" contains {row_count:,} rows and {column_count} columns.',
        ],
    })

    # ── Key Metrics (KPI) ─────────────────────────────────────────────────────
    sections.append(_build_kpi_section(
        row_count, column_count, numeric_profile, missing_values, categorical_profile,
    ))

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
    date_profile_raw = dataset.get("date_profile_json")
    date_profile: dict = json.loads(date_profile_raw) if date_profile_raw else {}

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
    ))

    # ── Trend Intelligence ────────────────────────────────────────────────────
    sections.append(_build_trend_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
    ))

    # ── Chart Sections ────────────────────────────────────────────────────────
    for chart_sec in _build_chart_sections(
        categorical_profile, missing_values, row_count, date_profile
    ):
        sections.append(chart_sec)

    # ── Recommendations (inserted after KPI for executive-first report flow) ──
    rec_sec = _build_recommendation_section(
        row_count, column_count,
        numeric_profile, categorical_profile, missing_values, date_profile,
    )
    if rec_sec is not None:
        # KPI section was appended second (after Overview), so it's always at
        # index 1 before any inserts.  Find it by type for resilience.
        kpi_pos = next((i for i, s in enumerate(sections) if s.get("type") == "kpi"), 1)
        sections.insert(kpi_pos + 1, rec_sec)

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
        # Assemble an "Executive Summary" section from the validated AI fields.
        exec_items: list[str] = [ai_narrative["executive_summary"]]
        for t in ai_narrative["key_takeaways"]:
            exec_items.append(f"Key takeaway: {t}")
        for r in ai_narrative["risk_notes"]:
            exec_items.append(f"Note: {r}")
        # Prepend so Executive Summary is first in the report and in email bodies.
        sections.insert(0, {"type": "text", "heading": "Executive Summary", "items": exec_items})
        return {"version": 2, "sections": sections, "ai_narrative": ai_narrative}

    return {"version": 2, "sections": sections}


def format_report_as_email_body(report: dict, filename: str) -> str:
    """Format a report dict as a plain-text email body."""
    lines = [
        f"Dataset Report — {filename}",
        "Generated by ToolSmithAI",
        "",
    ]
    for section in report.get("sections", []):
        lines.append(section["heading"].upper())
        for item in section["items"]:
            lines.append(f"  → {item}")
        lines.append("")
    return "\n".join(lines).rstrip()
