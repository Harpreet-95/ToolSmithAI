"""
Review Segmentation Service — CCPP Semantic Governance Activation (Milestone M-3, Part 3).

Buckets every dictionary table for a source into one of seven governed review
groups (A-G) purely by reading existing metadata signals already produced by
prior phases: profiling table class, dictionary approval state, domain/entity
assignment, PII flags, relationship participation, and the same naming-
convention tokens Enterprise Authoritative Source Ranking (M-2) already uses
in data.query_planning_service. No new schema, no persisted classification —
a table's group is recomputed on every call, so any approve/reject decision
made through the existing governance flows is reflected immediately. This is
how "human override" is satisfied without inventing a locked-classification
column: an approved dictionary table with an assigned domain/entity always
lands in group A regardless of naming, and PII always wins regardless of
everything else.
"""
from data.db import get_connection
from data.business_knowledge_service import get_table_business_context
from data.query_planning_service import (
    _DATED_COPY_RE, _NEGATIVE_NAME_SUBSTRINGS, _NEGATIVE_NAME_TOKENS,
)
from core.dictionary.rule_classifier import _tokenize

REVIEW_GROUPS: dict[str, str] = {
    "A": "candidate_authoritative_business_asset",
    "B": "derived_reporting_asset",
    "C": "historical_archive_asset",
    "D": "temporary_staging_import_asset",
    "E": "framework_system_log_asset",
    "F": "sensitive_restricted_asset",
    "G": "unknown_manual_review_asset",
}

# Partition of the existing _NEGATIVE_NAME_TOKENS (data/query_planning_service.py)
# into the three naming-driven review groups. Every token in that 13-value set
# lands in exactly one bucket below; nothing is duplicated or invented.
_STAGING_NAME_TOKENS = {"temp", "tmp", "staging", "import", "snapshot"}
_HISTORICAL_NAME_TOKENS = {"backup", "old", "archive", "history", "copy"}
_FRAMEWORK_NAME_TOKENS = {"log", "msgs", "generated"}

_AUTHORITATIVE_TABLE_CLASSES = {"Master", "Transactional", "Reference"}


def _naming_hits(table_fqn: str) -> set[str]:
    """Same hit-detection as _score_table_authority (M-2) — token-exact match
    plus vendor-substring plus dated-copy regex — reusing the actual constants,
    not a re-derived copy of the token list."""
    table_name = table_fqn.split(".")[-1]
    name_lower = table_name.lower()
    hits = set(_tokenize(table_name)) & _NEGATIVE_NAME_TOKENS
    hits |= {kw for kw in _NEGATIVE_NAME_SUBSTRINGS if kw in name_lower}
    if _DATED_COPY_RE.search(table_name):
        hits.add("dated_copy")
    return hits


def classify_asset(
    *,
    table_class: str | None,
    table_type: str | None,
    is_approved: bool,
    domain: str | None,
    entity: str | None,
    has_pii: bool,
    relationship_count: int,
    naming_hits: set[str],
) -> str:
    """
    Pure, deterministic precedence (highest safety concern first):
      F (sensitive)   > D (temp/staging) > E (framework/system/log)
      > C (historical/archive) > B (derived/reporting)
      > A (candidate authoritative) > G (unknown/manual review, fallback).

    An already-approved dictionary table with a real domain/entity assignment
    is never pulled out of group A by naming alone (human override) — this
    check runs before any naming/structural signal, so approval always wins.
    """
    if has_pii:
        return "F"

    if is_approved and domain not in (None, "Unknown") and entity not in (None, "Unknown"):
        return "A"

    if table_class == "Staging" or (naming_hits & _STAGING_NAME_TOKENS):
        return "D"

    if table_class == "Audit" or (naming_hits & _FRAMEWORK_NAME_TOKENS) \
            or (naming_hits & set(_NEGATIVE_NAME_SUBSTRINGS)):
        return "E"

    if naming_hits & _HISTORICAL_NAME_TOKENS or "dated_copy" in naming_hits:
        return "C"

    if table_class == "Reporting" or table_type == "VIEW":
        return "B"

    if table_class in _AUTHORITATIVE_TABLE_CLASSES \
            and domain not in (None, "Unknown") and entity not in (None, "Unknown"):
        return "A"

    return "G"


def _table_has_pii(ctx: dict) -> bool:
    """Same signal query_planning_service._resolve_term() already uses per
    column: dictionary.pii_risk OR profiling.pii_name_heuristic, on any column
    of the table — unconfirmed PII is still PII for review-segmentation
    purposes (confirmation only affects the *approval* path, not visibility)."""
    for col in ctx.get("columns") or []:
        dic = col.get("dictionary")
        prof = col.get("profiling")
        if (dic and dic.get("pii_risk")) or (prof and prof.get("pii_name_heuristic")):
            return True
    return False


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def segment_source_assets(
    source_id: int,
    user_id: str,
    *,
    tables_per_group_limit: int = 50,
) -> dict | None:
    """
    Classify every dictionary table for source_id into review groups A-G.

    Read-only: no writes, no new schema. Returns None if the source does not
    exist or is not owned by user_id. Reuses get_table_business_context() —
    the same composed read Enterprise Authoritative Source Ranking (M-2)
    already relies on — so no new metadata query shape is introduced.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None
        table_fqns = [
            r["table_fqn"] for r in conn.execute(
                "SELECT table_fqn FROM data_dictionary_tables WHERE source_id = ? ORDER BY table_fqn",
                (source_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    groups: dict[str, dict] = {
        code: {"label": label, "count": 0, "example_tables": []}
        for code, label in REVIEW_GROUPS.items()
    }

    for table_fqn in table_fqns:
        ctx = get_table_business_context(source_id, user_id, table_fqn)
        if ctx is None:
            continue

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
            relationship_count=len(relationships.get("outbound") or [])
            + len(relationships.get("inbound") or []),
            naming_hits=_naming_hits(table_fqn),
        )

        bucket = groups[group]
        bucket["count"] += 1
        if len(bucket["example_tables"]) < tables_per_group_limit:
            bucket["example_tables"].append(table_fqn)

    return {
        "source_id": source_id,
        "total_tables": len(table_fqns),
        "groups": groups,
    }
