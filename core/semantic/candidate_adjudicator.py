from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Day 2C, Task 2 — AI-Assisted Candidate Adjudication.
#
# One structured AI pass over a BOUNDED, already-gathered set of candidate-
# table evidence (data.semantic_contract_service.gather_candidate_evidence),
# run only when deterministic semantic-contract discovery
# (_discover_entity_contract_deterministic) already landed on NO_CANDIDATE
# for a target business entity. Follows the exact same provider pattern
# already established by core.semantic.ai_interpreter: one synchronous
# OpenAI() client per call, JSON mode, temperature=0, and a fail-closed
# contract — any missing key, config, package, API error, or schema
# violation returns None so the caller always keeps the deterministic
# NO_CANDIDATE result. This module never queries a live database and never
# generates SQL.
#
# Unlike ai_interpreter (which forbids the model from naming ANY physical
# identifier), this step's entire job is picking one — so instead of
# banning identifiers, every structured identifier field the model returns
# (selected_table_fqn, preferred_analytical_view_fqn, identity_key,
# key_attributes, status/date column names, relationships, excluded
# candidates) is hard-checked against the literal table_fqn/column_name
# strings present in the evidence it was given. Anything else is a
# SchemaViolation and the whole adjudication fails closed. Free-text fields
# (grain, evidence_references) are never treated as identifiers downstream
# and are only length-bounded, not identifier-checked.
# ---------------------------------------------------------------------------

try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


_ALLOWED_KEYS = frozenset({
    "selected_table_fqn", "no_safe_selection", "preferred_analytical_view_fqn",
    "grain", "identity_key", "key_attributes", "status_semantics", "date_semantics",
    "relationships", "excluded_candidates", "confidence", "evidence_references",
})

_ALLOWED_DATE_ROLES = frozenset({"created", "started", "completed", "updated"})
_MAX_LIST_ITEMS = 12
_MAX_TEXT_LENGTH = 300


class SchemaViolation(ValueError):
    """Raised when the AI response violates the strict adjudication output
    contract, OR references a table/column identifier that was not literally
    present in the evidence it was given."""


@dataclass(frozen=True)
class AdjudicationResult:
    selected_table_fqn: str | None = None
    no_safe_selection: bool = False
    preferred_analytical_view_fqn: str | None = None
    grain: str | None = None
    identity_key: str | None = None
    key_attributes: tuple[str, ...] = ()
    status_semantics: dict[str, Any] | None = None
    date_semantics: dict[str, str] = field(default_factory=dict)
    relationships: tuple[str, ...] = ()
    excluded_candidates: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0
    evidence_references: tuple[str, ...] = ()


_SYSTEM_PROMPT = """\
You are a data-governance adjudicator for an enterprise analytics platform. You will be \
given a business entity name and factual evidence about a small, bounded set of candidate \
database tables/views that might represent it. Your job is to pick the ONE best canonical \
object for that entity, or say none of them is safe to use.

Return ONLY a JSON object with these keys (omit a key entirely if it does not apply):
{
  "selected_table_fqn": "<the table_fqn of the best candidate, EXACTLY as given in the evidence, or null>",
  "no_safe_selection": <true|false — true when no candidate is safe/confident enough>,
  "preferred_analytical_view_fqn": "<a candidate's table_fqn to use for status/analytical questions, if a view is better for that than the canonical table itself; else null>",
  "grain": "<one sentence describing what one row represents, in business language>",
  "identity_key": "<the column_name (EXACTLY as given in the evidence) that uniquely identifies a row, or null>",
  "key_attributes": ["<column_name values, EXACTLY as given, for the entity's important business attributes>"],
  "status_semantics": {"column_name": "<EXACT column_name or omit>", "verified_values": ["<only values already present in that candidate's status_evidence.verified_values>"]},
  "date_semantics": {"<role>": "<EXACT column_name>"},
  "relationships": ["<table_fqn values, EXACTLY as given, of other candidates/related tables this entity genuinely relates to>"],
  "excluded_candidates": [{"table_fqn": "<EXACT table_fqn>", "reason": "<short reason>"}],
  "confidence": <0.0-1.0>,
  "evidence_references": ["<short factual notes justifying the selection>"]
}

date_semantics is a JSON object whose KEYS must each be exactly one of these four literal \
strings: "created", "started", "completed", "updated" — never any other key, and never the \
combined form "created|started|completed|updated". Include only the roles that genuinely apply, \
e.g. {"started": "StartDate", "completed": "EndDate"}. Omit date_semantics entirely if none apply.

STRICT RULES:
- Every table_fqn and column_name you return MUST be copied EXACTLY from the evidence you were \
  given. NEVER invent, guess, abbreviate, or reconstruct a table or column name. If you are not \
  certain an identifier is correct, omit that field instead.
- NEVER write SQL. NEVER output anything that looks like a SQL statement.
- A candidate whose derivative_flag is set (a naming-penalty hit — backup/export/history/rolling-\
  period copy) must not be selected as canonical unless you explicitly justify it as an \
  analytical view via preferred_analytical_view_fqn, and must otherwise appear in \
  excluded_candidates.
- Prefer a candidate whose grain genuinely matches "one row per <entity>" — a table that is \
  clearly a different, broader entity (e.g. a general roster table when the entity is a specific \
  program/cohort membership) must not be selected just because it scores well on name matching.
- Only put values in status_semantics.verified_values that already appear in that exact \
  candidate's own status_evidence.verified_values in the evidence given to you.
- Set no_safe_selection=true (and omit selected_table_fqn, or set it null) when none of the \
  candidates are trustworthy — this is a normal, safe outcome, not a failure.
- Do not include any keys other than the ones listed above.
"""


def _build_user_prompt(entity_name: str, evidence: list[dict], requested_attributes: list[str] | None) -> str:
    lines = [f'Business entity: "{entity_name}"']
    if requested_attributes:
        lines.append(f"Requested business attributes: {', '.join(requested_attributes[:_MAX_LIST_ITEMS])}")
    lines.append("")
    lines.append("Candidate evidence (JSON):")
    lines.append(json.dumps(evidence, default=str))
    return "\n".join(lines)


def _known_table_fqns(evidence: list[dict]) -> set[str]:
    known: set[str] = set()
    for cand in evidence:
        known.add(cand["table_fqn"])
        for rel in cand.get("relationships") or []:
            fqn = rel.get("table_fqn")
            if fqn:
                known.add(fqn)
    return known


def _known_columns_for(table_fqn: str, evidence: list[dict]) -> set[str]:
    for cand in evidence:
        if cand["table_fqn"] == table_fqn:
            return {c["column_name"] for c in (cand.get("columns") or [])}
    return set()


def _clean_text(value: Any, *, max_length: int = _MAX_TEXT_LENGTH) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaViolation(f"expected a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        return None
    return text[:max_length]


def _validate(raw: dict[str, Any], evidence: list[dict]) -> AdjudicationResult:
    if not isinstance(raw, dict):
        raise SchemaViolation("AI response is not a JSON object")

    known_fqns = _known_table_fqns(evidence)

    no_safe_selection = raw.get("no_safe_selection", False)
    if not isinstance(no_safe_selection, bool):
        raise SchemaViolation("no_safe_selection must be a boolean")

    selected_table_fqn = raw.get("selected_table_fqn")
    if selected_table_fqn is not None:
        if not isinstance(selected_table_fqn, str) or selected_table_fqn not in known_fqns:
            raise SchemaViolation(f"selected_table_fqn is not one of the given candidates: {selected_table_fqn!r}")
    if not no_safe_selection and not selected_table_fqn:
        raise SchemaViolation("either selected_table_fqn or no_safe_selection=true is required")

    preferred_view = raw.get("preferred_analytical_view_fqn")
    if preferred_view is not None:
        if not isinstance(preferred_view, str) or preferred_view not in known_fqns:
            raise SchemaViolation(f"preferred_analytical_view_fqn is not one of the given candidates: {preferred_view!r}")

    selected_columns = _known_columns_for(selected_table_fqn, evidence) if selected_table_fqn else set()
    # Status/date semantics may legitimately live on the preferred
    # analytical view instead of the canonical table itself (e.g. a base
    # membership table plus a separate "current status" view) — both are
    # evidence the model was actually given, so both are valid sources for
    # these two fields specifically. identity_key/key_attributes stay
    # scoped to the canonical table alone (grain-defining, below).
    view_columns = _known_columns_for(preferred_view, evidence) if preferred_view else set()
    status_date_columns = selected_columns | view_columns

    identity_key = raw.get("identity_key")
    if identity_key is not None:
        if not isinstance(identity_key, str) or (selected_table_fqn and identity_key not in selected_columns):
            raise SchemaViolation(f"identity_key is not a real column on the selected candidate: {identity_key!r}")

    key_attributes_raw = raw.get("key_attributes")
    key_attributes: tuple[str, ...] = ()
    if key_attributes_raw is not None:
        if not isinstance(key_attributes_raw, list):
            raise SchemaViolation("key_attributes must be a list")
        cleaned = []
        for item in key_attributes_raw[:_MAX_LIST_ITEMS]:
            if not isinstance(item, str) or (selected_table_fqn and item not in selected_columns):
                raise SchemaViolation(f"key_attributes contains an unknown column: {item!r}")
            cleaned.append(item)
        key_attributes = tuple(dict.fromkeys(cleaned))

    status_semantics_raw = raw.get("status_semantics")
    status_semantics: dict[str, Any] | None = None
    if status_semantics_raw is not None:
        if not isinstance(status_semantics_raw, dict):
            raise SchemaViolation("status_semantics must be an object")
        col = status_semantics_raw.get("column_name")
        if col is not None:
            if not isinstance(col, str) or (selected_table_fqn and col not in status_date_columns):
                raise SchemaViolation(f"status_semantics.column_name is not a real column: {col!r}")
            # The verified-values check is scoped to whichever candidate
            # (canonical or preferred view) actually carries this column —
            # a status column only ever has one owner in the evidence.
            candidate_status_values = set()
            for cand in evidence:
                if cand["table_fqn"] in (selected_table_fqn, preferred_view):
                    se = cand.get("status_evidence") or {}
                    if se.get("column_name") == col:
                        candidate_status_values = set(se.get("verified_values") or [])
                        break
            values_raw = status_semantics_raw.get("verified_values") or []
            if not isinstance(values_raw, list):
                raise SchemaViolation("status_semantics.verified_values must be a list")
            for v in values_raw:
                if not isinstance(v, str) or v not in candidate_status_values:
                    raise SchemaViolation(f"status_semantics.verified_values contains an unverified value: {v!r}")
            status_semantics = {"column_name": col, "verified_values": [str(v) for v in values_raw[:_MAX_LIST_ITEMS]]}

    date_semantics_raw = raw.get("date_semantics")
    date_semantics: dict[str, str] = {}
    if date_semantics_raw is not None:
        if not isinstance(date_semantics_raw, dict):
            raise SchemaViolation("date_semantics must be an object")
        for role, col in date_semantics_raw.items():
            if role not in _ALLOWED_DATE_ROLES:
                raise SchemaViolation(f"invalid date_semantics role: {role!r}")
            if not isinstance(col, str) or (selected_table_fqn and col not in status_date_columns):
                raise SchemaViolation(f"date_semantics[{role!r}] is not a real column: {col!r}")
            date_semantics[role] = col

    relationships_raw = raw.get("relationships")
    relationships: tuple[str, ...] = ()
    if relationships_raw is not None:
        if not isinstance(relationships_raw, list):
            raise SchemaViolation("relationships must be a list")
        cleaned = []
        for item in relationships_raw[:_MAX_LIST_ITEMS]:
            if not isinstance(item, str) or item not in known_fqns:
                raise SchemaViolation(f"relationships contains an unknown table_fqn: {item!r}")
            cleaned.append(item)
        relationships = tuple(dict.fromkeys(cleaned))

    excluded_raw = raw.get("excluded_candidates")
    excluded_candidates: tuple[dict[str, Any], ...] = ()
    if excluded_raw is not None:
        if not isinstance(excluded_raw, list):
            raise SchemaViolation("excluded_candidates must be a list")
        cleaned = []
        for item in excluded_raw[:_MAX_LIST_ITEMS]:
            if not isinstance(item, dict):
                raise SchemaViolation("each excluded_candidates entry must be an object")
            fqn = item.get("table_fqn")
            if not isinstance(fqn, str) or fqn not in known_fqns:
                raise SchemaViolation(f"excluded_candidates contains an unknown table_fqn: {fqn!r}")
            cleaned.append({"table_fqn": fqn, "reason": _clean_text(item.get("reason"))})
        excluded_candidates = tuple(cleaned)

    confidence = raw.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        raise SchemaViolation("confidence must be a number")
    confidence = max(0.0, min(1.0, float(confidence)))

    evidence_references_raw = raw.get("evidence_references")
    evidence_references: tuple[str, ...] = ()
    if evidence_references_raw is not None:
        if not isinstance(evidence_references_raw, list):
            raise SchemaViolation("evidence_references must be a list")
        evidence_references = tuple(
            t for t in (_clean_text(item) for item in evidence_references_raw[:_MAX_LIST_ITEMS]) if t
        )

    return AdjudicationResult(
        selected_table_fqn=selected_table_fqn,
        no_safe_selection=bool(no_safe_selection),
        preferred_analytical_view_fqn=preferred_view,
        grain=_clean_text(raw.get("grain")),
        identity_key=identity_key,
        key_attributes=key_attributes,
        status_semantics=status_semantics,
        date_semantics=date_semantics,
        relationships=relationships,
        excluded_candidates=excluded_candidates,
        confidence=confidence,
        evidence_references=evidence_references,
    )


def adjudicate_candidates(
    entity_name: str,
    evidence: list[dict],
    requested_attributes: list[str] | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> AdjudicationResult | None:
    """Run the Day 2C AI candidate-adjudication step for one entity's
    bounded evidence set (data.semantic_contract_service.
    gather_candidate_evidence's own output).

    Returns None on ANY failure — missing config, missing package, API
    error, malformed JSON, or a schema/identifier violation — never raises.
    Callers (data.semantic_contract_service._discover_entity_contract_via_
    adjudication) always keep the deterministic NO_CANDIDATE result and
    must never block on this step.
    """
    from core.config import (
        AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS,
        OPENAI_API_KEY,
        OPENAI_MODEL,
    )

    resolved_key = api_key if api_key is not None else OPENAI_API_KEY
    resolved_model = model or OPENAI_MODEL
    resolved_timeout = timeout_seconds if timeout_seconds is not None else AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS

    if not resolved_key:
        logger.debug("AI candidate adjudicator: OPENAI_API_KEY not configured; skipping")
        return None
    if not _OPENAI_AVAILABLE:
        logger.warning("AI candidate adjudicator: openai package not installed; skipping")
        return None
    if not evidence:
        return None

    try:
        client = _openai.OpenAI(api_key=resolved_key, timeout=resolved_timeout)
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(entity_name, evidence, requested_attributes)},
            ],
            max_tokens=1200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw_content = response.choices[0].message.content or "{}"
        raw = json.loads(raw_content)
        return _validate(raw, evidence)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AI candidate adjudicator failed for entity=%s (%s: %s); keeping deterministic result",
            entity_name, type(exc).__name__, exc,
        )
        return None
