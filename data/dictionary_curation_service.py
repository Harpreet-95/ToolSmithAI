"""
Autonomous Dictionary Curation — Milestone M-5, Part 4.

Composes only already-existing reads/writes into one thin eligibility +
action layer. No new engine, no new governance state machine, no new
approval mechanism:

  - governance_service.get_governance_profile()/evaluate_policies()  — hard
    safety policies (irreversible state, unconfirmed PII, high-risk domain)
    plus the new POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_DICTIONARY DB policy
    (data/models.py), reused verbatim.
  - review_segmentation_service.classify_asset()                    — the
    existing A-G review-group classifier (Milestone M-3); only group A
    ("candidate_authoritative_business_asset") is ever eligible, which
    already excludes temp/backup/historical/staging/log/sensitive/unknown
    tables by construction.
  - query_planning_service._score_table_authority()/_AMBIGUITY_MARGIN      —
    the same ranking/ambiguity machinery Milestone M-2/M-4 already use, so
    a table tied with another comparable candidate for the same business
    entity is never auto-approved here either.
  - dictionary_service.approve_table_dictionary()/approve_column_dictionary()
    — the one write path, extended (not duplicated) with an optional
    governance_state kwarg so this module can request AUTO_APPROVED instead
    of HUMAN_APPROVED.

Never auto-approves solely because a name matches, row count is high, an AI
suggestion exists, or a table is the only candidate — every one of those
signals only ever contributes to the *existing* authority/confidence scores
this module reads, never a standalone shortcut.
"""
from __future__ import annotations

import logging

from data.db import get_connection
from data.business_knowledge_service import get_table_business_context
from data.dictionary_service import approve_table_dictionary, approve_column_dictionary
from data.governance_service import get_governance_profile
from data.query_planning_service import _score_table_authority, _AMBIGUITY_MARGIN
from data.review_segmentation_service import classify_asset, _naming_hits, _table_has_pii

logger = logging.getLogger(__name__)

_ELIGIBLE_REVIEW_GROUP = "A"


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _check_no_ambiguous_sibling(source_id: int, user_id: str, table_fqn: str, ctx: dict) -> tuple[bool, str | None]:
    """
    "No material ambiguity with another authoritative candidate": compares
    this table's authority bonus (query_planning_service._score_table_authority
    — reused unmodified) against every OTHER table sharing the same entity
    assignment. If the margin over the closest sibling doesn't clear
    _AMBIGUITY_MARGIN (the same constant Milestone M-2/M-4 use), curation
    refuses rather than guessing which table is authoritative for that
    business concept.
    """
    entity = (ctx.get("entity") or {}).get("entity")
    if not entity or entity == "Unknown":
        return True, None  # nothing to compare against

    conn = get_connection()
    try:
        sibling_fqns = [
            r["table_fqn"] for r in conn.execute(
                "SELECT table_fqn FROM entity_assignments "
                "WHERE source_id = ? AND entity = ? AND table_fqn != ?",
                (source_id, entity, table_fqn),
            ).fetchall()
        ]
    finally:
        conn.close()

    if not sibling_fqns:
        return True, None

    own_bonus = _score_table_authority(table_fqn, ctx)["bonus"]
    best_sibling_bonus = None
    for sibling_fqn in sibling_fqns:
        sibling_ctx = get_table_business_context(source_id, user_id, sibling_fqn)
        if sibling_ctx is None:
            continue
        sibling_bonus = _score_table_authority(sibling_fqn, sibling_ctx)["bonus"]
        if best_sibling_bonus is None or sibling_bonus > best_sibling_bonus:
            best_sibling_bonus = sibling_bonus

    if best_sibling_bonus is None:
        return True, None

    if (own_bonus - best_sibling_bonus) < _AMBIGUITY_MARGIN:
        return False, (
            f"Table shares entity '{entity}' with {len(sibling_fqns)} other table(s) "
            f"whose authority score is within the {_AMBIGUITY_MARGIN} ambiguity margin "
            "— not auto-approved."
        )
    return True, None


def evaluate_curation_eligibility(
    source_id: int, user_id: str, table_fqn: str, column_name: str | None = None,
) -> dict:
    """
    Read-only eligibility check for one dict.table or dict.column object.
    Returns {"eligible": bool, "reasons": [...], "blocking_reasons": [...]}.
    Never invents an eligibility signal not already computed elsewhere.
    """
    reasons: list[str] = []
    blocking: list[str] = []

    ctx = get_table_business_context(source_id, user_id, table_fqn)
    if ctx is None:
        return {"eligible": False, "reasons": [], "blocking_reasons": ["Table not found or not owned by user."]}

    object_type = "dict.column" if column_name else "dict.table"
    profile = get_governance_profile(
        object_type=object_type, source_id=source_id, table_fqn=table_fqn, column_name=column_name,
    )
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

    if column_name is None:
        no_ambiguity, ambiguity_reason = _check_no_ambiguous_sibling(source_id, user_id, table_fqn, ctx)
        if no_ambiguity:
            reasons.append("No ambiguous authoritative sibling for this table's entity.")
        else:
            blocking.append(ambiguity_reason or "Ambiguous authoritative candidate.")

    return {"eligible": not blocking, "reasons": reasons, "blocking_reasons": blocking}


def run_dictionary_curation(
    source_id: int, user_id: str, *, dry_run: bool = True,
    actor_id: str = "system:m5-autonomous-curation",
) -> dict:
    """
    Evaluate every not-yet-approved dict.table/dict.column row for source_id
    and, when dry_run is False, auto-approve the eligible ones (writing
    GovernanceState.AUTO_APPROVED via the existing, extended
    approve_table_dictionary/approve_column_dictionary). dry_run defaults to
    True — no writes happen unless the caller explicitly opts in.

    Returns {"source_id", "dry_run", "auto_approved": [...], "blocked": [...],
             "skipped": [...], "queued_for_review": [...]} — each entry is
    {"table_fqn", "column_name": str|None, "reasons"/"blocking_reasons"}.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return {"source_id": source_id, "dry_run": dry_run, "error": "source not found or not owned by user"}

        table_rows = conn.execute(
            "SELECT table_fqn FROM data_dictionary_tables WHERE source_id = ? AND is_approved = 0 "
            "ORDER BY table_fqn",
            (source_id,),
        ).fetchall()
        column_rows = conn.execute(
            "SELECT table_fqn, column_name FROM data_dictionary_columns "
            "WHERE source_id = ? AND is_approved = 0 ORDER BY table_fqn, column_name",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    result = {
        "source_id": source_id, "dry_run": dry_run,
        "auto_approved": [], "blocked": [], "skipped": [], "queued_for_review": [],
    }

    for row in table_rows:
        table_fqn = row["table_fqn"]
        decision = evaluate_curation_eligibility(source_id, user_id, table_fqn)
        entry = {"table_fqn": table_fqn, "column_name": None}
        if decision["eligible"]:
            entry["reasons"] = decision["reasons"]
            if not dry_run:
                approve_table_dictionary(
                    source_id, user_id, table_fqn,
                    governance_state="AUTO_APPROVED", actor_id=actor_id,
                )
            result["auto_approved"].append(entry)
        else:
            entry["blocking_reasons"] = decision["blocking_reasons"]
            if any("Review group" in r for r in decision["blocking_reasons"]):
                result["skipped"].append(entry)
            else:
                result["queued_for_review"].append(entry)
            result["blocked"].append(entry)

    for row in column_rows:
        table_fqn, column_name = row["table_fqn"], row["column_name"]
        decision = evaluate_curation_eligibility(source_id, user_id, table_fqn, column_name)
        entry = {"table_fqn": table_fqn, "column_name": column_name}
        if decision["eligible"]:
            entry["reasons"] = decision["reasons"]
            if not dry_run:
                approve_column_dictionary(
                    source_id, user_id, table_fqn, column_name,
                    governance_state="AUTO_APPROVED", actor_id=actor_id,
                )
            result["auto_approved"].append(entry)
        else:
            entry["blocking_reasons"] = decision["blocking_reasons"]
            if any("Review group" in r for r in decision["blocking_reasons"]):
                result["skipped"].append(entry)
            else:
                result["queued_for_review"].append(entry)
            result["blocked"].append(entry)

    return result
