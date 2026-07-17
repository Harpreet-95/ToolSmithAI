"""
Enterprise Semantic Governance Rollout — Milestone M-23 (Phase 6.5).

Composes only already-existing reads/writes into one thin maturity-
classification + orchestration layer. No new engine, no new governance state
machine, no new approval mechanism, and no change to the ranking algorithm:

  - governance_service.get_governance_profile()/get_governance_explanation()  —
    unified profile + human-readable decision narrative for any governed
    object type, including the domain.assignment/entity.assignment dispatch
    this milestone adds (governance_service.py).
  - governance_service.is_hard_safety_policy()                               —
    tells a hard block (PII/high-risk-domain/irreversible/relationship-no-
    bulk-approve — must always stay manual) apart from a soft, DB-policy
    block (eligible for review, not permanently blocked).
  - review_segmentation_service.classify_asset()/_naming_hits()/_table_has_pii()
    — the existing A-G review-group classifier (Milestone M-3); only group A
    is ever eligible for auto-maturation, unchanged.
  - dictionary_curation_service.run_dictionary_curation()                    —
    reused verbatim for the dictionary half of the rollout; not reimplemented.
  - dictionary_curation_service._check_no_ambiguous_sibling()                —
    the same ambiguity-margin refusal M-2/M-4/M-5 already use, reused
    verbatim for domain/entity assignment maturation too.
  - domain_service.auto_mature_domain_assignment() /
    entity_service.auto_mature_entity_assignment()                          —
    the one new write path (Milestone M-23), itself a thin extension of the
    existing lock_domain_assignment/lock_entity_assignment pattern.

Never auto-matures an asset solely because a name matches, row count is high,
or an AI suggestion exists — every signal here only ever contributes to the
*existing* policy/authority/confidence scores this module reads.
"""
from __future__ import annotations

import logging

from data.db import get_connection
from data.business_knowledge_service import get_table_business_context
from data.dictionary_curation_service import (
    _ELIGIBLE_REVIEW_GROUP, _check_no_ambiguous_sibling, run_dictionary_curation,
)
from data.domain_service import auto_mature_domain_assignment
from data.entity_service import auto_mature_entity_assignment
from data.governance_service import (
    GovernedObjectType, get_governance_explanation, get_governance_profile,
    is_hard_safety_policy,
)
from data.review_segmentation_service import _naming_hits, _table_has_pii, classify_asset

logger = logging.getLogger(__name__)

_ASSIGNMENT_TYPES = (GovernedObjectType.DOMAIN_ASSIGNMENT, GovernedObjectType.ENTITY_ASSIGNMENT)

_AUTO_MATURE_FN = {
    GovernedObjectType.DOMAIN_ASSIGNMENT: auto_mature_domain_assignment,
    GovernedObjectType.ENTITY_ASSIGNMENT: auto_mature_entity_assignment,
}

# The four maturity tiers this milestone's brief asks every business asset to
# be classified into.
MATURITY_TRUSTED = "Trusted"
MATURITY_REVIEW_REQUIRED = "Review Required"
MATURITY_BLOCKED = "Blocked"
MATURITY_UNKNOWN = "Unknown"


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Maturity classification
# ---------------------------------------------------------------------------

def _maturity_from_explanation(explanation) -> tuple[str, str]:
    """
    Map governance_service's existing decision_type vocabulary (HUMAN_APPROVED
    | AUTO_APPROVED | REJECTED | DEPRECATED | ARCHIVED | ESCALATED |
    PENDING_AUTO_APPROVE | BLOCKED | GENERATED | PENDING_REVIEW) onto this
    milestone's Trusted/Review Required/Blocked/Unknown vocabulary. Pure
    function — no new signal, only a relabeling of what get_governance_explanation()
    already computed.
    """
    dt = explanation.decision_type

    if dt in ("HUMAN_APPROVED", "AUTO_APPROVED"):
        return MATURITY_TRUSTED, explanation.decision

    if dt == "GENERATED":
        return MATURITY_UNKNOWN, explanation.decision

    if any(is_hard_safety_policy(p) for p in explanation.blocking_policies):
        return MATURITY_BLOCKED, explanation.decision

    # REJECTED/DEPRECATED/ARCHIVED always carry blocking_policy=HARD_IRREVERSIBLE_STATE
    # from _check_hard_safety_policies, so the branch above already catches
    # them in practice; this is a defensive fallback, not the primary path.
    if dt in ("REJECTED", "DEPRECATED", "ARCHIVED"):
        return MATURITY_BLOCKED, explanation.decision

    # ESCALATED, PENDING_AUTO_APPROVE, BLOCKED (soft/DB policy), PENDING_REVIEW
    return MATURITY_REVIEW_REQUIRED, explanation.decision


def classify_asset_maturity(
    source_id: int, user_id: str, *, object_type: str, table_fqn: str,
    column_name: str | None = None,
) -> dict:
    """
    Classify one business asset as Trusted / Review Required / Blocked /
    Unknown, with the exact reason. Read-only.

    Returns {"status": str, "reason": str, "explanation": dict | None}.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return {
                "status": MATURITY_UNKNOWN,
                "reason": "Source not found or not owned by user.",
                "explanation": None,
            }
    finally:
        conn.close()

    explanation = get_governance_explanation(
        object_type=object_type, source_id=source_id, table_fqn=table_fqn,
        column_name=column_name,
    )
    if explanation is None:
        return {
            "status": MATURITY_UNKNOWN,
            "reason": "No governance profile exists for this object.",
            "explanation": None,
        }

    status, reason = _maturity_from_explanation(explanation)

    # Surface the specific ambiguous-sibling reason (one of the brief's named
    # "must stay manual" categories) instead of the generic "awaiting human
    # review" narrative, for table-level domain/entity assignments only.
    if status == MATURITY_REVIEW_REQUIRED and column_name is None and object_type in _ASSIGNMENT_TYPES:
        ctx = get_table_business_context(source_id, user_id, table_fqn)
        if ctx is not None:
            no_ambiguity, ambiguity_reason = _check_no_ambiguous_sibling(
                source_id, user_id, table_fqn, ctx,
            )
            if not no_ambiguity and ambiguity_reason:
                reason = ambiguity_reason

    return {"status": status, "reason": reason, "explanation": explanation.to_dict()}


# ---------------------------------------------------------------------------
# Governed automation — domain/entity assignment maturation
# ---------------------------------------------------------------------------

def _evaluate_assignment_eligibility(
    source_id: int, user_id: str, table_fqn: str, object_type: str,
) -> dict:
    """
    Read-only eligibility check for one domain.assignment/entity.assignment.

    Mirrors dictionary_curation_service.evaluate_curation_eligibility()'s
    three gates exactly (policy eligibility, review group A, no ambiguous
    sibling) — the first and third are reused verbatim via cross-module
    imports; the review-group check is re-derived from the same
    get_table_business_context() read since it does not depend on object type.
    """
    reasons: list[str] = []
    blocking: list[str] = []

    ctx = get_table_business_context(source_id, user_id, table_fqn)
    if ctx is None:
        return {"eligible": False, "reasons": [], "blocking_reasons": ["Table not found or not owned by user."]}

    profile = get_governance_profile(object_type=object_type, source_id=source_id, table_fqn=table_fqn)
    if profile is None:
        return {"eligible": False, "reasons": [], "blocking_reasons": ["No governance profile found."]}

    if profile.auto_approval_eligible:
        reasons.append(f"Governance policy '{profile.matched_policy}' permits auto-approval.")
    else:
        blocking.append(
            profile.review_reason or profile.blocking_policy or "Governance policy does not permit auto-approval."
        )

    profiling = ctx.get("profiling") or {}
    dictionary = ctx.get("dictionary") or {}
    domain_row = ctx.get("domain") or {}
    entity_row = ctx.get("entity") or {}
    relationships = ctx.get("relationships") or {}
    table_type = (ctx.get("table") or {}).get("table_type")

    group = classify_asset(
        table_class=profiling.get("table_class"),
        table_type=table_type,
        is_approved=bool(dictionary.get("is_approved")),
        domain=domain_row.get("domain"),
        entity=entity_row.get("entity"),
        has_pii=_table_has_pii(ctx),
        relationship_count=len(relationships.get("outbound") or []) + len(relationships.get("inbound") or []),
        naming_hits=_naming_hits(table_fqn),
    )
    if group == _ELIGIBLE_REVIEW_GROUP:
        reasons.append(f"Review group {group} ('candidate_authoritative_business_asset').")
    else:
        blocking.append(f"Review group {group}, not eligible group {_ELIGIBLE_REVIEW_GROUP}.")

    no_ambiguity, ambiguity_reason = _check_no_ambiguous_sibling(source_id, user_id, table_fqn, ctx)
    if no_ambiguity:
        reasons.append("No ambiguous authoritative sibling for this table's entity.")
    else:
        blocking.append(ambiguity_reason or "Ambiguous authoritative candidate.")

    return {"eligible": not blocking, "reasons": reasons, "blocking_reasons": blocking}


def run_semantic_governance_rollout(
    source_id: int, user_id: str, *, dry_run: bool = True,
    actor_id: str = "system:m23-governance-rollout",
) -> dict:
    """
    Evaluate every not-yet-matured dictionary/domain/entity object for
    source_id and, when dry_run is False, auto-mature the eligible ones.
    dry_run defaults to True — no writes happen unless the caller explicitly
    opts in.

    Not wired into core/lifecycle/runner.py's scan-triggered execution — same
    deliberate scoping decision Milestone M-5 already made for
    run_dictionary_curation: auto-writing governance state on every schema
    scan is a bigger production-behavior change than this milestone's brief
    asks for. Stays a directly-callable service function.

    Returns {"source_id", "dry_run", "dictionary": <run_dictionary_curation output>,
             "assignments": {"auto_approved", "blocked", "skipped", "queued_for_review"}}.
    """
    dictionary_result = run_dictionary_curation(source_id, user_id, dry_run=dry_run, actor_id=actor_id)

    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return {"source_id": source_id, "dry_run": dry_run, "error": "source not found or not owned by user"}

        domain_rows = conn.execute(
            "SELECT table_fqn FROM domain_assignments WHERE source_id = ? "
            "AND assignment_source NOT IN ('human', 'auto_governance') ORDER BY table_fqn",
            (source_id,),
        ).fetchall()
        entity_rows = conn.execute(
            "SELECT table_fqn FROM entity_assignments WHERE source_id = ? "
            "AND assignment_source NOT IN ('human', 'auto_governance') ORDER BY table_fqn",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    assignment_result: dict = {
        "auto_approved": [], "blocked": [], "skipped": [], "queued_for_review": [],
    }

    for object_type, rows in (
        (GovernedObjectType.DOMAIN_ASSIGNMENT, domain_rows),
        (GovernedObjectType.ENTITY_ASSIGNMENT, entity_rows),
    ):
        auto_mature = _AUTO_MATURE_FN[object_type]
        for row in rows:
            table_fqn = row["table_fqn"]
            decision = _evaluate_assignment_eligibility(source_id, user_id, table_fqn, object_type)
            entry = {"table_fqn": table_fqn, "column_name": None, "object_type": object_type}
            if decision["eligible"]:
                entry["reasons"] = decision["reasons"]
                if not dry_run:
                    auto_mature(source_id, table_fqn, actor_id=actor_id)
                assignment_result["auto_approved"].append(entry)
            else:
                entry["blocking_reasons"] = decision["blocking_reasons"]
                if any("Review group" in r for r in decision["blocking_reasons"]):
                    assignment_result["skipped"].append(entry)
                else:
                    assignment_result["queued_for_review"].append(entry)
                assignment_result["blocked"].append(entry)

    return {
        "source_id": source_id,
        "dry_run": dry_run,
        "dictionary": dictionary_result,
        "assignments": assignment_result,
    }
