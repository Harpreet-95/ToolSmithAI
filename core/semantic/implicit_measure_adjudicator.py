from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reconnect Reliability / Implicit Activity Measure milestone.
#
# "Show the most active clients" (or any "most/least <status word>" phrasing)
# has no direct numeric column to rank by — data.query_planning_service's own
# ranking_not_computed warning documents the gap: the status word itself
# consumes the ranking, leaving only a plain filter behind ("IsActive = 1"),
# never an actual ranking.
#
# This module runs ONE bounded, identifier-locked AI pass — the same
# provider/fail-closed contract already established by
# core.semantic.candidate_adjudicator and core.semantic.ai_interpreter (one
# synchronous OpenAI() client, JSON mode, temperature=0) — over a small,
# already-gathered evidence bundle (the grounded entity table's own columns,
# plus its AUTO/APPROVED-trusted related tables only) to propose an implicit
# "activity" measure: a COUNT of related rows, or a recency date. Every
# identifier the model returns is hard-checked against the literal evidence
# it was given; anything else fails closed and the caller keeps today's
# behavior (the disclosed filter-only warning). This module never queries a
# live database, never generates SQL, and never proposes a join that wasn't
# already an AUTO/APPROVED relationship in the evidence bundle it was given.
# ---------------------------------------------------------------------------

try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


class SchemaViolation(ValueError):
    """Raised when the AI response references a table/column that was not
    literally present in the bounded evidence it was given."""


@dataclass(frozen=True)
class ImplicitMeasureResult:
    kind: str  # "related_row_count" | "date_recency"
    related_table_fqn: str | None = None
    related_fk_column: str | None = None
    date_column: str | None = None
    confidence: float = 0.0
    evidence_reference: str | None = None


# This module's own fail-closed bar for trusting a single AI judgment with no
# human review — intentionally a local, named constant rather than reused
# from data.query_planning_service._AUTO_SELECT_MIN_CONFIDENCE, which scores
# a different signal entirely (deterministic name/text match quality, not an
# AI judgment's self-reported confidence).
_MIN_CONFIDENCE = 0.6

_MAX_LIST_ITEMS = 12
_MAX_TEXT_LENGTH = 300

_SYSTEM_PROMPT = """\
You are a data-governance adjudicator for an enterprise analytics platform. The user asked a \
question that ranks a business entity by "most" or "least" some quality (e.g. "most active \
clients"), but the entity's own table has no direct numeric column for that quality. You will be \
given the entity table's own columns and a small, bounded set of its ALREADY-TRUSTED related \
tables (real foreign-key relationships, already approved for use). Your job is to propose ONE \
implicit measure that plausibly represents the quality being asked about, using ONLY this \
evidence — or say none of it is safe to use.

Return ONLY a JSON object with these keys (omit a key entirely if it does not apply):
{
  "kind": "related_row_count" | "date_recency" | "none",
  "related_table_fqn": "<a table_fqn from related_tables, EXACTLY as given, required for related_row_count>",
  "related_fk_column": "<the column_name on that related table that references the entity, EXACTLY as given>",
  "date_column": "<a column_name on the entity table itself, EXACTLY as given, required for date_recency>",
  "confidence": <0.0-1.0>,
  "evidence_reference": "<one short factual sentence justifying the choice>"
}

STRICT RULES:
- Every table_fqn and column_name you return MUST be copied EXACTLY from the evidence you were \
  given. NEVER invent, guess, abbreviate, or reconstruct a table or column name. If you are not \
  certain an identifier is correct, set kind to "none" instead.
- NEVER write SQL. NEVER output anything that looks like a SQL statement.
- "related_row_count" means: rank the entity by how many rows in the chosen related table point \
  back to it (e.g. number of enrollments, signups, sessions, orders, payments) — pick the related \
  table whose row count most plausibly represents genuine business "activity" for this entity, not \
  merely the first one listed. A related table that is clearly a lookup/reference table (few rows, \
  static/categorical in nature) is NOT a good activity signal — prefer a related table that is \
  itself event/transaction-shaped.
- "date_recency" means: rank the entity by its own most recent date column instead (only sensible \
  when no related table plausibly represents activity, but a recency reading is still a fair proxy \
  for "active" — e.g. an UpdateDate/LastActivityDate column on the entity table itself).
- Set kind to "none" (and omit every other field except confidence) when nothing in the evidence \
  plausibly answers the question — this is a normal, safe outcome, not a failure.
- Do not include any keys other than the ones listed above.
"""


def _build_user_prompt(entity_table_fqn: str, table_context: dict, relationships: list[dict]) -> str:
    entity_columns = [c["column_name"] for c in (table_context.get("columns") or [])]
    related_tables = [
        {
            "table_fqn": r["related_table_fqn"],
            "fk_column": r["fk_column"],
            "columns": r.get("columns", [])[:_MAX_LIST_ITEMS],
            "has_primary_key": r.get("has_primary_key", False),
        }
        for r in relationships
    ]
    lines = [
        f'Entity table: "{entity_table_fqn}"',
        f"Entity table's own columns: {json.dumps(entity_columns[:_MAX_LIST_ITEMS * 2], default=str)}",
        "Trusted related tables (JSON):",
        json.dumps(related_tables, default=str),
    ]
    return "\n".join(lines)


def _known_related_fqns(relationships: list[dict]) -> set[str]:
    return {r["related_table_fqn"] for r in relationships}


def _known_related_columns(table_fqn: str, relationships: list[dict]) -> set[str]:
    for r in relationships:
        if r["related_table_fqn"] == table_fqn:
            return set(r.get("columns") or [])
    return set()


def _validate(
    raw: dict[str, Any], entity_table_fqn: str, entity_columns: set[str], relationships: list[dict],
) -> ImplicitMeasureResult | None:
    if not isinstance(raw, dict):
        raise SchemaViolation("AI response is not a JSON object")

    kind = raw.get("kind")
    confidence = raw.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        raise SchemaViolation("confidence must be a number")
    confidence = float(confidence)

    if kind == "none" or kind is None:
        return None

    evidence_reference = raw.get("evidence_reference")
    if evidence_reference is not None and not isinstance(evidence_reference, str):
        raise SchemaViolation("evidence_reference must be a string")
    if isinstance(evidence_reference, str):
        evidence_reference = evidence_reference.strip()[:_MAX_TEXT_LENGTH] or None

    if kind == "related_row_count":
        related_table_fqn = raw.get("related_table_fqn")
        related_fk_column = raw.get("related_fk_column")
        known_fqns = _known_related_fqns(relationships)
        if not isinstance(related_table_fqn, str) or related_table_fqn not in known_fqns:
            raise SchemaViolation(f"related_table_fqn is not one of the given related tables: {related_table_fqn!r}")
        known_cols = _known_related_columns(related_table_fqn, relationships)
        if not isinstance(related_fk_column, str) or related_fk_column not in known_cols:
            raise SchemaViolation(f"related_fk_column is not a real column on {related_table_fqn!r}: {related_fk_column!r}")
        return ImplicitMeasureResult(
            kind="related_row_count", related_table_fqn=related_table_fqn,
            related_fk_column=related_fk_column, confidence=confidence,
            evidence_reference=evidence_reference,
        )

    if kind == "date_recency":
        date_column = raw.get("date_column")
        if not isinstance(date_column, str) or date_column not in entity_columns:
            raise SchemaViolation(f"date_column is not a real column on {entity_table_fqn!r}: {date_column!r}")
        return ImplicitMeasureResult(
            kind="date_recency", date_column=date_column, confidence=confidence,
            evidence_reference=evidence_reference,
        )

    raise SchemaViolation(f"unrecognized kind: {kind!r}")


def infer_activity_measure(
    entity_table_fqn: str,
    table_context: dict,
    relationships: list[dict],
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
    session: Any | None = None,
) -> ImplicitMeasureResult | None:
    """
    Propose an implicit "activity" measure for `entity_table_fqn` from a
    BOUNDED evidence bundle only:

    - `table_context`: the entity table's own already-loaded column metadata
      (same shape data.query_planning_service's table_contexts[fqn] uses).
    - `relationships`: a list of dicts, each
      {"related_table_fqn": str, "fk_column": str, "columns": list[str],
       "has_primary_key": bool} — already filtered by the caller to only
      AUTO/APPROVED-trusted relationships touching entity_table_fqn. This
      function never queries table_relationships itself and never proposes a
      related table that isn't in this list.

    Fail-closed: returns None (never raises to the caller) on missing
    config, the `openai` package being unavailable, any API error, a
    response that fails the identifier-lock validation, or confidence below
    `_MIN_CONFIDENCE`. The caller must treat a `None` result as "no implicit
    measure available" and keep its existing fallback behavior unchanged.

    AI-FIRST CAPABILITY HARDENING milestone — `relationships` being empty no
    longer short-circuits to None before the AI is ever consulted. An entity
    with zero AUTO/APPROVED related tables (live-confirmed: dbo.EDU_Clients
    on a real production database) can still have a perfectly good "date_recency" answer sitting
    on its OWN columns (e.g. LastUpdated) — that path never depends on
    `relationships` at all (see _validate's "date_recency" branch, which only
    checks `entity_columns`). The old guard threw away that evidence before
    the model ever saw it. `_build_user_prompt` already renders an empty
    `related_tables: []` list correctly, and the system prompt already
    instructs the model that "date_recency" is the correct choice "only
    sensible when no related table plausibly represents activity" — exactly
    this case — so no prompt change was needed, only removing this early
    return.
    """
    if not _OPENAI_AVAILABLE:
        logger.info("Implicit measure adjudicator: openai package unavailable, skipping.")
        return None

    from core.config import (
        AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS,
        OPENAI_API_KEY,
        OPENAI_MODEL,
    )

    resolved_key = api_key if api_key is not None else OPENAI_API_KEY
    resolved_model = model or OPENAI_MODEL
    # Reuses candidate_adjudicator's own timeout budget — same kind of
    # bounded, single-shot adjudication call; no need for a second config
    # constant carrying an identical value.
    resolved_timeout = timeout_seconds if timeout_seconds is not None else AI_CANDIDATE_ADJUDICATION_TIMEOUT_SECONDS
    if not resolved_key:
        logger.debug("Implicit measure adjudicator: OPENAI_API_KEY not configured; skipping")
        return None

    entity_columns = {c["column_name"] for c in (table_context.get("columns") or [])}

    def _call_openai() -> ImplicitMeasureResult | None:
        try:
            client = _openai.OpenAI(api_key=resolved_key, timeout=resolved_timeout)
            response = client.chat.completions.create(
                model=resolved_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(entity_table_fqn, table_context, relationships)},
                ],
                max_tokens=400,
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw_content = response.choices[0].message.content or "{}"
            raw = json.loads(raw_content)
            result = _validate(raw, entity_table_fqn, entity_columns, relationships)
            if result is None or result.confidence < _MIN_CONFIDENCE:
                return None
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Implicit measure adjudicator failed for table=%s (%s: %s); no implicit measure used",
                entity_table_fqn, type(exc).__name__, exc,
            )
            return None

    if session is None:
        return _call_openai()

    try:
        evidence_fingerprint = json.dumps(relationships, sort_keys=True, default=str)
    except (TypeError, ValueError):
        evidence_fingerprint = repr(relationships)
    cache_key = ("implicit_activity_measure", entity_table_fqn, resolved_model, evidence_fingerprint)
    return session.get_or_compute_unless_none(cache_key, _call_openai)
