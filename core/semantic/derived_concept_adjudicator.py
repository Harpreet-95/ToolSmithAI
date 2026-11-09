from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AI-FIRST CAPABILITY HARDENING milestone, Part 3/4.
#
# Some business concepts a user names ("status", "activity", "tier", ...)
# have no literal column on the entity's own table — they must be DERIVED
# from other columns whose names/nullability/dictionary evidence imply the
# concept (e.g. a "status" for a student is really "CancelID IS NULL AND
# EndDate IS NULL" -> Active; live-confirmed against a real production database,
# dbo.EDU_Student has no Status column at all). This module runs ONE
# bounded, identifier-locked AI pass — the same fail-closed, JSON-mode,
# temperature=0 contract already established by core.semantic.
# candidate_adjudicator and core.semantic.implicit_measure_adjudicator —
# over a small evidence bundle (the entity's own columns, their dictionary
# metadata if any, and cheap already-persisted profiling stats) to decide
# whether `requested_concept` can be SAFELY derived, and if so, to propose a
# small set of mutually-exclusive CATEGORIES each defined by a conjunction of
# real-column NULL/NOT-NULL predicates.
#
# This module NEVER queries a live database, NEVER samples raw row values,
# NEVER writes SQL, and NEVER proposes a column that was not literally in
# the evidence it was given. It is deliberately narrower than a general
# "invent business rules" capability: V1 predicates are existence-shaped
# only (IS NULL / IS NOT NULL) — the exact evidence class the milestone's own
# worked example uses (Cancel/EndDate/DeletedAt/ClosedDate-style nullable
# "did this stop" columns) and the only predicate class that requires no
# guessed literal value. A concept that needs anything richer than that
# (a real value comparison, a computed range) has no safe generic evidence
# source yet and must come back UNSUPPORTED/AMBIGUOUS rather than guess.
#
# Deterministic SQL construction (a CASE WHEN expression) lives downstream in
# data.sql_planning_service/data.sql_generation_service — this module never
# emits SQL, only a structured, fully identifier-locked specification.
# ---------------------------------------------------------------------------

try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


class SchemaViolation(ValueError):
    """Raised when the AI response references a column not literally present
    in the bounded evidence it was given, or an operator outside the safe set."""


@dataclass(frozen=True)
class DerivedCondition:
    column_name: str
    is_null: bool  # True == "IS NULL", False == "IS NOT NULL"


@dataclass(frozen=True)
class DerivedCategory:
    label: str
    conditions: tuple[DerivedCondition, ...]


@dataclass(frozen=True)
class DerivedConceptResult:
    verdict: str  # "SUPPORTED" | "AMBIGUOUS" | "UNSUPPORTED"
    concept: str
    categories: tuple[DerivedCategory, ...] = field(default_factory=tuple)
    else_label: str = "Other"
    confidence: float = 0.0
    evidence: str | None = None


_MIN_CONFIDENCE = 0.6
_MAX_CATEGORIES = 6
_MAX_CONDITIONS_PER_CATEGORY = 3
_MAX_TEXT_LENGTH = 300
_MAX_COLUMNS_IN_PROMPT = 40

_SYSTEM_PROMPT = """\
You are a data-governance adjudicator for an enterprise analytics platform. The user asked a \
question that names a business concept (e.g. "status", "activity tier") which has NO literal, \
identically-named column on the entity's table. Your job is to decide whether this concept can be \
SAFELY DERIVED from the entity's real columns, using ONLY nullability of real columns as evidence \
— never invented business rules, never a guessed literal value comparison.

You will be given the entity table's own columns (name, data type, nullable, and any dictionary \
metadata/profiling stats already recorded for them). A column whose NAME plausibly represents a \
"stopped/ended/cancelled/removed" signal (e.g. CancelID, CancelledDate, EndDate, ClosedDate, \
DeletedAt, TerminationDate) being NULL vs NOT NULL is the canonical safe derivation: NULL across all \
such signals typically means "still active/ongoing"; NOT NULL on any of them typically means the \
requested status category applies (e.g. "Cancelled", "Ended", "Inactive").

Return ONLY a JSON object with these keys:
{
  "verdict": "SUPPORTED" | "AMBIGUOUS" | "UNSUPPORTED",
  "categories": [
    {
      "label": "<short category name, e.g. 'Active'>",
      "conditions": [{"column_name": "<EXACT column name from evidence>", "is_null": true|false}, ...]
    },
    ...
  ],
  "else_label": "<label for rows matching none of the above categories, e.g. 'Other'>",
  "confidence": <0.0-1.0>,
  "evidence": "<one short factual sentence citing the specific column name(s) that justify this>"
}

STRICT RULES:
- Every column_name you return MUST be copied EXACTLY from the evidence you were given. NEVER \
  invent, guess, abbreviate, or reconstruct a column name.
- Only "is_null" conditions are allowed (true or false) — never propose a value comparison \
  (e.g. never "IsActive = 1"); if the only safe signal is a boolean/flag column, still express it \
  through nullability evidence if genuinely nullable, otherwise set verdict to "UNSUPPORTED".
- Categories must be mutually exclusive in ordinary use (list them in priority order — the first \
  matching category wins, like a CASE WHEN chain) and there must be at most 6 of them, each with at \
  most 3 conditions.
- Set verdict to "UNSUPPORTED" when no column's name/evidence plausibly represents the requested \
  concept at all — this is a normal, safe outcome, not a failure.
- Set verdict to "AMBIGUOUS" when more than one materially different, equally plausible derivation \
  exists and you cannot confidently choose between them (e.g. two unrelated nullable columns could \
  each independently mean "status", but combining them is unjustified) — do NOT guess one.
- NEVER write SQL. NEVER output anything that looks like a SQL statement.
- Do not include any keys other than the ones listed above.
"""


def _build_user_prompt(entity_table_fqn: str, requested_concept: str, columns_evidence: list[dict]) -> str:
    lines = [
        f'Entity table: "{entity_table_fqn}"',
        f'Requested business concept: "{requested_concept}"',
        "Entity table's own columns (JSON):",
        json.dumps(columns_evidence[:_MAX_COLUMNS_IN_PROMPT], default=str),
    ]
    return "\n".join(lines)


def _validate(raw: dict[str, Any], concept: str, known_columns: set[str]) -> DerivedConceptResult:
    if not isinstance(raw, dict):
        raise SchemaViolation("AI response is not a JSON object")

    verdict = raw.get("verdict")
    if verdict not in ("SUPPORTED", "AMBIGUOUS", "UNSUPPORTED"):
        raise SchemaViolation(f"unrecognized verdict: {verdict!r}")

    confidence = raw.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        raise SchemaViolation("confidence must be a number")
    confidence = float(confidence)

    evidence = raw.get("evidence")
    if evidence is not None and not isinstance(evidence, str):
        raise SchemaViolation("evidence must be a string")
    if isinstance(evidence, str):
        evidence = evidence.strip()[:_MAX_TEXT_LENGTH] or None

    if verdict != "SUPPORTED":
        return DerivedConceptResult(verdict=verdict, concept=concept, confidence=confidence, evidence=evidence)

    raw_categories = raw.get("categories")
    if not isinstance(raw_categories, list) or not raw_categories:
        raise SchemaViolation("SUPPORTED verdict requires a non-empty categories list")
    if len(raw_categories) > _MAX_CATEGORIES:
        raise SchemaViolation(f"too many categories: {len(raw_categories)}")

    categories: list[DerivedCategory] = []
    for raw_cat in raw_categories:
        if not isinstance(raw_cat, dict):
            raise SchemaViolation("each category must be a JSON object")
        label = raw_cat.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SchemaViolation(f"category label is not a non-empty string: {label!r}")
        raw_conditions = raw_cat.get("conditions")
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise SchemaViolation(f"category {label!r} has no conditions")
        if len(raw_conditions) > _MAX_CONDITIONS_PER_CATEGORY:
            raise SchemaViolation(f"category {label!r} has too many conditions")
        conditions: list[DerivedCondition] = []
        for raw_cond in raw_conditions:
            if not isinstance(raw_cond, dict):
                raise SchemaViolation("each condition must be a JSON object")
            column_name = raw_cond.get("column_name")
            is_null = raw_cond.get("is_null")
            if not isinstance(column_name, str) or column_name not in known_columns:
                raise SchemaViolation(f"condition column_name is not a real column on this table: {column_name!r}")
            if not isinstance(is_null, bool):
                raise SchemaViolation(f"condition is_null must be a boolean: {is_null!r}")
            conditions.append(DerivedCondition(column_name=column_name, is_null=is_null))
        categories.append(DerivedCategory(label=label.strip()[:100], conditions=tuple(conditions)))

    else_label = raw.get("else_label")
    if else_label is not None and not isinstance(else_label, str):
        raise SchemaViolation("else_label must be a string")
    else_label = (else_label or "Other").strip()[:100] or "Other"

    return DerivedConceptResult(
        verdict="SUPPORTED", concept=concept, categories=tuple(categories),
        else_label=else_label, confidence=confidence, evidence=evidence,
    )


def _build_columns_evidence(table_context: dict) -> list[dict]:
    """Assembles the bounded, per-column evidence bundle from table_context's
    own already-loaded sections (data.business_knowledge_service.
    get_table_business_context's "schema"/"dictionary"/"profiling" per
    column) — no new DB read of any kind. profiling's null_percentage/
    distinct_count are cheap, already-persisted aggregate STATS from the
    last profiling snapshot, never raw sample values, never live-queried.
    Every field here is a real, already-computed fact about a real column;
    nothing is invented."""
    columns = table_context.get("columns") or []
    evidence: list[dict] = []
    for c in columns:
        schema = c.get("schema") or {}
        dic = c.get("dictionary") or {}
        prof = c.get("profiling") or {}
        evidence.append({
            "column_name": c["column_name"],
            "data_type": schema.get("data_type"),
            "is_nullable": schema.get("is_nullable"),
            "business_label": dic.get("business_label"),
            "meaning": dic.get("meaning"),
            "semantic_type": dic.get("semantic_type"),
            "is_date": dic.get("is_date"),
            "is_id": dic.get("is_id"),
            "null_percentage": prof.get("null_percentage"),
            "distinct_count": prof.get("distinct_count"),
        })
    return evidence


def infer_derived_concept(
    entity_table_fqn: str,
    table_context: dict,
    requested_concept: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
    session: Any | None = None,
) -> DerivedConceptResult | None:
    """Propose a derived-category definition for `requested_concept` on
    `entity_table_fqn`, from a BOUNDED evidence bundle only (this table's own
    columns — schema + dictionary + cheap persisted profiling stats).

    Fail-closed: returns None (never raises to the caller) on missing
    config, the `openai` package being unavailable, any API error, or a
    response that fails the identifier-lock validation. A non-None result
    with verdict != "SUPPORTED" (AMBIGUOUS/UNSUPPORTED) or confidence below
    `_MIN_CONFIDENCE` is still returned (not coerced to None) so the caller
    can distinguish "we asked and it's genuinely not derivable" from "we
    never got a usable answer at all" — the caller must safely refuse in
    both cases, never guess.
    """
    columns = table_context.get("columns") or []
    if not columns:
        return None
    if not _OPENAI_AVAILABLE:
        logger.info("Derived-concept adjudicator: openai package unavailable, skipping.")
        return None

    from core.config import (
        AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS,
        OPENAI_API_KEY,
        OPENAI_MODEL,
    )

    resolved_key = api_key if api_key is not None else OPENAI_API_KEY
    resolved_model = model or OPENAI_MODEL
    resolved_timeout = timeout_seconds if timeout_seconds is not None else AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS
    if not resolved_key:
        logger.debug("Derived-concept adjudicator: OPENAI_API_KEY not configured; skipping")
        return None

    known_columns = {c["column_name"] for c in columns}
    columns_evidence = _build_columns_evidence(table_context)

    def _call_openai() -> DerivedConceptResult | None:
        try:
            client = _openai.OpenAI(api_key=resolved_key, timeout=resolved_timeout)
            response = client.chat.completions.create(
                model=resolved_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(entity_table_fqn, requested_concept, columns_evidence)},
                ],
                max_tokens=600,
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw_content = response.choices[0].message.content or "{}"
            raw = json.loads(raw_content)
            return _validate(raw, requested_concept, known_columns)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Derived-concept adjudicator failed for table=%s concept=%s (%s: %s); no derivation used",
                entity_table_fqn, requested_concept, type(exc).__name__, exc,
            )
            return None

    if session is None:
        result = _call_openai()
    else:
        try:
            evidence_fingerprint = json.dumps(columns_evidence, sort_keys=True, default=str)
        except (TypeError, ValueError):
            evidence_fingerprint = repr(columns_evidence)
        cache_key = ("derived_concept", entity_table_fqn, requested_concept, resolved_model, evidence_fingerprint)
        result = session.get_or_compute_unless_none(cache_key, _call_openai)

    if result is not None and result.verdict == "SUPPORTED" and result.confidence < _MIN_CONFIDENCE:
        return DerivedConceptResult(
            verdict="AMBIGUOUS", concept=requested_concept, confidence=result.confidence,
            evidence=result.evidence,
        )
    return result
