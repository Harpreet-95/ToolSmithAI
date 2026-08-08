from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EDP Day 1 — Modern Semantic Understanding.
#
# One structured AI pass over the raw question, run BEFORE the deterministic
# planner (core.orchestrator.context_builder._plan_with_autonomous_preparation
# -> data.query_planning_service.plan_business_query). Follows the same
# provider pattern already established by core.ai.providers.openai_provider:
# one synchronous OpenAI() client per call, JSON mode, temperature=0, and a
# fail-closed contract — any missing key, config, package, API error, or
# schema violation returns None so the caller always falls back to the
# existing deterministic parse (core.semantic.concept_resolver.extract_terms)
# alone. This module never queries a live database and never generates SQL.
# ---------------------------------------------------------------------------

try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


# Day 1 rule 2 — the model may return only these keys. Anything else is
# stripped; a raw dict with an unknown key does not by itself fail parsing
# (models are chatty), but every field is independently type/shape-checked
# below before it is trusted.
_ALLOWED_KEYS = frozenset({
    "entities", "measures", "dimensions", "filters", "date_range",
    "sorting", "requested_attributes", "relationship_intent",
    "expected_result_shape", "clarification_required", "clarification_reason",
})

_ALLOWED_RESULT_SHAPES = frozenset({"list", "detail", "count", "aggregate", "unknown"})
_ALLOWED_RELATIONSHIP_INTENTS = frozenset({
    "single_entity", "list_with_related", "scoped_count", "scoped_aggregate",
})
_ALLOWED_SORT_DIRECTIONS = frozenset({"asc", "desc"})

# Day 1 rule 3 — "The model must not generate SQL or physical identifiers."
# Any business-term string containing SQL vocabulary, statement punctuation,
# or a schema.table / table.column shaped token is treated as a schema
# violation and fails the whole interpretation closed (never partially
# trusted) — this is a safety net on top of the prompt instruction, not a
# replacement for it.
_SQL_TOKEN_RE = re.compile(
    r"\b(SELECT|FROM|WHERE|JOIN|GROUP\s+BY|ORDER\s+BY|INSERT|UPDATE|DELETE|DROP|UNION|EXEC)\b"
    r"|--|;|\*/|/\*",
    re.IGNORECASE,
)
_PHYSICAL_ID_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b")

_MAX_LIST_ITEMS = 12
_MAX_TERM_LENGTH = 80


class SchemaViolation(ValueError):
    """Raised when the AI response violates the strict Day-1 output contract."""


@dataclass(frozen=True)
class AIQuestionInterpretation:
    """Validated, provider-agnostic output of the Day-1 semantic interpretation step.

    Every field is a business-level concept — never a table/column identifier
    and never a SQL fragment (enforced by _validate below).
    """

    entities: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    filters: tuple[dict[str, Any], ...] = ()
    date_range: dict[str, Any] | None = None
    sorting: dict[str, Any] | None = None
    requested_attributes: tuple[str, ...] = ()
    relationship_intent: str | None = None
    expected_result_shape: str = "unknown"
    clarification_required: bool = False
    clarification_reason: str | None = None


_SYSTEM_PROMPT = """\
You are a business-question interpreter for an enterprise data analytics platform.
A non-technical user asked a natural-language question about their data. Your job \
is to extract the BUSINESS MEANING of the question — never to write SQL, never to \
name a physical table or column, never to decide which database object answers it.

Return ONLY a JSON object with these keys (omit a key entirely if it does not apply):
{
  "entities": ["<business objects the question is about, e.g. 'candidates', 'invoices'>"],
  "measures": ["<business metrics/quantities requested, e.g. 'placements', 'amount'>"],
  "dimensions": ["<business attributes to break down or display by, e.g. 'recruiter', 'course'>"],
  "filters": [{"attribute": "<business attribute>", "operator": "<=, !=, >, <, contains, between>", "value": "<business value, e.g. 'active', 'Northeast'>"}],
  "date_range": {"label": "<e.g. 'this year', 'this quarter'>", "grain": "<year|quarter|month|week|day>"},
  "sorting": {"by": "<business attribute or measure>", "direction": "asc|desc", "limit": <integer or null>},
  "requested_attributes": ["<specific business fields the user wants shown>"],
  "relationship_intent": "<one of: single_entity, list_with_related, scoped_count, scoped_aggregate>",
  "expected_result_shape": "<one of: list, detail, count, aggregate, unknown>",
  "clarification_required": <true|false>,
  "clarification_reason": "<short reason, only if clarification_required is true>"
}

Strict rules:
- Every value is a BUSINESS TERM taken from natural language, in the user's own vocabulary \
  or the "known business vocabulary" hints given to you — never a table name, column name, \
  schema name, or SQL keyword. If you cannot describe something without naming a physical \
  object, omit it instead.
- Never include SQL syntax (SELECT, FROM, JOIN, WHERE, semicolons, etc.) anywhere in any value.
- clarification_required must be true only when the question is genuinely ambiguous about \
  WHAT the user wants (e.g. two unrelated meanings). It must be false for a well-formed \
  business question, even if you are not sure which database table will answer it — table \
  selection is not your job.
- Only use "entities"/"measures"/"dimensions" values that are singular business concepts \
  (one or two words), not full sentences.
- Do not include any keys other than the ones listed above.
- expected_result_shape/relationship_intent must be "count"/"scoped_count" ONLY when the \
  question uses explicit counting language (e.g. "how many", "count", "number of", "total \
  number", "distinct count"). A plain list/show/display/return request is "list"/ \
  "list_with_related" even when it also carries a status filter — a filter narrows WHICH \
  rows come back, it never turns a list request into a count.

Examples:
Question: "Active invoices"
{
  "entities": ["invoice"],
  "filters": [{"attribute": "status", "operator": "=", "value": "active"}],
  "relationship_intent": "single_entity",
  "expected_result_shape": "list"
}

Question: "List open job orders"
{
  "entities": ["job order"],
  "filters": [{"attribute": "status", "operator": "=", "value": "open"}],
  "relationship_intent": "single_entity",
  "expected_result_shape": "list"
}
"""


def _build_user_prompt(question: str, known_vocabulary: dict[str, list[str]] | None) -> str:
    lines = [f'Question: "{question}"']
    vocab = known_vocabulary or {}
    entities_hint = vocab.get("entities") or []
    domains_hint = vocab.get("domains") or []
    if entities_hint or domains_hint:
        lines.append("")
        lines.append("Known business vocabulary for this data source (prefer these terms when they match):")
        if entities_hint:
            lines.append(f"- Known entities: {', '.join(entities_hint[:_MAX_LIST_ITEMS])}")
        if domains_hint:
            lines.append(f"- Known business domains: {', '.join(domains_hint[:_MAX_LIST_ITEMS])}")
    return "\n".join(lines)


def build_grounding_vocabulary(source_id: int, user_id: str) -> dict[str, list[str]]:
    """Best-effort, bounded vocabulary hint pulled from metadata this source
    has already generated (entity/domain assignment summaries) — the same
    aggregate lookups core.orchestrator.context_builder's own "entity"/
    "domain" evidence adapters already call, reused here rather than a new
    retrieval path. Never raises; returns an empty vocabulary on any failure
    (unowned source, no assignments generated yet, etc.) so a missing
    grounding signal never blocks interpretation.
    """
    entities: list[str] = []
    domains: list[str] = []
    try:
        from data.entity_service import get_entity_summary
        summary = get_entity_summary(source_id=source_id, user_id=user_id)
        if summary:
            entities = [
                name for name in (summary.get("entity_counts") or {}).keys()
                if name and name != "Unknown"
            ][:_MAX_LIST_ITEMS]
    except Exception:  # noqa: BLE001
        logger.debug("AI interpreter: entity vocabulary lookup failed", exc_info=True)

    try:
        from data.domain_service import get_domain_summary
        summary = get_domain_summary(source_id=source_id, user_id=user_id)
        if summary:
            domains = [
                name for name in (summary.get("domain_counts") or {}).keys()
                if name and name != "Unknown"
            ][:_MAX_LIST_ITEMS]
    except Exception:  # noqa: BLE001
        logger.debug("AI interpreter: domain vocabulary lookup failed", exc_info=True)

    return {"entities": entities, "domains": domains}


def _clean_term(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    term = value.strip()
    if not term or len(term) > _MAX_TERM_LENGTH:
        return None
    if _SQL_TOKEN_RE.search(term) or _PHYSICAL_ID_RE.search(term):
        raise SchemaViolation(f"term looks like SQL or a physical identifier: {term!r}")
    return term


def _clean_term_list(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SchemaViolation("expected a list of business terms")
    cleaned: list[str] = []
    for item in raw[:_MAX_LIST_ITEMS]:
        term = _clean_term(item)
        if term:
            cleaned.append(term)
    return tuple(dict.fromkeys(cleaned))


def _clean_filters(raw: Any) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SchemaViolation("expected a list of filter objects")
    cleaned: list[dict[str, Any]] = []
    for item in raw[:_MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            raise SchemaViolation("each filter must be an object")
        attribute = _clean_term(item.get("attribute"))
        if not attribute:
            continue
        operator = item.get("operator")
        if operator is not None and not isinstance(operator, str):
            raise SchemaViolation("filter operator must be a string")
        value = item.get("value")
        if isinstance(value, str):
            _clean_term(value)  # raises on SQL/identifier-shaped filter values
        cleaned.append({"attribute": attribute, "operator": operator, "value": value})
    return tuple(cleaned)


def _clean_date_range(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SchemaViolation("date_range must be an object")
    label = _clean_term(raw.get("label")) if raw.get("label") is not None else None
    grain = raw.get("grain")
    if grain is not None and (not isinstance(grain, str) or grain.lower() not in
                               {"year", "quarter", "month", "week", "day"}):
        raise SchemaViolation(f"invalid date_range grain: {grain!r}")
    if not label and not grain:
        return None
    return {"label": label, "grain": grain.lower() if grain else None}


def _clean_sorting(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SchemaViolation("sorting must be an object")
    by = _clean_term(raw.get("by")) if raw.get("by") is not None else None
    direction = raw.get("direction")
    if direction is not None:
        if not isinstance(direction, str) or direction.lower() not in _ALLOWED_SORT_DIRECTIONS:
            raise SchemaViolation(f"invalid sorting direction: {direction!r}")
        direction = direction.lower()
    limit = raw.get("limit")
    if limit is not None and not isinstance(limit, int):
        raise SchemaViolation("sorting limit must be an integer")
    if not by and direction is None and limit is None:
        return None
    return {"by": by, "direction": direction, "limit": limit}


def _validate(raw: dict[str, Any]) -> AIQuestionInterpretation:
    """Strict Day-1 schema validation. Raises SchemaViolation on any field
    that doesn't conform — the caller treats that identically to a network
    failure (log + return None + fall back to the deterministic parse)."""
    if not isinstance(raw, dict):
        raise SchemaViolation("AI response is not a JSON object")

    relationship_intent = raw.get("relationship_intent")
    if relationship_intent is not None:
        if not isinstance(relationship_intent, str) or relationship_intent not in _ALLOWED_RELATIONSHIP_INTENTS:
            raise SchemaViolation(f"invalid relationship_intent: {relationship_intent!r}")

    expected_shape = raw.get("expected_result_shape", "unknown")
    if not isinstance(expected_shape, str) or expected_shape not in _ALLOWED_RESULT_SHAPES:
        raise SchemaViolation(f"invalid expected_result_shape: {expected_shape!r}")

    clarification_required = raw.get("clarification_required", False)
    if not isinstance(clarification_required, bool):
        raise SchemaViolation("clarification_required must be a boolean")

    clarification_reason = raw.get("clarification_reason")
    if clarification_reason is not None:
        clarification_reason = _clean_term(clarification_reason)

    return AIQuestionInterpretation(
        entities=_clean_term_list(raw.get("entities")),
        measures=_clean_term_list(raw.get("measures")),
        dimensions=_clean_term_list(raw.get("dimensions")),
        filters=_clean_filters(raw.get("filters")),
        date_range=_clean_date_range(raw.get("date_range")),
        sorting=_clean_sorting(raw.get("sorting")),
        requested_attributes=_clean_term_list(raw.get("requested_attributes")),
        relationship_intent=relationship_intent,
        expected_result_shape=expected_shape,
        clarification_required=clarification_required,
        clarification_reason=clarification_reason,
    )


# ---------------------------------------------------------------------------
# Day 2A, Task 1 Part 2 — deterministic contract hardening.
#
# This is a correction of the AI MODEL'S OWN output, independent of the
# prompt examples above (a safety net for questions the examples don't
# literally cover) — never a new AI/LLM call. Verified live that the model
# itself sometimes returns expected_result_shape="count"/relationship_intent
# ="scoped_count" for a plain list question with a status filter (e.g.
# "Active invoices") despite no counting language anywhere in the question.
#
# IMPORTANT — this hardens the CONTRACT, not the current runtime behavior:
# expected_result_shape/relationship_intent are not read by
# data.query_planning_service or any answer-formatting code today (only
# attached to an explanatory request_payload key in
# core.orchestrator.context_builder — see that module's own comment). The
# Day 1 "Active invoices -> count" report was traced to reading this exact
# field directly rather than the system's actual (correct) list-shaped
# decision; nothing here changes today's generated SQL or answer shape.
# This fix exists so that when these fields ARE eventually wired into
# planning, they are already trustworthy.
# ---------------------------------------------------------------------------

_EXPLICIT_COUNT_LANGUAGE_RE = re.compile(
    r"\bhow many\b|\bcount\b|\bnumber of\b|\btotal number\b|\bdistinct count\b",
    re.IGNORECASE,
)


def _enforce_count_language_rule(
    question: str, interpretation: AIQuestionInterpretation,
) -> AIQuestionInterpretation:
    """count/scoped_count may only stand when the question itself uses
    explicit counting language; otherwise a list/show/display/return-shaped
    question that the model mislabeled as a count is downgraded back to
    list/single_entity. Never touches aggregate/scoped_aggregate (SUM/AVG/
    etc. are governed by their own language — "total"/"average" — and are
    out of scope for this narrow rule) or any other field.
    """
    needs_count_language = (
        interpretation.expected_result_shape == "count"
        or interpretation.relationship_intent == "scoped_count"
    )
    if not needs_count_language or _EXPLICIT_COUNT_LANGUAGE_RE.search(question or ""):
        return interpretation

    updates: dict[str, Any] = {}
    if interpretation.expected_result_shape == "count":
        updates["expected_result_shape"] = "list"
    if interpretation.relationship_intent == "scoped_count":
        updates["relationship_intent"] = "single_entity"
    return replace(interpretation, **updates)


def interpret(
    question: str,
    *,
    known_vocabulary: dict[str, list[str]] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> AIQuestionInterpretation | None:
    """Run the Day-1 AI semantic interpretation step for one question.

    Returns None on ANY failure — missing config, missing package, API
    error, malformed JSON, or a schema violation — never raises. Callers
    (core.orchestrator.context_builder) always have a deterministic fallback
    and must never block a question on this step.
    """
    from core.config import (
        AI_QUESTION_INTERPRETER_TIMEOUT_SECONDS,
        OPENAI_API_KEY,
        OPENAI_MODEL,
    )

    resolved_key = api_key if api_key is not None else OPENAI_API_KEY
    resolved_model = model or OPENAI_MODEL
    resolved_timeout = timeout_seconds if timeout_seconds is not None else AI_QUESTION_INTERPRETER_TIMEOUT_SECONDS

    if not resolved_key:
        logger.debug("AI question interpreter: OPENAI_API_KEY not configured; skipping")
        return None
    if not _OPENAI_AVAILABLE:
        logger.warning("AI question interpreter: openai package not installed; skipping")
        return None
    if not question or not question.strip():
        return None

    try:
        client = _openai.OpenAI(api_key=resolved_key, timeout=resolved_timeout)
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(question, known_vocabulary)},
            ],
            max_tokens=600,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw_content = response.choices[0].message.content or "{}"
        raw = json.loads(raw_content)
        return _enforce_count_language_rule(question, _validate(raw))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AI question interpreter failed (%s: %s); falling back to deterministic parse",
            type(exc).__name__, exc,
        )
        return None
