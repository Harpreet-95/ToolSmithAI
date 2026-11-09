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
    "relationship_semantics", "expected_result_shape",
    "clarification_required", "clarification_reason",
})

_ALLOWED_RESULT_SHAPES = frozenset({"list", "detail", "count", "aggregate", "unknown"})
_ALLOWED_RELATIONSHIP_INTENTS = frozenset({
    "single_entity", "list_with_related", "scoped_count", "scoped_aggregate",
})
_ALLOWED_SORT_DIRECTIONS = frozenset({"asc", "desc"})

# Step 5 — generic relationship_intent normalization.
#
# Live-confirmed failure (smoke test, "top 10 clients by revenue last
# quarter compared to the previous quarter"): the model returned
# relationship_intent="list" — a valid expected_result_shape value, but not
# a valid relationship_intent value — and the WHOLE interpretation was
# discarded even though every other field was correct. Root cause is
# generic, not specific to that one question: relationship_intent and
# expected_result_shape share overlapping vocabulary ("list"/"count"/
# "aggregate"), and any executive question whose surface phrasing leans on
# that shared vocabulary (ranking, plain lists, grouped/aggregate questions
# with no explicit second entity) can trigger the same confusion — verified
# by the parametrized alias tests below, not just the one triggering
# question.
#
# This is a closed, deterministic alias table — not a fuzzy/permissive
# change. A value not in this table (or in _ALLOWED_RELATIONSHIP_INTENTS)
# still fails closed exactly as before; validation strictness is unchanged,
# only its vocabulary tolerance is widened to match what the model's own
# prompt vocabulary can plausibly produce. Same "harden the contract against
# the model's own observed output" pattern as _enforce_count_language_rule
# below (a correction of the model's output, not a new AI call).
_RELATIONSHIP_INTENT_ALIASES: dict[str, str] = {
    # expected_result_shape vocabulary bleeding into this field — the
    # observed failure mode. A plain ranked/grouped/detail list of one
    # entity, with no second related entity named, is "single_entity".
    "list": "single_entity",
    "detail": "single_entity",
    "unknown": "single_entity",
    # Ranking/grouping/comparison phrasing the model sometimes names
    # directly instead of picking one of the four canonical values.
    "ranking": "single_entity",
    "ranked_list": "single_entity",
    "grouped": "single_entity",
    "grouped_aggregate": "scoped_aggregate",
    "comparison": "scoped_aggregate",
    "aggregate": "scoped_aggregate",
    "count": "scoped_count",
}

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
    # AI Physical Planner Coverage Expansion, Part F — a word that describes
    # HOW two entities relate ("enrolled", "assigned", "registered"), as
    # distinct from a requested output column. Only meaningful alongside
    # relationship_intent="list_with_related"; never a physical join
    # condition or column name (same "business term, not identifier"
    # contract as every other field here). Used by the physical planner as
    # evidence for CHOOSING among real schema relationship-path candidates
    # between the named entities, never as an output attribute itself.
    relationship_semantics: tuple[str, ...] = ()
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
  "requested_attributes": ["<specific business fields the user wants shown, e.g. 'email', 'phone' — never a word that only describes HOW two entities relate>"],
  "relationship_intent": "<one of exactly: single_entity, list_with_related, scoped_count, scoped_aggregate — use single_entity for a ranking, grouping, or plain-list question about ONE business object with no second related object named; list_with_related only when a second, distinct related object is explicitly part of the answer>",
  "relationship_semantics": ["<only when relationship_intent is list_with_related: a VERB or PARTICIPLE (often ending in -ed/-ing) describing the ACTION that connects the two entities, e.g. 'enrolled' in 'students and their ENROLLED classes', 'assigned' in 'instructors and their ASSIGNED classes'. NEVER a noun naming a thing to show — a noun (e.g. 'accounts', 'details', 'email') always belongs in requested_attributes instead, never here, even when it appears right next to one of the entity words.>"],
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
- A word that only describes HOW two named entities relate (e.g. "enrolled" in "students \
  and their enrolled classes", "assigned" in "instructors and their assigned classes") goes \
  in relationship_semantics, never in requested_attributes and never treated as a third \
  entity. requested_attributes is reserved for an actual output FIELD the user wants shown \
  (e.g. "student email", "class location").

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

Question: "Top 10 clients by revenue last quarter compared to the previous quarter"
{
  "entities": ["client"],
  "measures": ["revenue"],
  "date_range": {"label": "last quarter", "grain": "quarter"},
  "sorting": {"by": "revenue", "direction": "desc", "limit": 10},
  "relationship_intent": "single_entity",
  "expected_result_shape": "list"
}

Question: "Placements grouped by recruiter"
{
  "entities": ["placement"],
  "dimensions": ["recruiter"],
  "relationship_intent": "single_entity",
  "expected_result_shape": "list"
}

Question: "Which clients have open job orders?"
{
  "entities": ["client", "job order"],
  "filters": [{"attribute": "status", "operator": "=", "value": "open"}],
  "relationship_intent": "list_with_related",
  "expected_result_shape": "list"
}

Question: "Show students and their enrolled classes"
{
  "entities": ["student", "class"],
  "relationship_intent": "list_with_related",
  "relationship_semantics": ["enrolled"],
  "expected_result_shape": "list"
}

Question: "Show students and their user accounts"
{
  "entities": ["student", "user"],
  "requested_attributes": ["account"],
  "relationship_intent": "list_with_related",
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

    # Ranking Semantics Completion milestone, Part 13 (ORIGINAL design,
    # SUPERSEDED below) — the connection-time Business Learning Layer's
    # preliminary map (core.orchestrator.business_learning) used to be
    # rendered here as a separately-labeled UNVALIDATED prompt section.
    #
    # Business Learning Trust Hardening milestone — the preliminary map is
    # now NEVER rendered into the interpreter's prompt at all, in any form
    # (not candidate_measures, not major_entities, not even a single generic
    # `likely_domain` phrase). This was arrived at in three steps, each
    # live-tested against Fixture A/C/E: (1) dropping candidate_measures
    # alone fixed the exact live-reproduced Section O#2 mechanism ("Which
    # account has the highest total order amount?" summing the wrong-but-
    # real Invoices.Amount instead of Orders.TotalAmount) in isolation, but
    # (2) this milestone's own regression testing then showed major_entities
    # ALONE reproduces the SAME class of defect plus the independent O#1
    # WHERE-filter-drop defect ("How many enterprise accounts are there?"
    # losing its WHERE AccountTier='Enterprise' filter). (3) Stripping to
    # ONLY `likely_domain` (one short, non-enumerable descriptive phrase,
    # e.g. "commerce") still reproduced BOTH failures — the Invoices-over-
    # Orders measure pick persisted, and the filter question moved from
    # "wrong answer" to "safe refusal" rather than "correct" — even though
    # a bare domain phrase has no lexical connection to "Invoices" or
    # "AccountTier" at all. That result shows the mechanism is not really
    # about the SPECIFIC business content of the hint: merely inserting any
    # extra "UNVALIDATED hint" prompt block measurably destabilizes the
    # LLM's response on these already-borderline disambiguations (two real
    # "amount" columns on different tables; an implicit categorical filter
    # requiring extra investigation). Since no smaller footprint reliably
    # closed the gap, and this architecture's own principle is that a
    # preliminary hint must NEVER be able to change a final measure/filter/
    # dimension decision, the robust, verifiable choice is to stop injecting
    # it into question-time interpretation entirely. Business Learning's
    # other outputs remain fully intact and useful: the persisted
    # preliminary_business_map itself (operator-facing), the connect-time
    # BusinessLearningTrace (Part 21 observability), and the provenance-
    # tagged dictionary/entity/domain rows ensure_business_evidence writes
    # (still real, still speed up on-demand generation by pre-populating a
    # bounded set of tables — see data.query_planning_service.
    # _dictionary_is_preliminary and data.entity_service.get_entity_summary's
    # exclude_preliminary for how those stay non-definitive elsewhere).
    # Honestly reported in Part 13/17 of the trust-hardening report: this
    # closes off Business Learning's only current live question-answering
    # channel, so its measured live usefulness for Q&A itself is now ~zero —
    # WRONG=0 dominates per this milestone's own Part 21 GO criteria.
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
    # Business Learning Trust Hardening milestone — exclude_preliminary=True
    # on both lookups below. This "known business vocabulary — prefer these
    # terms when they match" block is rendered with no caveat at all (unlike
    # the separately-labeled UNVALIDATED preliminary_business_map block
    # further down), so it must only ever contain entity/domain assignments
    # generated through the normal on-demand path (or human-approved), never
    # an unverified guess written by core.orchestrator.business_learning's
    # blind, pre-question connect-time discovery pass. Live-confirmed this
    # mattered: that pass's own entity_assignments rows, when included here
    # unlabeled, measurably suppressed the qualifier-resolution investigation
    # that a WHERE-filter-bearing question (e.g. "enterprise accounts")
    # otherwise correctly triggers under a fresh connection with no prior
    # vocabulary at all.
    entities: list[str] = []
    domains: list[str] = []
    try:
        from data.entity_service import get_entity_summary
        summary = get_entity_summary(source_id=source_id, user_id=user_id, exclude_preliminary=True)
        if summary:
            entities = [
                name for name in (summary.get("entity_counts") or {}).keys()
                if name and name != "Unknown"
            ][:_MAX_LIST_ITEMS]
    except Exception:  # noqa: BLE001
        logger.debug("AI interpreter: entity vocabulary lookup failed", exc_info=True)

    try:
        from data.domain_service import get_domain_summary
        summary = get_domain_summary(source_id=source_id, user_id=user_id, exclude_preliminary=True)
        if summary:
            domains = [
                name for name in (summary.get("domain_counts") or {}).keys()
                if name and name != "Unknown"
            ][:_MAX_LIST_ITEMS]
    except Exception:  # noqa: BLE001
        logger.debug("AI interpreter: domain vocabulary lookup failed", exc_info=True)

    preliminary_business_map: dict = {}
    try:
        from data.datasource_service import get_preliminary_business_map
        preliminary_business_map = get_preliminary_business_map(source_id, user_id) or {}
    except Exception:  # noqa: BLE001
        logger.debug("AI interpreter: preliminary business map lookup failed", exc_info=True)

    return {
        "entities": entities, "domains": domains,
        "preliminary_business_map": preliminary_business_map,
    }


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
        if not isinstance(relationship_intent, str):
            raise SchemaViolation(f"invalid relationship_intent: {relationship_intent!r}")
        if relationship_intent not in _ALLOWED_RELATIONSHIP_INTENTS:
            relationship_intent = _RELATIONSHIP_INTENT_ALIASES.get(relationship_intent)
            if relationship_intent is None:
                raise SchemaViolation(f"invalid relationship_intent: {raw.get('relationship_intent')!r}")

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
        relationship_semantics=_clean_term_list(raw.get("relationship_semantics")),
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
