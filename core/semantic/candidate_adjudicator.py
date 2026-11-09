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

# Business Learning Candidate Discovery milestone (PRELIMINARY_BUSINESS_LEARNING)
# + View Semantic Discovery milestone (CURATED_VIEW_EVIDENCE) — both tag a
# candidate found only via a discovery-aid source, not validated dictionary/
# schema evidence. Either tag must always defer a rank#1/only candidate to
# real AI adjudication rather than a deterministic no-AI fast path — see the
# two _PROVENANCE_REQUIRES_AI checks below and the system prompt's own
# candidate_provenance guidance.
_PROVENANCE_REQUIRES_AI = frozenset({"PRELIMINARY_BUSINESS_LEARNING", "CURATED_VIEW_EVIDENCE"})


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
- Each candidate's own candidate_business_role, human_readable_descriptor_columns, \
  person_reference_columns, deterministic_rank, and authority_reasons are independent, already-\
  validated structural evidence (not opinion) — read them before deciding. A candidate whose \
  candidate_business_role is "assignment_or_event" (its columns are dominated by \
  person_reference_columns — e.g. a StudentID/InstructorID/UserID-shaped column — with no genuine \
  human_readable_descriptor_columns of its own) represents a per-person ASSIGNMENT or EVENT record, \
  not a browsable catalog entity for the requested business concept, even when its table NAME \
  literally contains the entity word. Do not select an "assignment_or_event" candidate over a \
  "catalog_or_entity" candidate merely because the assignment table's raw name is a closer lexical \
  match, its row count is larger, or its deterministic_rank number happens to be close — that is \
  exactly the kind of name-substring bias this evidence exists to correct. You MAY still select the \
  lower-ranked or name-weaker candidate when it is genuinely "catalog_or_entity" and the higher-\
  ranked one is "assignment_or_event", or when relationships/domain evidence concretely justifies \
  it — but in that case evidence_references must name the SPECIFIC evidence (not a generic \
  restatement of the table name) that justifies overriding candidate_business_role/deterministic_rank.
- Only put values in status_semantics.verified_values that already appear in that exact \
  candidate's own status_evidence.verified_values in the evidence given to you.
- A candidate whose restricted_view_evidence.restriction_detected is true represents only a \
  business-filtered SLICE of a broader concept (its own definition contains a hardcoded WHERE \
  literal and/or a row-limiting clause — e.g. one specific program, one status, one date range) — \
  NOT the full population an unqualified request for the entity means. Do not select it as the \
  canonical entity merely because its name is the closest lexical match to the business term. If \
  another candidate in the evidence is a lineage_source_view's own broader source (marked via \
  lineage_source_view being null on it while another candidate names it as their \
  lineage_source_view), or is otherwise clearly the unrestricted version of the same concept, \
  prefer that broader candidate instead. A restricted view MAY still be selected when the \
  question's own wording explicitly asks for the specific slice it represents (e.g. names the \
  program/status/date range the view is filtered to) — evidence_references must then say so \
  explicitly. If no unrestricted candidate is available and you cannot confirm the restricted \
  view's slice matches what was asked, set no_safe_selection=true rather than silently returning \
  partial-population data as if it were complete.
- Set no_safe_selection=true (and omit selected_table_fqn, or set it null) when none of the \
  candidates are trustworthy — this is a normal, safe outcome, not a failure.
- Do not include any keys other than the ones listed above.
- Only include status_semantics for a candidate whose OWN status_evidence in the evidence you \
  were given is non-null and actually has a column_name — never invent a status column for an \
  entity that has no status/subscription/membership-state concept (e.g. a plain count-only \
  entity). Omit status_semantics entirely rather than guess.
- A candidate whose candidate_provenance field is "PRELIMINARY_BUSINESS_LEARNING" was suggested \
  by an unvalidated, connect-time automated guess — it is a hint about WHERE to look, never \
  evidence about WHAT is true. Its mere presence in this evidence list must NOT by itself raise \
  your confidence or make you more willing to select it. Judge it using exactly the same \
  structural evidence (columns, candidate_business_role, name_score, relationships, row counts, \
  status/restriction evidence) you would use for any other candidate with candidate_provenance \
  absent, and select it only when that independent evidence justifies it on its own merits — the \
  same bar every other candidate must clear. A candidate with no candidate_provenance field (the \
  normal case) was found by real search/vocabulary/mapping evidence and needs no special caution.
- A candidate whose candidate_provenance field is "CURATED_VIEW_EVIDENCE" was found only through a \
  real, human-authored VIEW's own SQL definition and the base table(s) it structurally reads from \
  (see referenced_tables) — a genuine structural signal, stronger than an unvalidated guess, but \
  still not a human-approved dictionary mapping. Treat it the same way as \
  "PRELIMINARY_BUSINESS_LEARNING" above: its presence must NOT by itself raise your confidence past \
  what the candidate's own real evidence (columns, candidate_business_role, relationships, \
  status/restriction evidence) independently supports.
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

    # V1.3 — identity_key repair. Live-observed (recurring, reproduced):
    # the model sometimes names a plausible-looking but non-existent column
    # as identity_key (e.g. a hyphenated 'D-LocationCode' that isn't a real
    # column) while everything that actually defines WHAT was selected
    # (selected_table_fqn, no_safe_selection) is perfectly valid. Before this
    # fix, that single bad field failed the ENTIRE adjudication closed
    # (_call_openai's outer except Exception caught the SchemaViolation and
    # discarded the whole result, falling back to the weaker deterministic
    # candidate) even though the table choice itself was never in question —
    # a FORMAT_VALIDATION_FAILURE on a supplementary annotation, not a
    # NO_SAFE_SELECTION/semantic failure. identity_key is only ever consumed
    # downstream as "use if present and already re-validated against real
    # column names, else None" (data.semantic_contract_service.py:2612-2613
    # independently re-checks `result.identity_key in column_names` before
    # ever using it) — None is already a fully-supported, safe state, so
    # dropping an invalid value here (same pattern as status_semantics/
    # date_semantics below) never fabricates or trusts an unverified
    # identifier; it only prevents one bad supplementary field from
    # discarding an otherwise-valid, evidence-grounded table selection.
    identity_key = raw.get("identity_key")
    if identity_key is not None:
        try:
            if not isinstance(identity_key, str) or (selected_table_fqn and identity_key not in selected_columns):
                raise SchemaViolation(f"identity_key is not a real column on the selected candidate: {identity_key!r}")
        except SchemaViolation as exc:
            logger.warning("AI candidate adjudicator: dropping invalid identity_key (%s)", exc)
            identity_key = None

    # V1.3 — key_attributes repair. Live-observed alongside the identity_key
    # fix above (SAME "employee" entity, same evidence bundle): the model
    # can hallucinate one bad column name inside an otherwise-fine list
    # (e.g. 'In-FirstName' — not a real column) — key_attributes is a
    # supplementary display-attribute list ABOUT the already-validated
    # selected_table_fqn, not something that defines WHAT was selected, so
    # it gets the SAME "drop the bad item(s), keep the rest" treatment as
    # identity_key/date_semantics rather than failing the whole
    # adjudication closed over one bad list entry. A non-list value is
    # likewise degraded (treated as empty), matching date_semantics_raw's
    # own non-object handling below, instead of raising.
    key_attributes_raw = raw.get("key_attributes")
    key_attributes: tuple[str, ...] = ()
    if key_attributes_raw is not None:
        if not isinstance(key_attributes_raw, list):
            logger.warning("AI candidate adjudicator: dropping non-list key_attributes (%r)", key_attributes_raw)
        else:
            cleaned = []
            for item in key_attributes_raw[:_MAX_LIST_ITEMS]:
                if not isinstance(item, str) or (selected_table_fqn and item not in selected_columns):
                    logger.warning(
                        "AI candidate adjudicator: dropping invalid key_attributes entry (%r)", item,
                    )
                    continue
                cleaned.append(item)
            key_attributes = tuple(dict.fromkeys(cleaned))

    # status_semantics/date_semantics are the two fields the adjudication
    # prompt explicitly documents as "omit if it does not apply" — a
    # question like "How many students are there?" has no status/date
    # concept to report at all. Live-observed: the model sometimes still
    # emits an invalid value for one of these anyway (e.g. a hallucinated
    # status column on an entity whose own status_evidence is null). Unlike
    # selected_table_fqn/identity_key/key_attributes/relationships/
    # excluded_candidates (which define WHAT was selected, and stay fully
    # fail-closed below), these two are supplementary annotations every
    # downstream consumer already treats as optional
    # (core.orchestrator.ai_physical_planning and
    # data.semantic_contract_service._discover_entity_contract_via_
    # adjudication both read them via `result.status_semantics or {}` /
    # `.get(...)`) — so an invalid value here is dropped with a warning
    # instead of discarding an otherwise-correct table selection.
    status_semantics_raw = raw.get("status_semantics")
    status_semantics: dict[str, Any] | None = None
    if status_semantics_raw is not None:
        try:
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
        except SchemaViolation as exc:
            logger.warning("AI candidate adjudicator: dropping invalid status_semantics (%s)", exc)
            status_semantics = None

    date_semantics_raw = raw.get("date_semantics")
    date_semantics: dict[str, str] = {}
    if date_semantics_raw is not None:
        if not isinstance(date_semantics_raw, dict):
            logger.warning("AI candidate adjudicator: dropping non-object date_semantics (%r)", date_semantics_raw)
        else:
            for role, col in date_semantics_raw.items():
                try:
                    if role not in _ALLOWED_DATE_ROLES:
                        raise SchemaViolation(f"invalid date_semantics role: {role!r}")
                    if not isinstance(col, str) or (selected_table_fqn and col not in status_date_columns):
                        raise SchemaViolation(f"date_semantics[{role!r}] is not a real column: {col!r}")
                    date_semantics[role] = col
                except SchemaViolation as exc:
                    logger.warning("AI candidate adjudicator: dropping invalid date_semantics entry (%s)", exc)
                    continue

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


# ---------------------------------------------------------------------------
# LIVE PROGRAM ADJUDICATION MILESTONE, Part 6 — deterministic backstop.
#
# Does not trust prompt compliance alone: strengthening the system prompt
# (Part 3/5, the STRICT RULES bullet above referencing candidate_business_
# role) makes correct behavior MORE likely, but an LLM call can still
# disregard it. This is a bounded, deterministic, evidence-only check run
# on every real adjudication result (never invoked for a no_safe_selection
# result, which is already the safe outcome) — it never SUBSTITUTES a
# different table (Part 6: "Do NOT simply force candidate A"), it only
# REJECTS a selection that is inconsistent with the evidence the model was
# itself given, sending the caller down its existing no-safe-selection ->
# disqualify/refuse path (the same safe outcome a genuine no_safe_selection
# already produces).
# ---------------------------------------------------------------------------

def _apply_business_role_backstop(
    result: AdjudicationResult | None, evidence: list[dict],
) -> AdjudicationResult | None:
    if result is None or result.no_safe_selection or not result.selected_table_fqn:
        return result

    by_fqn = {e.get("table_fqn"): e for e in evidence}
    selected_evidence = by_fqn.get(result.selected_table_fqn)
    if selected_evidence is None or selected_evidence.get("candidate_business_role") != "assignment_or_event":
        return result  # nothing to backstop -- selected candidate isn't assignment/event-shaped

    catalog_alternatives = [
        e for e in evidence
        if e.get("candidate_business_role") == "catalog_or_entity"
        and e.get("table_fqn") != result.selected_table_fqn
    ]
    if not catalog_alternatives:
        return result  # no catalog/entity candidate exists to have been passed over

    selected_rank = selected_evidence.get("deterministic_rank")
    better_ranked_catalog = [
        e for e in catalog_alternatives
        if e.get("deterministic_rank") is not None
        and selected_rank is not None
        and e["deterministic_rank"] < selected_rank
    ]
    if not better_ranked_catalog:
        return result  # the assignment/event candidate was ranked at least as strong deterministically

    # The model selected an assignment/event-shaped table over a better-
    # ranked catalog/entity candidate. Only accept this when the model
    # itself explicitly acknowledged the alternative (named it in
    # `relationships` — the one structured field this contract already
    # requires for "this entity genuinely relates to" reasoning) — a bare
    # selection with no acknowledgment of the passed-over catalog candidate
    # is exactly the "restated the table name, no real justification" shape
    # Part 5/6 says must not be trusted on prompt compliance alone.
    acknowledged = {fqn for fqn in result.relationships} | {
        c.get("table_fqn") for c in result.excluded_candidates if isinstance(c, dict)
    }
    if any(e.get("table_fqn") in acknowledged for e in better_ranked_catalog):
        return result

    logger.info(
        "AI candidate adjudicator backstop: rejected selection of assignment/event-shaped %s "
        "over better-ranked catalog/entity candidate(s) %s with no acknowledging evidence "
        "(entity=%s) — deferring to safe refusal/disqualification.",
        result.selected_table_fqn, [e["table_fqn"] for e in better_ranked_catalog],
        by_fqn.get(result.selected_table_fqn, {}).get("table_fqn"),
    )
    return None


# ---------------------------------------------------------------------------
# 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — restricted-view backstop.
# The system prompt's own restricted_view_evidence rule (above) makes
# correct behavior MORE likely but, live-confirmed, an LLM call can still
# select a restricted VIEW as canonical despite the evidence and despite an
# unrestricted lineage-root candidate sitting right next to it in the same
# evidence list. Deterministic, evidence-only, same idiom as
# _apply_business_role_backstop directly above: never invents a table that
# isn't already in the evidence, never runs on a no_safe_selection result.
# Unlike that backstop (which only rejects), this one MAY substitute — but
# only to the one candidate data.semantic_contract_service.
# gather_candidate_evidence itself already verified, via a direct read of
# the restricted view's own definition text, is that view's structural
# FROM-clause source (evidence.lineage_source_view == the selected view).
# That is a structural fact this codebase confirmed independently, not an
# LLM opinion being second-guessed — categorically different from "forcing
# candidate A" on a hunch. Ambiguous or unavailable lineage falls back to
# rejection (None), exactly like the business-role backstop, which sends
# the caller down its existing safe-refusal path rather than trusting a
# confident-but-unverifiable substitution.
# ---------------------------------------------------------------------------

def _apply_restricted_view_backstop(
    result: AdjudicationResult | None, evidence: list[dict],
) -> AdjudicationResult | None:
    if result is None or result.no_safe_selection or not result.selected_table_fqn:
        return result

    by_fqn = {e.get("table_fqn"): e for e in evidence}
    selected_evidence = by_fqn.get(result.selected_table_fqn)
    restriction = (selected_evidence or {}).get("restricted_view_evidence")
    if not restriction or not restriction.get("restriction_detected"):
        return result  # nothing to backstop -- selected candidate isn't a restricted view

    lineage_alternatives = [
        e for e in evidence
        if e.get("lineage_source_view") == result.selected_table_fqn
        and not ((e.get("restricted_view_evidence") or {}).get("restriction_detected"))
    ]
    # RESTRICTED-VIEW BACKSTOP LINEAGE FIX — data.semantic_contract_service.
    # gather_candidate_evidence only tags a candidate with lineage_source_view
    # when IT added that candidate as a synthetic lineage-root entry, which
    # it skips whenever the root table is already present in evidence for
    # any other reason (already a direct-match candidate, or already claimed
    # as another restricted view's synthetic root — first view in wins).
    # That means a later restricted view sharing the SAME root as an earlier
    # one, or a root that was independently retrieved as its own candidate,
    # would show zero lineage_source_view-tagged alternatives here even
    # though the correct unrestricted root IS in evidence. The selected
    # view's own lineage_root_fqn (recorded unconditionally, unlike the
    # synthetic-entry tag) closes that gap without weakening the existing
    # ambiguity check below -- it only ever contributes one specific,
    # already-computed root, and is skipped if that root is already counted.
    root_fqn = (selected_evidence or {}).get("lineage_root_fqn")
    if root_fqn and root_fqn not in {a.get("table_fqn") for a in lineage_alternatives}:
        root_evidence = by_fqn.get(root_fqn)
        if root_evidence is not None and not (
            (root_evidence.get("restricted_view_evidence") or {}).get("restriction_detected")
        ):
            lineage_alternatives.append(root_evidence)
    if len(lineage_alternatives) == 1:
        root_evidence = lineage_alternatives[0]
        root_fqn = root_evidence["table_fqn"]
        logger.info(
            "AI candidate adjudicator backstop: substituting restricted-view selection %s "
            "-> its own verified structural lineage source %s.",
            result.selected_table_fqn, root_fqn,
        )
        # identity_key/key_attributes were scoped to the ORIGINAL selection's
        # own columns and must not carry over to a different table's schema
        # — re-derived from the substituted table's OWN evidence (the same
        # primary_key/descriptor-column evidence gather_candidate_evidence
        # already computed for it), never guessed.
        root_primary_key = (root_evidence.get("primary_key") or {}).get("column_name")
        root_key_attrs = tuple((root_evidence.get("human_readable_descriptor_columns") or [])[:8])
        return AdjudicationResult(
            selected_table_fqn=root_fqn,
            no_safe_selection=False,
            preferred_analytical_view_fqn=result.selected_table_fqn,
            grain=result.grain,
            identity_key=root_primary_key,
            key_attributes=root_key_attrs,
            status_semantics=None,
            date_semantics={},
            relationships=result.relationships,
            excluded_candidates=result.excluded_candidates + (
                {"table_fqn": result.selected_table_fqn,
                 "reason": "Restricted view (definition indicates a filtered business slice); "
                           "substituted for its own verified structural lineage source."},
            ),
            confidence=result.confidence,
            evidence_references=result.evidence_references + (
                f"Backstop: {result.selected_table_fqn} is a restricted view; substituted its "
                f"verified FROM-clause source {root_fqn}.",
            ),
        )

    logger.info(
        "AI candidate adjudicator backstop: rejected restricted-view selection %s "
        "(no unambiguous unrestricted lineage source found: %d candidate(s)) — "
        "deferring to safe refusal/disqualification.",
        result.selected_table_fqn, len(lineage_alternatives),
    )
    return None


# ---------------------------------------------------------------------------
# PERFORMANCE PHASE D — deterministic, evidence-only fast path for the
# single narrowest, most-conservative shape: gather_candidate_evidence
# returned EXACTLY ONE candidate, and that candidate's own evidence
# contains no signal that would ever have made the AI adjudicator's
# judgment necessary in the first place. This is NOT a replacement for AI
# adjudication -- every condition below exists because live evidence (this
# milestone's shadow-mode run against the real 37-question suite, see the
# module-level SHADOW_LOG machinery) proved the AI adjudicator selects the
# sole candidate 100% of the time when ALL of these hold, and because each
# condition mirrors a safety check this codebase ALREADY trusts elsewhere
# (the business-role/restricted-view backstops above, and
# core.orchestrator.ai_physical_planning's own _MIN_TRUSTED_NAME_SCORE
# weak-match gate) rather than inventing a new one.
#
# Scope: only ever consulted by callers that pass simple_selection_only=
# True to adjudicate_candidates below -- confirmed by direct read that
# EVERY core.orchestrator.ai_physical_planning call site only ever consumes
# .no_safe_selection/.selected_table_fqn/.confidence/.evidence_references
# from the returned AdjudicationResult, never .grain/.date_semantics/
# .identity_key/.status_semantics/.relationships/.preferred_analytical_
# view_fqn -- fields only an LLM can meaningfully populate. data.semantic_
# contract_service._discover_entity_contract_via_adjudication (the ONE
# other caller, which persists a full contract needing those rich fields)
# never passes this flag and is completely unaffected by this milestone.
# ---------------------------------------------------------------------------

# Mirrors core.orchestrator.ai_physical_planning._MIN_TRUSTED_NAME_SCORE
# exactly (not a new number) -- the same bar this codebase already trusts
# to distinguish a real table-name match from lexical noise.
_FAST_PATH_MIN_NAME_SCORE = 0.5


def _evaluate_fast_path_candidate(
    entity_name: str, evidence: list[dict], requested_attributes: list[str] | None,
) -> tuple[AdjudicationResult | None, str]:
    """Pure, deterministic, no-AI-call evaluation of whether `evidence`
    (the exact bundle the AI adjudicator would otherwise be shown) is
    unambiguous enough to skip the AI call entirely.

    Returns (result, reason): result is a fully-formed AdjudicationResult
    when eligible, else None; reason is always a short, human-readable
    string (used for observability, never PII/business values -- only
    table_fqns, which are already-cataloged schema identifiers, and
    generic condition names).

    Deliberately narrow: candidate_count must be exactly 1. A dominant-
    but-not-sole candidate (multiple candidates, one clearly best) is
    Part 6's "dominant-candidate fast path" -- explicitly NOT implemented
    this milestone (only to be attempted after this narrower class is
    proven safe in production), so len(evidence) != 1 always falls
    through to the real AI adjudicator, unconditionally.
    """
    if len(evidence) != 1:
        return None, f"ineligible: candidate_count={len(evidence)} (only candidate_count==1 is in scope)"

    cand = evidence[0]
    fqn = cand.get("table_fqn")
    if not fqn:
        return None, "ineligible: candidate evidence missing table_fqn"

    # Business Learning Candidate Discovery milestone, Part 3c — a
    # candidate whose ONLY discovery signal was the unvalidated preliminary
    # business map must never be auto-selected by a deterministic no-AI
    # fast path: skipping AI here would mean nothing ever weighs the other
    # structural evidence against the fact that this candidate is merely a
    # suggestion, which is exactly the judgment call adjudicate_candidates'
    # own system-prompt guidance (candidate_provenance) exists to make.
    # Deliberately checked before any other condition below.
    if cand.get("candidate_provenance") in _PROVENANCE_REQUIRES_AI:
        return None, f"ineligible: candidate_provenance={cand.get('candidate_provenance')!r}, defers to AI"

    # Condition: requested_attributes present -> whether this candidate's
    # own columns actually satisfy each one is exactly the kind of judgment
    # call this narrowest fast path defers to AI rather than reimplementing.
    if requested_attributes:
        return None, "ineligible: requested_attributes present, defers to AI"

    # Condition: positive, non-assignment business-role evidence. A single
    # "assignment_or_event"-or-"unknown"-role candidate is exactly the
    # shape that produced the real live Program regression (D4b) --
    # gather_candidate_evidence's own retrieval returned only wrong,
    # assignment-shaped candidates with the true catalog table absent from
    # evidence entirely, so no backstop could ever have caught it. Fast-path
    # eligibility requires the SAME "catalog_or_entity" role the AI system
    # prompt itself treats as the trustworthy shape.
    role = cand.get("candidate_business_role")
    if role != "catalog_or_entity":
        return None, f"ineligible: candidate_business_role={role!r} (requires catalog_or_entity)"

    # Condition: not a naming-penalty (derivative/backup/export/history)
    # hit -- the AI system prompt's own rule this codebase already trusts.
    if cand.get("derivative_flag"):
        return None, f"ineligible: derivative_flag={cand.get('derivative_flag')!r}"

    # Condition: not a restricted VIEW, and restriction status must be
    # POSITIVELY known (not merely absent/unknown) whenever the candidate
    # is a VIEW at all -- mirrors _apply_restricted_view_backstop's own
    # "no lineage source == reject" discipline: incomplete evidence about
    # restriction status is never treated as evidence of safety.
    object_type = cand.get("object_type")
    restriction = cand.get("restricted_view_evidence")
    if object_type == "VIEW":
        if restriction is None:
            return None, "ineligible: VIEW candidate with unknown restriction status"
        if restriction.get("restriction_detected"):
            return None, "ineligible: VIEW candidate is a restricted view"

    # Condition: not a known-zero-row (source-data-unusable) candidate.
    # None (unknown -- profiling depth varies) is tolerated for a real
    # TABLE, since this codebase's own profiling doesn't always cover
    # every table; explicit 0 is never tolerated.
    approx_row_count = cand.get("approx_row_count")
    if approx_row_count == 0:
        return None, "ineligible: approx_row_count=0 (source-data-unusable)"

    # Condition: lexical name-relevance evidence must clear the SAME bar
    # ai_physical_planning.py's own downstream weak-match gate already
    # requires -- a candidate this weak would be rejected a few lines later
    # regardless of who selected it, so trusting it here would only mean
    # doing wasted, riskier work instead of a clean "defer to AI" signal.
    name_score = cand.get("name_score") or 0.0
    if name_score < _FAST_PATH_MIN_NAME_SCORE:
        return None, f"ineligible: name_score={name_score:.3f} below {_FAST_PATH_MIN_NAME_SCORE}"

    # Condition: deterministic_rank must genuinely be 1 -- defensive
    # sanity check (trivially true for a single-candidate evidence set,
    # kept explicit rather than assumed).
    if cand.get("deterministic_rank") not in (1, None):
        return None, f"ineligible: deterministic_rank={cand.get('deterministic_rank')!r} (expected 1)"

    result = AdjudicationResult(
        selected_table_fqn=fqn,
        no_safe_selection=False,
        confidence=float(cand.get("score") or 0.0),
        evidence_references=(
            f"Fast path: single unambiguous catalog_or_entity candidate for {entity_name!r} "
            f"(name_score={name_score:.3f}, no restriction/derivative/zero-row evidence) — "
            "no AI adjudication call made.",
        ),
    )
    return result, "eligible: single unambiguous catalog_or_entity candidate"


# ---------------------------------------------------------------------------
# PERFORMANCE PHASE D3 — role-aware MULTI-candidate dominance gate. Distinct
# from _evaluate_fast_path_candidate above (which is scoped to
# candidate_count==1 only, proven safe but empirically inert -- zero real
# calls in production ever have exactly one candidate). This function
# evaluates the wider, real shape (5-12 candidates) but ONLY for roles
# already proven safe by real shadow-mode evidence (_ROLE_SAFE_FOR_
# DOMINANCE, empty by default -- see the Phase D3 final report for which
# roles, if any, were added and the evidence that justified it).
#
# Deliberately reuses the SAME two safety checks _apply_business_role_
# backstop/_apply_restricted_view_backstop already apply to a real AI
# selection, evaluated here BEFORE any AI call rather than after one --
# not a new heuristic, the existing trusted logic run one step earlier.
# Score margin is deliberately NOT a condition anywhere in this function:
# Phase D2's real shadow-mode evidence proved it does not separate safe
# from unsafe cases in this system (the one real necessary override,
# "course fee", had a LARGER margin than several genuinely safe cases).
# ---------------------------------------------------------------------------

def _evaluate_role_dominance_candidate(
    entity_name: str, evidence: list[dict], requested_attributes: list[str] | None, role_context: str | None,
) -> tuple[AdjudicationResult | None, str]:
    if role_context not in _ROLE_SAFE_FOR_DOMINANCE:
        return None, f"ineligible: role_context={role_context!r} not in empirically-safe role set"

    if len(evidence) < 1:
        return None, "ineligible: no candidates"

    if requested_attributes:
        return None, "ineligible: requested_attributes present, defers to AI"

    top1 = evidence[0]
    fqn = top1.get("table_fqn")
    if not fqn:
        return None, "ineligible: candidate evidence missing table_fqn"

    # Business Learning Candidate Discovery milestone, Part 3c — same
    # guard as _evaluate_fast_path_candidate above: a rank#1 candidate
    # whose only discovery signal was the unvalidated preliminary business
    # map must always defer to real AI adjudication, never be dominance-
    # selected by a deterministic no-AI path.
    if top1.get("candidate_provenance") in _PROVENANCE_REQUIRES_AI:
        return None, f"ineligible: candidate_provenance={top1.get('candidate_provenance')!r}, defers to AI"

    if top1.get("deterministic_rank") not in (1, None):
        return None, f"ineligible: rank#1 candidate deterministic_rank={top1.get('deterministic_rank')!r} (expected 1)"

    if top1.get("derivative_flag"):
        return None, f"ineligible: derivative_flag={top1.get('derivative_flag')!r}"

    object_type = top1.get("object_type")
    restriction = top1.get("restricted_view_evidence")
    if object_type == "VIEW":
        if restriction is None:
            return None, "ineligible: rank#1 VIEW candidate with unknown restriction status"
        if restriction.get("restriction_detected"):
            return None, "ineligible: rank#1 candidate is a restricted view"

    approx_row_count = top1.get("approx_row_count")
    if approx_row_count == 0:
        return None, "ineligible: approx_row_count=0 (source-data-unusable)"

    name_score = top1.get("name_score") or 0.0
    if name_score < _FAST_PATH_MIN_NAME_SCORE:
        return None, f"ineligible: name_score={name_score:.3f} below {_FAST_PATH_MIN_NAME_SCORE}"

    # Business-role gate — adapted from _apply_business_role_backstop's own
    # "an assignment/event selection is untrustworthy whenever a genuine
    # catalog/entity alternative exists" rule. The backstop itself compares
    # ranks because it evaluates whatever the AI actually SELECTED (which
    # could be any position in evidence); here we are always evaluating
    # `top1 == evidence[0]`, the deterministic BEST rank by construction --
    # no other candidate can ever have a "better" (lower) rank than it, so
    # a rank comparison would be vacuously always-false. The real Program
    # regression (D4b) shape this must catch is precisely "assignment/event
    # candidate ranked #1, catalog/entity candidate ranked #2+ elsewhere in
    # the SAME evidence" — so the correct condition is simply "does a
    # catalog_or_entity candidate exist anywhere else in evidence at all."
    role = top1.get("candidate_business_role")
    if role not in ("catalog_or_entity", "assignment_or_event"):
        return None, f"ineligible: candidate_business_role={role!r}"
    if role == "assignment_or_event":
        competing_catalog = [c for c in evidence[1:] if c.get("candidate_business_role") == "catalog_or_entity"]
        if competing_catalog:
            return None, "ineligible: assignment_or_event rank#1 with a catalog_or_entity alternative present"

    result = AdjudicationResult(
        selected_table_fqn=fqn,
        no_safe_selection=False,
        confidence=float(top1.get("score") or 0.0),
        evidence_references=(
            f"Role-aware fast path: rank#1 candidate for {entity_name!r} in role {role_context!r} "
            f"(business_role={role!r}, name_score={name_score:.3f}, no restriction/derivative/"
            "zero-row/better-catalog-alternative evidence) — no AI adjudication call made.",
        ),
    )
    return result, f"eligible: role-aware dominance ({role_context}, business_role={role})"


# Set to True only after this milestone's shadow-mode run proved zero
# false-safe selections across the real 37-question suite (see the final
# report). A module-level switch, not a config flag -- this is a controlled,
# evidence-gated rollout within this one milestone, not a user-facing
# toggle.
_FAST_PATH_ACTIVE = False

# Mirrors data.query_planning_service._AUTO_SELECT_MIN_CONFIDENCE exactly
# (not a new number) -- the same bar this codebase already trusts to accept
# a deterministic candidate without further judgment.
_PERSISTED_EVIDENCE_MIN_CONFIDENCE = 0.5


def _evaluate_persisted_evidence_candidate(
    source_id: int, entity_name: str, evidence: list[dict], requested_attributes: list[str] | None,
    *, session=None,
) -> tuple[AdjudicationResult | None, str]:
    """Milestone 6 (Executive Report Performance Recovery) — pure,
    deterministic, no-AI-call evaluation of whether an ALREADY-PERSISTED,
    source-scoped entity_assignments row can stand in for a fresh AI
    adjudication of `entity_name`.

    Root cause this targets: core.orchestrator.ai_physical_planning.py's
    ROLE_ENTITY/ROLE_RELATIONSHIP_ENTITY call sites call gather_candidate_
    evidence + adjudicate_candidates directly for EVERY question that names
    a business entity, never consulting entity_assignments/semantic_entity_
    contracts at all -- live-traced against the reference database's Executive Report:
    entity-level contract reuse (data.semantic_contract_service.
    get_or_build_entity_contract, the Milestone 5 fix) is fast (3 calls,
    0.02s total), while CANDIDATE_ADJUDICATION cost 97.74s across 19 calls
    in the same run -- confirming this planning-time entity resolution,
    not entity *discovery*, is the dominant, previously-uninstrumented cost.

    Every condition below reuses an EXISTING trusted primitive from
    _evaluate_fast_path_candidate (role, derivative_flag, restricted-view,
    zero-row, name_score) applied to whichever evidence candidate the
    persisted assignment names, plus three NEW persisted-evidence-specific
    gates: (1) the assignment's own confidence must clear the same trusted
    bar the rest of this codebase already uses, (2) the assignment must be
    current as of the LATEST profiling snapshot (a schema/relationship
    change invalidates an older assignment automatically -- this is the
    "matching semantic contract revision" freshness check, expressed via
    entity_assignments.profiling_snapshot_id since that table has no
    separate revision column), and (3) no OTHER candidate in the SAME
    evidence bundle may also carry a catalog_or_entity role (a genuinely
    competing entity always defers to AI, never resolved here).

    Fails closed on every path: requires session (for source_id/an open
    connection), requires requested_attributes to be empty (Part 8: never
    skip AI when a specific attribute still needs judgment), requires the
    persisted entity to match entity_name case-insensitively (entity_
    assignments stores canonical Titlecase names like "Program" while
    callers here pass whatever casing the question/term used, e.g.
    "program" -- confirmed live against the reference database: every real call passes a
    lowercase term), requires the persisted confidence and snapshot
    currency checks above, and requires EXACTLY ONE candidate in the WHOLE
    evidence bundle to survive this combined filter.

    Deliberately does NOT require evidence to contain only one
    catalog_or_entity-role candidate overall -- live-traced against real
    the reference database evidence for "program": a real gather_candidate_evidence call
    routinely returns several weak/irrelevant catalog_or_entity candidates
    alongside the correct one (e.g. a case-study-references table scoring
    name_score=0.4 purely from generic vocabulary overlap), which would
    make an "exactly one catalog_or_entity candidate in evidence" gate
    empirically inert in practice (mirroring _evaluate_fast_path_candidate's
    own documented zero real-call eligibility). Grounding ambiguity
    resolution in PERSISTED classification instead -- does this specific
    candidate ALSO carry its own persisted entity_assignments row naming
    THIS entity_name -- is both safer (a second, independently-computed,
    already-validated source of truth must agree, not just a role
    heuristic) and practically activates on real evidence: an unrelated
    catalog_or_entity candidate is filtered out here because IT has no
    persisted assignment to this entity_name, not because it was merely
    outscored.
    """
    if session is None:
        return None, "ineligible: no session (no source_id/connection available)"
    if requested_attributes:
        return None, "ineligible: requested_attributes present, defers to AI"
    if not evidence:
        return None, "ineligible: no candidates"

    catalog_candidates = [c for c in evidence if c.get("candidate_business_role") == "catalog_or_entity"]
    if not catalog_candidates:
        return None, "ineligible: 0 catalog_or_entity candidates"

    from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

    conn = session.conn
    latest_snapshot = get_latest_profiling_snapshot(source_id, conn=conn)
    if latest_snapshot is None:
        return None, "ineligible: no profiling snapshot for this source"

    normalized_entity_name = (entity_name or "").strip().lower()
    matches: list[tuple[dict, float]] = []
    skip_reasons: list[str] = []
    for cand in catalog_candidates:
        fqn = cand.get("table_fqn")
        if not fqn:
            skip_reasons.append("candidate evidence missing table_fqn")
            continue
        if cand.get("derivative_flag"):
            skip_reasons.append(f"{fqn}: derivative_flag={cand.get('derivative_flag')!r}")
            continue
        object_type = cand.get("object_type")
        restriction = cand.get("restricted_view_evidence")
        if object_type == "VIEW":
            if restriction is None:
                skip_reasons.append(f"{fqn}: VIEW candidate with unknown restriction status")
                continue
            if restriction.get("restriction_detected"):
                skip_reasons.append(f"{fqn}: VIEW candidate is a restricted view")
                continue
        if cand.get("approx_row_count") == 0:
            skip_reasons.append(f"{fqn}: approx_row_count=0 (source-data-unusable)")
            continue
        # Deliberately NO name_score gate here (unlike _evaluate_fast_path_
        # candidate above) -- live-traced against a real production database: the whole
        # reason a table needs persisted-evidence reuse in the first place
        # (see Milestone 5's Program/EDU_Track finding) is that its LEXICAL
        # table-name match can be zero while its true business meaning was
        # established through column-name/relationship evidence instead.
        # Requiring name_score here would reject exactly the cases this
        # gate exists to serve. The trust signal here is the persisted
        # entity_assignments confidence checked below, not lexical
        # similarity to entity_name.

        row = conn.execute(
            "SELECT entity, confidence, profiling_snapshot_id FROM entity_assignments "
            "WHERE source_id = ? AND table_fqn = ?",
            (source_id, fqn),
        ).fetchone()
        if row is None:
            skip_reasons.append(f"{fqn}: no persisted entity_assignments row")
            continue
        if (row["entity"] or "").strip().lower() != normalized_entity_name:
            skip_reasons.append(f"{fqn}: persisted entity={row['entity']!r} does not match {entity_name!r}")
            continue
        persisted_confidence = row["confidence"] or 0.0
        if persisted_confidence < _PERSISTED_EVIDENCE_MIN_CONFIDENCE:
            skip_reasons.append(f"{fqn}: persisted confidence={persisted_confidence} below {_PERSISTED_EVIDENCE_MIN_CONFIDENCE}")
            continue
        if row["profiling_snapshot_id"] != latest_snapshot.id:
            skip_reasons.append(f"{fqn}: persisted assignment predates the latest profiling snapshot (possibly stale)")
            continue
        matches.append((cand, persisted_confidence))

    if len(matches) != 1:
        detail = "; ".join(skip_reasons) if skip_reasons else "no catalog_or_entity candidate carried a table_fqn"
        return None, (
            f"ineligible: {len(matches)} candidates with matching persisted entity_assignments "
            f"(need exactly 1) — {detail}"
        )

    cand, persisted_confidence = matches[0]
    fqn = cand["table_fqn"]
    result = AdjudicationResult(
        selected_table_fqn=fqn,
        no_safe_selection=False,
        confidence=float(persisted_confidence),
        evidence_references=(
            f"Persisted-evidence fast path: entity_assignments already classifies {fqn} as "
            f"{entity_name!r} (confidence={persisted_confidence}, current as of the latest "
            "profiling snapshot) — no AI adjudication call made.",
        ),
    )
    return result, "eligible: persisted entity_assignments evidence"


# Milestone 6 (Executive Report Performance Recovery) — kept OFF, matching
# _FAST_PATH_ACTIVE's own established "built, unit-tested, but not yet
# activated" convention in this file. The gate's OWN logic is verified safe
# against controlled fixtures (see tests/test_persisted_evidence_fast_path.py
# — stale-snapshot, low-confidence, competing-candidate, and requested-
# attribute cases all correctly fall through to AI) and DID correctly speed
# up Program/Student entity resolution in a live the reference database Executive Report run
# (ai_candidate_adjudication total dropped from ~98s to ~69s across that
# run). But a SEPARATE live run surfaced a real correctness risk this gate
# cannot see from its own inputs alone: entity_assignments for this specific
# source has 85+ tables independently classified entity='Student' by the
# pre-existing rule-based classifier (core.entities.rules.detect_table_
# entity), several at HIGH confidence (0.875-1.0) purely from generic
# table-name-token matching (e.g. dbo.EDU_Assessment_Student, a narrow
# DISC-assessment sub-table, at confidence=0.875) — when the evidence
# bundle for a "how many students" question happened to surface exactly
# ONE of those as its sole catalog_or_entity candidate (candidate search is
# itself non-deterministic across runs), this gate confidently selected it
# over the correct dbo.EDU_Student, producing a WRONG COUNT with no AI
# judgment in the loop to catch it. The Program case did not hit this
# because its persisted assignment came from the qualitatively stronger
# Milestone 5 relationship-rescue path (data.entity_service.promote_
# unresolved_entities_via_semantic_contracts), not the noisy baseline
# classifier -- but entity_assignments has no field distinguishing WHICH
# mechanism produced a given row, so this gate cannot tell the two apart
# today. Shipping this active would violate this milestone's own hard
# acceptance bar (Student regression = 0, wrong answer = 0), so it stays
# off pending either (a) a provenance signal in entity_assignments that
# lets this gate trust only relationship-rescue-validated rows, or (b) a
# cleanup pass on the underlying rule-based classifier's over-eager
# name-token matching. See the Milestone 6 final report for the full trace.
_PERSISTED_EVIDENCE_REUSE_ACTIVE = False

# Shadow-mode data collection (Part 7/14) — appended to only when a caller
# explicitly opts in via enable_shadow_log(), never during normal
# production operation. None means "not collecting" (zero overhead, zero
# behavior change) — the default.
_SHADOW_LOG: list[dict] | None = None


def enable_shadow_log() -> None:
    global _SHADOW_LOG
    _SHADOW_LOG = []


def get_shadow_log() -> list[dict]:
    return list(_SHADOW_LOG or [])


def disable_shadow_log() -> None:
    global _SHADOW_LOG
    _SHADOW_LOG = None


# ---------------------------------------------------------------------------
# PERFORMANCE PHASE D3 — role context. A closed vocabulary describing WHY a
# candidate is being resolved (never what table eventually wins), derived
# from actual call sites in core.orchestrator.ai_physical_planning (the
# only module that ever passes simple_selection_only=True) -- not invented:
#
#   ENTITY              attempt_ai_physical_plan's own primary entity
#                        resolution, and attempt_structured_kpi_plan's
#                        anchor-table resolution (same purpose: picking the
#                        one table a whole plan is built around).
#   MEASURE              _resolve_ai_measure_cross_table -- resolving which
#                        table owns a named business QUANTITY/metric.
#   DIMENSION            _resolve_non_calendar_dimension -- resolving a
#                        GROUP BY dimension term's owning table.
#   RELATIONSHIP_ENTITY   _attempt_relationship_list_plan's per-entity loop
#                        -- resolving ONE of two-or-more named entities in a
#                        genuine multi-entity relationship question (a wrong
#                        choice here breaks a join, not just one selection).
#   CORROBORATION_RETRY   _corroborate_weak_entity_candidate's own two calls
#                        (the anchor lookup and the annotated-evidence
#                        retry) -- ONLY ever reached after the primary
#                        resolution already failed the trust bar; by
#                        construction never a fast-path candidate.
#
# data.semantic_contract_service._discover_entity_contract_via_adjudication
# (the one caller that never sets simple_selection_only) passes no role at
# all -- role_context stays None there, exactly matching its untouched,
# out-of-scope status.
# ---------------------------------------------------------------------------
ROLE_ENTITY = "ENTITY"
ROLE_MEASURE = "MEASURE"
ROLE_DIMENSION = "DIMENSION"
ROLE_RELATIONSHIP_ENTITY = "RELATIONSHIP_ENTITY"
ROLE_CORROBORATION_RETRY = "CORROBORATION_RETRY"
_KNOWN_ROLES = frozenset({
    ROLE_ENTITY, ROLE_MEASURE, ROLE_DIMENSION, ROLE_RELATIONSHIP_ENTITY, ROLE_CORROBORATION_RETRY,
})

# Roles empirically proven safe for the deterministic dominance gate below
# (Part 6/7/8/9) -- populated only after real shadow-mode evidence supports
# it; starts empty (no role is trusted by default). See the Phase D3 final
# report for exactly which roles (if any) were added and why.
_ROLE_SAFE_FOR_DOMINANCE: frozenset[str] = frozenset()


def adjudicate_candidates(
    entity_name: str,
    evidence: list[dict],
    requested_attributes: list[str] | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
    session=None,
    simple_selection_only: bool = False,
    role_context: str | None = None,
) -> AdjudicationResult | None:
    """Run the Day 2C AI candidate-adjudication step for one entity's
    bounded evidence set (data.semantic_contract_service.
    gather_candidate_evidence's own output).

    Returns None on ANY failure — missing config, missing package, API
    error, malformed JSON, or a schema/identifier violation — never raises.
    Callers (data.semantic_contract_service._discover_entity_contract_via_
    adjudication) always keep the deterministic NO_CANDIDATE result and
    must never block on this step.

    session, when given (data.request_metadata_session.RequestMetadataSession),
    memoizes the result for this one planning request, keyed on the exact
    (entity_name, requested_attributes, evidence) this call receives — a
    literal-input memoization, not a role-based one: core.orchestrator.
    ai_physical_planning.py can ask this same entity/measure/dimension TERM
    to be adjudicated several times in one question (entity selection, weak-
    match corroboration, cross-table measure/dimension resolution), and a
    real OpenAI round trip is the single most expensive step in planning.
    Reuse only ever fires for byte-identical inputs — a "program" asked as a
    grouping dimension gets its OWN evidence bundle from its own
    gather_candidate_evidence(..., "program", session=session) call, so a
    different role naturally produces a different cache key whenever the
    evidence actually differs, and an IDENTICAL key means the call would
    have been given the identical prompt anyway (this only removes a
    duplicate network round trip, it never substitutes one role's answer
    for another's). Never caches a None (failed/unavailable) adjudication —
    a transient failure must not be pinned for the rest of the request.

    PERFORMANCE PHASE D — simple_selection_only, passed ONLY by callers
    that never read any AdjudicationResult field besides .no_safe_
    selection/.selected_table_fqn/.confidence/.evidence_references
    (verified: every core.orchestrator.ai_physical_planning call site;
    never data.semantic_contract_service's own contract-persistence
    caller, which needs the full rich contract only an LLM can produce and
    never sets this flag). When True AND _evaluate_fast_path_candidate
    finds the evidence provably unambiguous AND _FAST_PATH_ACTIVE is True
    AND shadow-mode logging is not enabled, the AI call is skipped entirely
    and the deterministic result is returned directly. In shadow mode
    (enable_shadow_log() called), the AI is ALWAYS still invoked
    unconditionally and the fast-path proposal is only compared against
    it, never substituted -- see _SHADOW_LOG.

    PERFORMANCE PHASE D3 — role_context, one of the closed ROLE_* constants
    above (or None), passed by the SAME callers that set simple_selection_
    only=True. Observability-only unless the caller's role is in the
    (empirically-evidenced, empty-by-default) _ROLE_SAFE_FOR_DOMINANCE set
    AND _FAST_PATH_ACTIVE is True AND shadow mode is off -- see
    _evaluate_role_dominance_candidate. Included in the session cache key
    below so that if role ever DOES affect eligibility, the SAME evidence
    resolved under two different roles can never be conflated into one
    cache entry.
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

    from core.perf import stage_timer

    fast_result: AdjudicationResult | None = None
    fast_reason = "ineligible: simple_selection_only=False"
    role_result: AdjudicationResult | None = None
    role_reason = "ineligible: simple_selection_only=False"
    persisted_result: AdjudicationResult | None = None
    persisted_reason = "ineligible: simple_selection_only=False"
    if simple_selection_only:
        fast_result, fast_reason = _evaluate_fast_path_candidate(entity_name, evidence, requested_attributes)
        role_result, role_reason = _evaluate_role_dominance_candidate(
            entity_name, evidence, requested_attributes, role_context,
        )
        if session is not None:
            persisted_result, persisted_reason = _evaluate_persisted_evidence_candidate(
                session.source_id, entity_name, evidence, requested_attributes, session=session,
            )
        shadow_active = _SHADOW_LOG is not None
        stage_timer.record(
            "ai_candidate_adjudication_fast_path_eligible" if fast_result is not None
            else "ai_candidate_adjudication_fast_path_rejected", 0.0,
        )
        stage_timer.record(
            "ai_candidate_adjudication_role_dominance_eligible" if role_result is not None
            else "ai_candidate_adjudication_role_dominance_rejected", 0.0,
        )
        stage_timer.record(
            "ai_candidate_adjudication_persisted_evidence_eligible" if persisted_result is not None
            else "ai_candidate_adjudication_persisted_evidence_rejected", 0.0,
        )
        activated_result = fast_result or role_result
        persisted_activated = (
            persisted_result is not None and _PERSISTED_EVIDENCE_REUSE_ACTIVE and not shadow_active
        )
        if persisted_activated:
            stage_timer.record("ai_candidate_adjudication_persisted_evidence_used", 0.0)
            logger.info(
                "AI candidate adjudicator persisted-evidence fast path: selected %s for entity=%s "
                "without an AI call (%s).",
                persisted_result.selected_table_fqn, entity_name, persisted_reason,
            )
            return persisted_result
        if activated_result is not None and _FAST_PATH_ACTIVE and not shadow_active:
            # ACTIVATED: skip the AI call entirely for this provably-
            # unambiguous, narrow class. Still passes through every normal
            # downstream caller-side validation unchanged (this function
            # never bypasses anything outside itself).
            stage_timer.record("ai_candidate_adjudication_fast_path_used", 0.0)
            logger.info(
                "AI candidate adjudicator fast path: selected %s for entity=%s without an AI call (%s).",
                activated_result.selected_table_fqn, entity_name, fast_reason if fast_result else role_reason,
            )
            return activated_result

    def _call_openai() -> AdjudicationResult | None:
        try:
            with stage_timer.measure("ai_candidate_adjudication"):
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
            validated = _apply_business_role_backstop(_validate(raw, evidence), evidence)
            return _apply_restricted_view_backstop(validated, evidence)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AI candidate adjudicator failed for entity=%s (%s: %s); keeping deterministic result",
                entity_name, type(exc).__name__, exc,
            )
            return None

    _shadow_t0 = None
    if simple_selection_only and _SHADOW_LOG is not None:
        import time as _shadow_time
        _shadow_t0 = _shadow_time.monotonic()

    if session is None:
        real_result = _call_openai()
    else:
        try:
            evidence_fingerprint = json.dumps(evidence, sort_keys=True, default=str)
        except (TypeError, ValueError):
            evidence_fingerprint = repr(evidence)
        cache_key = (
            "ai_candidate_adjudication", entity_name, resolved_model,
            tuple(requested_attributes or ()), evidence_fingerprint, role_context,
        )
        before_hits = session.request_context_hits
        real_result = session.get_or_compute_unless_none(cache_key, _call_openai)
        if session.request_context_hits > before_hits:
            stage_timer.record("ai_candidate_adjudication_cache_hit", 0.0)

    if simple_selection_only and _SHADOW_LOG is not None:
        import time as _shadow_time

        latency_ms = (_shadow_time.monotonic() - _shadow_t0) * 1000
        by_fqn = {c.get("table_fqn"): c for c in evidence}
        top1_fqn = evidence[0].get("table_fqn") if evidence else None
        top1 = evidence[0] if evidence else {}
        second = evidence[1] if len(evidence) > 1 else None
        score_margin = (
            (top1.get("score") or 0.0) - (second.get("score") or 0.0)
            if second is not None else None
        )
        selected_fqn = real_result.selected_table_fqn if real_result else None
        selected_cand = by_fqn.get(selected_fqn) if selected_fqn else None
        _SHADOW_LOG.append({
            "entity_name": entity_name,
            "requested_attributes": list(requested_attributes) if requested_attributes else [],
            "candidate_count": len(evidence),
            # Compact per-candidate structural summary -- every field Part 1
            # of this milestone asks for except free-text description/
            # domain (never needed for the dominance analysis).
            "candidates": [
                {
                    "table_fqn": c.get("table_fqn"),
                    "deterministic_rank": c.get("deterministic_rank"),
                    "score": c.get("score"),
                    "name_score": c.get("name_score"),
                    "candidate_business_role": c.get("candidate_business_role"),
                    "derivative_flag": c.get("derivative_flag"),
                    "object_type": c.get("object_type"),
                    "restricted_view_evidence": c.get("restricted_view_evidence"),
                    "lineage_source_view": c.get("lineage_source_view"),
                    "approx_row_count": c.get("approx_row_count"),
                    "relationships": [r.get("table_fqn") for r in (c.get("relationships") or [])],
                    "descriptor_column_count": len(c.get("human_readable_descriptor_columns") or []),
                    "person_ref_column_count": len(c.get("person_reference_columns") or []),
                    "authority_reasons": c.get("authority_reasons"),
                }
                for c in evidence
            ],
            "top1_fqn": top1_fqn,
            "top1_score_margin_over_2nd": score_margin,
            "role_context": role_context,
            "fast_path_eligible": fast_result is not None,
            "fast_path_reason": fast_reason,
            "fast_path_selected_table_fqn": fast_result.selected_table_fqn if fast_result else None,
            "role_dominance_eligible": role_result is not None,
            "role_dominance_reason": role_reason,
            "role_dominance_selected_table_fqn": role_result.selected_table_fqn if role_result else None,
            "real_result_is_none": real_result is None,
            "real_no_safe_selection": real_result.no_safe_selection if real_result else None,
            "real_selected_table_fqn": selected_fqn,
            "real_selected_deterministic_rank": (selected_cand or {}).get("deterministic_rank"),
            "real_confidence": real_result.confidence if real_result else None,
            "real_excluded_candidates": (
                [ec.get("table_fqn") if isinstance(ec, dict) else ec for ec in real_result.excluded_candidates]
                if real_result else []
            ),
            "adjudication_latency_ms": round(latency_ms, 1),
        })

    return real_result
