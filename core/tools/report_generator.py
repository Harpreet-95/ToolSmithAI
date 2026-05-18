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
        sections.insert(0, {"heading": "Executive Summary", "items": exec_items})
        return {"sections": sections, "ai_narrative": ai_narrative}

    return {"sections": sections}


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
