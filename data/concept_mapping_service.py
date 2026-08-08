"""
Scoped Semantic Memory — Phase 2, Step 7.

Composes only already-existing machinery into one thin write/read layer over
concept_table_mappings (data/models.py). No new engine, no new approval
mechanism:

  - governance_service.GovernedObjectType.CONCEPT_MAPPING / GovernanceState —
    the same lifecycle every other governed object uses (see
    governance_service._build_concept_mapping_profile).
  - The only write path built in this cut is the human clarification-resume
    fast path (promote_clarification_selection): a user resolving a
    clarification turn (core/orchestrator/context_builder.py's
    _apply_clarification_overrides) has already made the disambiguation
    decision, so this writes governance_state='HUMAN_APPROVED' directly —
    no eligibility gate, mirroring how a human's direct dictionary approval
    (data/dictionary_service.py) never re-runs an auto-curation eligibility
    check either.
  - An auto_governance batch curator (mirroring
    data/dictionary_curation_service.py's dry-run pattern, sourcing terms
    from already-governed dictionary/domain evidence rather than raw query
    history) is deliberately NOT built in this cut — CCPP's dictionary is
    0%-approved, so it would have no governed vocabulary to source
    candidate terms from yet, and an automatic "remember what worked"
    writer from raw question history is exactly the unbounded-write/
    silent-redefinition risk the design explicitly avoids. Every read below
    only ever returns AUTO_APPROVED/HUMAN_APPROVED rows, so that curator can
    be added later purely additively without touching this file's contract.

Resolution-time consultation (query_planning_service._concept_mapping_bonus,
semantic_retrieval_service's candidate merge) calls get_approved_mappings()
below, which reads ONLY approved rows — GENERATED/SUGGESTED mappings are
invisible to live query answering, same as an unapproved dictionary entry
never earning _score_table_authority's approval bonus.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from data.db import get_connection
from data.governance_service import log_governance_event
from data.vocabulary_service import normalize_term

logger = logging.getLogger(__name__)


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def get_approved_mappings(source_id: int, concept_term: str) -> list[dict]:
    """
    Read-only. Returns every AUTO_APPROVED/HUMAN_APPROVED row for
    (source_id, normalize_term(concept_term)), highest confidence first.
    GENERATED/SUGGESTED/REJECTED/DEPRECATED rows are never returned — this
    is the "approved outranks inferred" boundary every caller relies on.
    Never raises; returns [] on any failure.

    Prefer get_all_approved_mappings() when resolving multiple terms in the
    same request (e.g. query_planning_service.plan_business_query) — it
    does one query for the whole source instead of one per term.
    """
    normalized = normalize_term(concept_term)
    if not normalized:
        return []
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT * FROM concept_table_mappings
                   WHERE source_id = ? AND concept_term = ?
                     AND governance_state IN ('AUTO_APPROVED', 'HUMAN_APPROVED')
                   ORDER BY confidence DESC""",
                (source_id, normalized),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "get_approved_mappings: read failed for source_id=%s concept_term=%r",
            source_id, concept_term, exc_info=True,
        )
        return []


def get_all_approved_mappings(source_id: int, *, session=None) -> dict[str, list[dict]]:
    """
    Read-only, one query for the whole source. Returns every AUTO_APPROVED/
    HUMAN_APPROVED row for source_id, grouped by concept_term (already
    normalized — see promote_clarification_selection, the only write path),
    each group ordered highest confidence first.

    Intended for a single request resolving multiple terms (query_planning_
    service.plan_business_query calls this once per question, not once per
    term) — avoids N separate DB round trips for what is, on most sources,
    an empty or near-empty table. Never raises; returns {} on any failure.

    Phase 3.2A: with a session, the read itself is cached per request (see
    RequestMetadataSession.get_or_compute) — a single planning request can
    call this from more than one retrieval pass (e.g. the multi-table path
    and _find_sufficient_single_object's own lookup) with different terms
    each time; the underlying source-wide row set doesn't change between
    them, so redoing the same query per pass is pure waste.
    """
    def _read() -> dict[str, list[dict]]:
        try:
            own_connection = session is None
            conn = get_connection() if own_connection else session.conn
            try:
                rows = conn.execute(
                    """SELECT * FROM concept_table_mappings
                       WHERE source_id = ? AND governance_state IN ('AUTO_APPROVED', 'HUMAN_APPROVED')
                       ORDER BY concept_term, confidence DESC""",
                    (source_id,),
                ).fetchall()
            finally:
                if own_connection:
                    conn.close()
        except Exception:
            logger.warning(
                "get_all_approved_mappings: read failed for source_id=%s", source_id, exc_info=True,
            )
            return {}

        grouped: dict[str, list[dict]] = {}
        for row in rows:
            d = dict(row)
            grouped.setdefault(d["concept_term"], []).append(d)
        return grouped

    if session is None:
        return _read()
    return session.get_or_compute(f"approved_mappings:{source_id}", _read)


def promote_clarification_selection(
    source_id: int, user_id: str, concept_term: str, table_fqn: str,
    column_name: str | None = None, *, actor_id: str,
) -> dict:
    """
    Human fast-path write: a user has just resolved a clarification turn
    (picked one candidate over the others query_planning_service already
    ranked), so this records that decision directly as HUMAN_APPROVED — no
    eligibility gate, the human already decided.

    Demotes any other currently-AUTO_APPROVED/HUMAN_APPROVED row for the
    same (source_id, concept_term, column_name) to DEPRECATED in the same
    transaction, enforcing "only one approved mapping active per term" as an
    application-level invariant — concept_table_mappings' own unique index
    is intentionally wider than that (data/models.py), to allow multiple
    competing candidate rows before one is ever approved.

    Returns {"created": True, "mapping_id": int, "demoted": [int, ...]}.
    Raises ValueError if source_id is not owned by user_id or concept_term
    normalizes to empty.
    """
    normalized = normalize_term(concept_term)
    if not normalized:
        raise ValueError("concept_term must not be empty after normalization.")
    col = column_name or ""
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            raise ValueError("source not found or not owned by user")

        demoted_rows = conn.execute(
            """SELECT id FROM concept_table_mappings
               WHERE source_id = ? AND concept_term = ? AND column_name = ?
                 AND governance_state IN ('AUTO_APPROVED', 'HUMAN_APPROVED')
                 AND NOT (table_fqn = ? AND column_name = ?)""",
            (source_id, normalized, col, table_fqn, col),
        ).fetchall()
        demoted_ids = [r["id"] for r in demoted_rows]
        if demoted_ids:
            placeholders = ",".join("?" for _ in demoted_ids)
            conn.execute(
                f"UPDATE concept_table_mappings SET governance_state = 'DEPRECATED', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (now, *demoted_ids),
            )

        evidence = [{"type": "clarification_resume", "actor_id": actor_id, "at": now}]
        conn.execute(
            """INSERT INTO concept_table_mappings
                   (source_id, concept_term, table_fqn, column_name, confidence,
                    evidence_json, competing_candidates_json, assignment_source,
                    governance_state, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1.0, ?, '[]', 'human', 'HUMAN_APPROVED', ?, ?, ?)
               ON CONFLICT (source_id, concept_term, table_fqn, column_name) DO UPDATE SET
                   confidence = 1.0,
                   evidence_json = excluded.evidence_json,
                   assignment_source = 'human',
                   governance_state = 'HUMAN_APPROVED',
                   created_by = excluded.created_by,
                   updated_at = excluded.updated_at""",
            (source_id, normalized, table_fqn, col, json.dumps(evidence), actor_id, now, now),
        )
        mapping_row = conn.execute(
            """SELECT id FROM concept_table_mappings
               WHERE source_id = ? AND concept_term = ? AND table_fqn = ? AND column_name = ?""",
            (source_id, normalized, table_fqn, col),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()

    mapping_id = mapping_row["id"]
    target = f"{table_fqn}.{col}" if col else table_fqn
    log_governance_event(
        object_type_id="concept.mapping", object_id=str(mapping_id),
        event_type="human_approved", from_state=None, to_state="HUMAN_APPROVED",
        actor_id=actor_id, notes=f"Clarification resume: '{normalized}' -> {target}",
        source_service="concept_mapping_service",
    )
    for demoted_id in demoted_ids:
        log_governance_event(
            object_type_id="concept.mapping", object_id=str(demoted_id),
            event_type="deprecated", from_state="APPROVED", to_state="DEPRECATED",
            actor_id=actor_id,
            notes=f"Superseded by mapping {mapping_id} for concept '{normalized}'.",
            source_service="concept_mapping_service",
        )

    return {"created": True, "mapping_id": mapping_id, "demoted": demoted_ids}


def get_synonym_canonical(source_id: int, term: str, *, session=None) -> str | None:
    """
    Read-only. If a human has previously taught this source that `term`
    means the same thing as some other term (see remember_synonym), returns
    that other term (already normalized) so callers can resolve `term`
    through it before falling back to fuzzy search — e.g.
    get_approved_mappings(source_id, get_synonym_canonical(source_id, term)
    or term). Returns None if nothing has been taught for this term, or on
    any failure; never raises.

    Phase 3.2A: with a session, the same (source_id, term) lookup within one
    planning request is cached (see RequestMetadataSession.get_or_compute) —
    this is called once per term, and the same term can recur across more
    than one retrieval pass in a single request.
    """
    normalized = normalize_term(term)
    if not normalized:
        return None

    def _read() -> str | None:
        try:
            own_connection = session is None
            conn = get_connection() if own_connection else session.conn
            try:
                row = conn.execute(
                    "SELECT canonical_term FROM concept_term_synonyms WHERE source_id = ? AND synonym_term = ?",
                    (source_id, normalized),
                ).fetchone()
                return row["canonical_term"] if row else None
            finally:
                if own_connection:
                    conn.close()
        except Exception:
            logger.warning(
                "get_synonym_canonical: read failed for source_id=%s term=%r",
                source_id, term, exc_info=True,
            )
            return None

    if session is None:
        return _read()
    return session.get_or_compute(f"synonym_canonical:{source_id}:{normalized}", _read)


def remember_synonym(
    source_id: int, user_id: str, synonym_term: str, canonical_term: str,
    *, actor_id: str, confirm_replace: bool = False,
) -> dict:
    """
    Conversational memory write: the user just confirmed (via the
    clarification "remember this choice" action) that `synonym_term` means
    the same thing as `canonical_term` on this source. No confidence/
    approval lifecycle — a human either taught this or didn't.

    A term means one thing at a time. If `synonym_term` was already taught
    to mean a *different* canonical_term:
      - confirm_replace=False (default): the existing teaching is left
        untouched and {"conflict": True, "existing_canonical_term": ...,
        "new_canonical_term": ...} is returned so the caller can ask the
        user "You previously taught me X means <existing>. Replace with
        <new>?" — nothing is written.
      - confirm_replace=True: the row is updated to the new canonical_term.

    Re-teaching the same (synonym_term, canonical_term) pair again is a
    no-op ({"created": False, "unchanged": True}). Teaching a term to mean
    itself is also a no-op (nothing to remember).

    Raises ValueError if source_id is not owned by user_id, or if either
    term normalizes to empty.
    """
    normalized_synonym = normalize_term(synonym_term)
    normalized_canonical = normalize_term(canonical_term)
    if not normalized_synonym or not normalized_canonical:
        raise ValueError("synonym_term and canonical_term must not be empty after normalization.")
    now = datetime.now(timezone.utc).isoformat()

    if normalized_synonym == normalized_canonical:
        return {"created": False, "unchanged": True, "canonical_term": normalized_canonical}

    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            raise ValueError("source not found or not owned by user")

        existing = conn.execute(
            "SELECT id, canonical_term FROM concept_term_synonyms WHERE source_id = ? AND synonym_term = ?",
            (source_id, normalized_synonym),
        ).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO concept_term_synonyms
                       (source_id, synonym_term, canonical_term, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_id, normalized_synonym, normalized_canonical, actor_id, now, now),
            )
            conn.commit()
            result = {
                "created": True,
                "synonym_term": normalized_synonym,
                "canonical_term": normalized_canonical,
            }
        elif existing["canonical_term"] == normalized_canonical:
            result = {
                "created": False, "unchanged": True,
                "synonym_term": normalized_synonym, "canonical_term": normalized_canonical,
            }
        elif not confirm_replace:
            result = {
                "conflict": True,
                "synonym_term": normalized_synonym,
                "existing_canonical_term": existing["canonical_term"],
                "new_canonical_term": normalized_canonical,
            }
        else:
            previous = existing["canonical_term"]
            conn.execute(
                "UPDATE concept_term_synonyms SET canonical_term = ?, updated_at = ? WHERE id = ?",
                (normalized_canonical, now, existing["id"]),
            )
            conn.commit()
            result = {
                "created": False, "replaced": True,
                "synonym_term": normalized_synonym,
                "previous_canonical_term": previous,
                "canonical_term": normalized_canonical,
            }
    finally:
        conn.close()

    return result
