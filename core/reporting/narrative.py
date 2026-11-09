"""
Executive Report Query Foundation milestone, Part 12 — executive narrative
generation.

The AI receives ONLY the already-computed, already-governed
ExecutiveReportData (KPI values, deltas, chart series, table samples) as
bounded structured text — never raw SQL, never table/column identifiers,
never anything it could use to invent a NEW number. _numbers_are_grounded
below is a hard, mechanical backstop: every numeric token the AI's narrative
contains must already appear somewhere in the structured context it was
given, or the narrative is rejected outright (fail closed, report continues
with narrative=None) — this is deliberately mechanical rather than trusting
the model's own restraint, mirroring core/tools/report_generator.py's own
"OpenAI does NOT replace report generation; it only summarizes existing
output" contract for the unrelated dataset-report narrative path.
"""
from __future__ import annotations

import json
import logging
import re

from core.reporting.report_models import ExecutiveReportData

logger = logging.getLogger(__name__)

_NARRATIVE_SYSTEM_PROMPT = """You are writing a short executive summary of a business report.

You will be given a JSON object containing KPI cards, comparisons, chart data,
and tables — every number in it was computed by governed database queries,
already verified correct.

STRICT rules:
- Use ONLY the numbers given to you. Never compute, estimate, or round to a
  different value than what is given. Never invent a number that is not
  present in the data.
- Distinguish OBSERVED FACT ("revenue increased 12% versus last month") from
  INTERPRETATION. Do not state a cause (e.g. "because marketing improved")
  unless the data itself contains that causal information — it will not.
- Do not speculate about data you were not given.
- Keep it concise: 2-4 sentences for the summary, at most 3 short bullet
  highlights.
- Return ONLY a JSON object: {"summary": str, "highlights": [str, ...]}.
  No markdown, no explanation outside the JSON.
"""

_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*%?")


def _normalize_number_token(tok: str) -> str:
    return tok.replace(",", "").rstrip("%").rstrip(".")


def _numbers_in_text(text: str) -> set[str]:
    return {_normalize_number_token(m) for m in _NUMBER_RE.findall(text) if _normalize_number_token(m)}


def _numbers_are_grounded(narrative_text: str, context_text: str) -> bool:
    """True only when every numeric token in narrative_text also appears
    (after normalization) in context_text. A single-digit token (0-9) is
    exempt — too common in ordinary prose ("a few", list positions) to be
    meaningful hallucination evidence, and rejecting on it would make this
    check too trigger-happy to be usable."""
    narrative_numbers = _numbers_in_text(narrative_text)
    context_numbers = _numbers_in_text(context_text)
    for n in narrative_numbers:
        if len(n.lstrip("-")) <= 1:
            continue
        if n not in context_numbers:
            return False
    return True


def _build_narrative_context(report: ExecutiveReportData) -> str:
    lines: list[str] = [f"Report: {report.title}"]
    for card in report.kpi_cards:
        line = f"- KPI '{card.title}': {card.value}"
        if card.unit:
            line += f" {card.unit}"
        if card.previous_value is not None:
            line += (
                f" (previous: {card.previous_value}, change: {card.absolute_change}, "
                f"{card.percent_change}%, direction: {card.direction})"
            )
        lines.append(line)
    for chart in report.charts:
        lines.append(f"- Chart '{chart.title}' ({chart.chart_type}): labels={list(chart.labels)}")
        for series in chart.series:
            lines.append(f"    series '{series.name}': {list(series.data)}")
    for table in report.tables:
        lines.append(f"- Table '{table.title}': {len(table.rows)} row(s), columns={list(table.columns)}")
        for row in table.rows[:5]:
            lines.append(f"    {dict(row)}")
    if report.warnings:
        lines.append(f"- Limitations: {'; '.join(report.warnings[:5])}")
    return "\n".join(lines)


def generate_executive_narrative(report: ExecutiveReportData) -> dict | None:
    """Returns {"summary": str, "highlights": [str, ...]} or None when
    narrative generation is disabled/unavailable/fails validation — a
    missing narrative is never a report failure, the structured data stands
    on its own."""
    try:
        from core.config import ENABLE_AI_REPORT_NARRATIVE, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS
    except Exception:
        return None

    if not ENABLE_AI_REPORT_NARRATIVE or not OPENAI_API_KEY:
        return None
    if not report.kpi_cards and not report.charts and not report.tables:
        return None

    try:
        import openai as _openai
    except ImportError:
        logger.warning("[report_narrative] openai package not installed; skipping")
        return None

    context = _build_narrative_context(report)

    try:
        client = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _NARRATIVE_SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            max_tokens=400,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = json.loads((response.choices[0].message.content or "").strip())
        summary = str(raw.get("summary") or "").strip()
        highlights = [str(h).strip() for h in (raw.get("highlights") or []) if str(h).strip()][:3]
        if not summary:
            return None

        full_text = summary + " " + " ".join(highlights)
        if not _numbers_are_grounded(full_text, context):
            logger.warning("[report_narrative] rejected: narrative contains a number not present in report data")
            return None

        return {"summary": summary[:800], "highlights": highlights}
    except Exception as exc:
        logger.warning("[report_narrative] failed (%s: %s); report continues without narrative", type(exc).__name__, exc)
        return None
