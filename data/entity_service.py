import json
import logging
from datetime import datetime, timezone

from core.entities.learning import LearnedEntityRule, apply_learned_entity_rules
from core.entities.rules import detect_table_entity
from data.db import get_connection
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_UPSERT = """
    INSERT INTO entity_assignments (
        source_id, profiling_snapshot_id, table_fqn, entity,
        confidence, evidence_json, competing_entities_json,
        assignment_source, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn) DO UPDATE SET
        profiling_snapshot_id   = excluded.profiling_snapshot_id,
        entity                  = excluded.entity,
        confidence              = excluded.confidence,
        evidence_json           = excluded.evidence_json,
        competing_entities_json = excluded.competing_entities_json,
        assignment_source       = excluded.assignment_source,
        updated_at              = excluded.updated_at
    WHERE entity_assignments.assignment_source NOT IN ('human', 'auto_governance')
"""
# Milestone M-23: see domain_service.py's identical _UPSERT comment —
# 'auto_governance' rows are protected from silent overwrite the same way
# 'human' rows already are.
#
# Business Learning Trust Hardening milestone — assignment_source is now a
# bound parameter (was hardcoded 'rule') so generate_entity_assignments can
# tag rows written from the Business-Learning connect-time bounded discovery
# path (core.orchestrator.business_learning -> core.analysis.
# cold_start_discovery.ensure_business_evidence) as 'preliminary_connect_time'
# — distinguishable from an ordinary on-demand 'rule' row written by normal
# question-time preparation. assignment_source is now also re-asserted on
# UPDATE (excluded.assignment_source) so a later on-demand regeneration for
# the same table correctly PROMOTES a preliminary row back to 'rule' instead
# of leaving it permanently marked preliminary.


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def generate_entity_assignments(
    source_id: int, user_id: str, table_fqns: list[str] | None = None,
    assignment_source: str = 'rule',
) -> dict | None:
    """Classify every profiled table for source_id into a business entity and persist.

    Uses the latest profiling snapshot.  Column semantic types from
    profiling_column_profiles are passed to detect_table_entity() as
    additional signal.

    table_fqns: when provided, only these tables are classified/upserted (used
    by the autonomous metadata lifecycle to refresh changed/new objects only).
    None (default) preserves the original full-source behavior used by the
    manual "Generate Entities" action. Rows with assignment_source='human' are
    never overwritten regardless of this parameter (see _UPSERT guard).

    assignment_source: provenance tag written to entity_assignments.
    assignment_source. Defaults to 'rule' (ordinary on-demand generation).
    Business Learning Trust Hardening milestone: core.orchestrator.
    business_learning's connect-time bounded discovery path passes
    'preliminary_connect_time' here so core.semantic.ai_interpreter.
    build_grounding_vocabulary can exclude these rows from the "known,
    trust these" vocabulary block, and data.query_planning_service can
    treat them as non-definitive — see those modules for the consuming
    side of this trust boundary.

    Returns:
        Summary dict, or None if source_id does not belong to user_id.

    Raises:
        ValueError: No profiling snapshot exists for this source.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        snapshot = get_latest_profiling_snapshot(source_id, conn=conn)
        if snapshot is None:
            raise ValueError(
                "No profiling snapshot found. "
                "Run POST /sources/{id}/profile/structural first."
            )

        snap_id = snapshot.id

        if table_fqns is not None:
            if not table_fqns:
                table_rows = []
                col_rows = []
            else:
                placeholders = ",".join("?" for _ in table_fqns)
                table_rows = conn.execute(
                    "SELECT * FROM profiling_table_profiles "
                    f"WHERE profiling_snapshot_id = ? AND table_fqn IN ({placeholders})",
                    (snap_id, *table_fqns),
                ).fetchall()
                col_rows = conn.execute(
                    "SELECT table_fqn, semantic_type, semantic_confidence, cardinality_tier, "
                    "uniqueness_score, null_percentage, blank_percentage, quality_score, "
                    "quality_grade, distribution_shape, pii_confirmed, pii_signals_json, "
                    "dominant_pattern, pattern_coverage "
                    f"FROM profiling_column_profiles "
                    f"WHERE profiling_snapshot_id = ? AND table_fqn IN ({placeholders})",
                    (snap_id, *table_fqns),
                ).fetchall()
        else:
            table_rows = conn.execute(
                "SELECT * FROM profiling_table_profiles "
                "WHERE profiling_snapshot_id = ?",
                (snap_id,),
            ).fetchall()

            # Full column profiles per table — used for deep profiling entity intelligence.
            # Fetches all columns (not just those with semantic_type) to use cardinality,
            # quality, distribution, PII signals alongside semantic type.
            col_rows = conn.execute(
                "SELECT table_fqn, semantic_type, semantic_confidence, cardinality_tier, "
                "uniqueness_score, null_percentage, blank_percentage, quality_score, "
                "quality_grade, distribution_shape, pii_confirmed, pii_signals_json, "
                "dominant_pattern, pattern_coverage "
                "FROM profiling_column_profiles "
                "WHERE profiling_snapshot_id = ?",
                (snap_id,),
            ).fetchall()

        # Load APPROVED active entity rules for this source, sorted by
        # confidence desc so higher-confidence rules take priority.
        rule_rows = conn.execute(
            "SELECT * FROM entity_learning_rules "
            "WHERE source_id = ? AND approval_status = 'APPROVED' AND active = 1 "
            "ORDER BY confidence DESC",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    sem_map: dict[str, list[str]] = {}
    col_profiles_map: dict[str, list[dict]] = {}
    for row in col_rows:
        row_dict = dict(row)
        fqn = row_dict["table_fqn"]
        col_profiles_map.setdefault(fqn, []).append(row_dict)
        sem = row_dict.get("semantic_type")
        if sem is not None:
            sem_map.setdefault(fqn, []).append(sem)

    learned_rules: list[LearnedEntityRule] = []
    for row in rule_rows:
        d = dict(row)
        learned_rules.append(LearnedEntityRule(
            id=d["id"],
            source_id=d["source_id"],
            pattern_type=d["pattern_type"],
            pattern_value=d["pattern_value"],
            entity=d["entity"],
            confidence=float(d["confidence"]),
            approval_status=d["approval_status"],
            created_by=d["created_by"],
            approved_by=d.get("approved_by"),
            created_at=d["created_at"],
            approved_at=d.get("approved_at"),
            active=bool(d["active"]),
        ))

    now = _now()
    assignments = []
    learned_matches   = 0
    heuristic_matches = 0
    unknown_matches   = 0

    for row in table_rows:
        profile   = dict(row)
        sem_types = sem_map.get(profile["table_fqn"])

        # Learned rules take priority; fall back to heuristic detection.
        if learned_rules:
            a = apply_learned_entity_rules(profile, learned_rules)
            if a is not None:
                learned_matches += 1
                assignments.append(a)
                continue

        a = detect_table_entity(
            profile,
            sem_types,
            column_profiles=col_profiles_map.get(profile["table_fqn"]),
        )
        assignments.append(a)
        if a.entity == "Unknown":
            unknown_matches += 1
        else:
            heuristic_matches += 1

    conn = get_connection()
    try:
        for a in assignments:
            competing = [
                {"entity": c.entity, "score": c.score, "evidence": c.evidence}
                for c in a.competing_entities
            ]
            conn.execute(
                _UPSERT,
                (
                    source_id,
                    snap_id,
                    a.table_fqn,
                    a.entity,
                    a.confidence,
                    json.dumps(a.evidence),
                    json.dumps(competing),
                    assignment_source,
                    now,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    entity_counts: dict[str, int] = {}
    for a in assignments:
        entity_counts[a.entity] = entity_counts.get(a.entity, 0) + 1

    tables_total   = len(assignments)
    tables_unknown = entity_counts.get("Unknown", 0)

    return {
        "source_id":             source_id,
        "profiling_snapshot_id": snap_id,
        "tables_total":          tables_total,
        "entities_assigned":     tables_total - tables_unknown,
        "entities_unknown":      tables_unknown,
        "entity_counts":         entity_counts,
        "learned_matches":       learned_matches,
        "heuristic_matches":     heuristic_matches,
        "unknown_matches":       unknown_matches,
        "generated_at":          now,
    }


def _best_persisted_entity_contract(source_id: int, entity_name: str, *, session=None) -> dict | None:
    """The highest-confidence RESOLVED/PARTIAL contract ever persisted for
    entity_name on this source — NOT simply the latest contract_version.

    data.semantic_contract_service.get_latest_entity_contract intentionally
    returns the most recent version even when it's a worse result than an
    earlier one (e.g. a later standalone question asked about the term
    alone, with no other grounded entity to relationship-rescue against,
    correctly lands on NO_CANDIDATE and is persisted as the new latest
    version — observed live for the reference database's own 'Program' contract history:
    version 4 was PARTIAL/0.75/dbo.EDU_Track, but version 5 was a later
    NO_CANDIDATE). Using "latest" as this function's own reuse-check would
    make a good, already-proven resolution invisible again. This is a
    read-only, additional query against the SAME table, not a change to
    get_latest_entity_contract's own (correct, differently-scoped) meaning.
    """
    import json as _json

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        row = conn.execute(
            "SELECT * FROM semantic_entity_contracts "
            "WHERE source_id = ? AND entity_name = ? AND canonical_table_fqn IS NOT NULL "
            "  AND resolution_status IN ('RESOLVED', 'PARTIAL') "
            "ORDER BY confidence DESC, contract_version DESC LIMIT 1",
            (source_id, entity_name),
        ).fetchone()
    finally:
        if own_connection:
            conn.close()
    if row is None:
        return None
    d = dict(row)
    d["contract"] = _json.loads(d.pop("contract_json") or "{}")
    return d


def promote_unresolved_entities_via_semantic_contracts(
    source_id: int, user_id: str, *, session=None,
) -> dict:
    """Milestone 5 — bounded, autonomous entity-coverage repair.

    generate_entity_assignments (above) classifies every table purely from
    table-name/schema-name keywords plus profiling-column signals
    (core.entities.rules.detect_table_entity) — it never looks at a
    column's own NAME, and it never looks at relationship structure. Some
    tables only carry their real business meaning in a column name (e.g. a
    "ProgramType" column on a table named "EDU_Track", which shares no word
    with "program" at all) reachable via a trusted relationship to a table
    ALREADY confidently classified — exactly the evidence
    data.semantic_contract_service._relationship_rescued_candidate already
    computes for live Q&A (backed by data.vocabulary_bootstrap_service's
    generated, per-column business vocabulary), but whose result was never
    consulted by this batch/business-map path.

    Confirmed live against the reference database: entity_assignments already had 16 weak
    (confidence ~0.5, table/schema-name-substring) 'Program' matches (e.g.
    dnnuser.Portals, dbo.EDU_Bio_Degrees) — so a naive "entity already
    has a table" gate would wrongly treat Program as fully covered — while
    the actual central program-catalog table, dbo.EDU_Track, stayed
    'Unknown' and a real PARTIAL/0.75 semantic_entity_contracts row for it
    already existed (created by an earlier live question) and was simply
    never consulted here. This function does not replace or remove any
    existing weak assignment; it only ever ADDS a promotion onto a table
    that is currently 'Unknown', so downstream authority-score ranking
    (data.business_knowledge_service.get_source_metric_evidence) can weigh
    the newly-labeled table against whatever was already assigned.

    For every canonical vocabulary entity name (core.entities.rules.
    _ENTITY_KEYWORDS), reuse-first: check get_latest_entity_contract for an
    already-persisted RESOLVED/PARTIAL contract before computing anything
    new. Only when no usable contract exists yet does it attempt ONE bounded
    relationship-rescue lookup, anchored on the tables already confidently
    assigned to OTHER entities. A promotion is only ever written onto a
    table whose current assignment is still 'Unknown' — this never
    overrides an existing, different classification.

    Idempotent / learning: the reuse-first check means a second call for an
    entity that was already resolved (by this function or by an earlier
    live question) does ZERO new AI/vocabulary/relationship-BFS work and
    creates no duplicate semantic_entity_contracts row — it only re-confirms
    (or, if the table has since been promoted, no-ops) the entity_assignments
    upsert.

    No global scan: at most len(_ENTITY_KEYWORDS) contract lookups (~15,
    each O(1)), and a fresh rescue computation only for entity names with no
    prior contract at all — each such computation is itself bounded (one
    generated-vocabulary lookup + one capped relationship-hop BFS, both
    already cached per session). No new profiling, no new dictionary
    generation, no per-table AI call.

    Returns a summary dict; never raises (best-effort autonomous repair —
    a rescue failure for one entity name is logged and skipped, not fatal).
    """
    from core.entities.rules import _ENTITY_KEYWORDS
    from data.semantic_contract_service import _relationship_rescued_candidate

    empty_result = {
        "source_id": source_id, "promoted": {}, "attempted": [], "reused_existing_contract": [],
    }

    summary = get_entity_summary(source_id, user_id)
    if summary is None or summary["tables_total"] == 0:
        return empty_result

    assignments = list_entity_assignments(source_id, user_id) or []
    by_entity: dict[str, list[str]] = {}
    for a in assignments:
        by_entity.setdefault(a["entity"], []).append(a["table_fqn"])

    anchor_tables: set[str] = {
        fqn for entity, fqns in by_entity.items() if entity != "Unknown" for fqn in fqns
    }
    if not anchor_tables:
        return empty_result

    promoted: dict[str, str] = {}
    attempted: list[str] = []
    reused_existing_contract: list[str] = []

    conn = get_connection()
    try:
        snapshot = get_latest_profiling_snapshot(source_id, conn=conn)
        snap_id = snapshot.id if snapshot else None
    finally:
        conn.close()
    if snap_id is None:
        return empty_result

    # Read/rescue phase — deliberately holds NO connection open across these
    # calls: _relationship_rescued_candidate (and, when it succeeds,
    # persist_entity_contract inside it) opens its own short-lived
    # connection per call. Holding this function's own connection open across
    # the whole loop caused real "database is locked" contention against
    # that nested write (observed live against the reference database) — collecting proposed
    # promotions here and writing them in one short transaction below avoids
    # it entirely.
    to_persist: list[tuple[str, str, float, list[str]]] = []
    for entity_name in _ENTITY_KEYWORDS:
        existing = _best_persisted_entity_contract(source_id, entity_name, session=session)
        if existing:
            reused_existing_contract.append(entity_name)
            contract = existing
        else:
            attempted.append(entity_name)
            try:
                contract = _relationship_rescued_candidate(
                    source_id, user_id, entity_name, anchor_tables, session=session,
                )
            except Exception:  # noqa: BLE001 - best-effort autonomous repair, never fatal
                logger.warning(
                    "promote_unresolved_entities_via_semantic_contracts: rescue failed for "
                    "source_id=%s entity_name=%s", source_id, entity_name, exc_info=True,
                )
                continue
        if not contract or not contract.get("canonical_table_fqn"):
            continue

        contract_evidence = (contract.get("contract") or {}).get("evidence") or []
        evidence = [
            f"Promoted from an existing semantic entity contract "
            f"(generation_method={contract.get('generation_method', 'relationship_rescue')!r}, "
            f"confidence={contract.get('confidence')}): {e}"
            for e in contract_evidence
        ] or [f"Promoted from semantic_entity_contracts (confidence={contract.get('confidence')})"]
        to_persist.append((entity_name, contract["canonical_table_fqn"], contract.get("confidence") or 0.0, evidence))

    if not to_persist:
        return {
            "source_id": source_id, "promoted": promoted, "attempted": attempted,
            "reused_existing_contract": reused_existing_contract,
        }

    # Confidence-based conflict resolution — two DIFFERENT entity names can
    # each independently rescue onto the SAME table (observed live in a
    # fixture mirroring the a real production database shape: a "Paths" table drew both a
    # curated course/path vocabulary synonym at 0.45 confidence AND a
    # ProgramType column-name signal at 0.35). Persisting whichever entity
    # happened to iterate first would make the outcome depend on
    # _ENTITY_KEYWORDS' arbitrary dict order rather than evidence strength.
    # Keep only the highest-confidence claim per table; the rest are still
    # honestly reported in `attempted`/`reused_existing_contract` (a real
    # attempt was made), they simply lost the tie-break.
    best_per_table: dict[str, tuple[str, str, float, list[str]]] = {}
    for entry in to_persist:
        entity_name, table_fqn, confidence, evidence = entry
        current_best = best_per_table.get(table_fqn)
        if current_best is None or confidence > current_best[2]:
            best_per_table[table_fqn] = entry
    to_persist = list(best_per_table.values())

    now = _now()
    conn = get_connection()
    try:
        for entity_name, table_fqn, confidence, evidence in to_persist:
            current = conn.execute(
                "SELECT entity FROM entity_assignments WHERE source_id = ? AND table_fqn = ?",
                (source_id, table_fqn),
            ).fetchone()
            if current is not None and current["entity"] != "Unknown":
                continue  # never override an existing, different classification

            conn.execute(
                _UPSERT,
                (source_id, snap_id, table_fqn, entity_name, confidence, json.dumps(evidence), json.dumps([]), 'rule', now, now),
            )
            promoted[entity_name] = table_fqn
        conn.commit()
    finally:
        conn.close()

    return {
        "source_id": source_id, "promoted": promoted, "attempted": attempted,
        "reused_existing_contract": reused_existing_contract,
    }


def lock_entity_assignment(
    source_id: int, user_id: str, table_fqn: str, entity: str | None = None
) -> dict | None:
    """Mark an entity assignment as human-set so it is never overwritten by
    generate_entity_assignments() again.

    If entity is provided, the value is corrected at the same time. Otherwise
    the existing entity value is left as-is and only assignment_source flips
    to 'human'.

    Returns the updated row, or None if source_id does not belong to user_id
    or no assignment row exists yet for table_fqn.
    """
    now = _now()
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        if entity is not None:
            cursor = conn.execute(
                "UPDATE entity_assignments "
                "SET entity = ?, assignment_source = 'human', updated_at = ? "
                "WHERE source_id = ? AND table_fqn = ?",
                (entity, now, source_id, table_fqn),
            )
        else:
            cursor = conn.execute(
                "UPDATE entity_assignments "
                "SET assignment_source = 'human', updated_at = ? "
                "WHERE source_id = ? AND table_fqn = ?",
                (now, source_id, table_fqn),
            )
        conn.commit()

        if cursor.rowcount == 0:
            return None

        row = conn.execute(
            "SELECT * FROM entity_assignments WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType, log_governance_event,
        )
        log_governance_event(
            object_type_id = GovernedObjectType.ENTITY_ASSIGNMENT,
            object_id      = f"{source_id}:{table_fqn}",
            event_type     = "HUMAN_LOCK",
            from_state     = GovernanceState.GENERATED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "entity_service",
        )
    except Exception:
        logger.warning("governance logging failed for entity.assignment %s:%s", source_id, table_fqn)

    d = dict(row)
    d["evidence"]           = json.loads(d.pop("evidence_json", "[]") or "[]")
    d["competing_entities"] = json.loads(d.pop("competing_entities_json", "[]") or "[]")
    return d


def auto_mature_entity_assignment(
    source_id: int, table_fqn: str, *, actor_id: str,
) -> dict | None:
    """
    Mark an entity assignment as policy-matured (Milestone M-23 — Enterprise
    Semantic Governance Rollout). See domain_service.auto_mature_domain_assignment's
    docstring — identical contract, entity-assignment side.
    """
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE entity_assignments "
            "SET assignment_source = 'auto_governance', updated_at = ? "
            "WHERE source_id = ? AND table_fqn = ? AND assignment_source != 'human'",
            (now, source_id, table_fqn),
        )
        conn.commit()

        if cursor.rowcount == 0:
            return None

        row = conn.execute(
            "SELECT * FROM entity_assignments WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType, log_governance_event, upsert_governance_state,
        )
        object_id = f"{source_id}:{table_fqn}"
        log_governance_event(
            object_type_id = GovernedObjectType.ENTITY_ASSIGNMENT,
            object_id      = object_id,
            event_type     = "AUTO_GOVERNANCE_APPROVED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.AUTO_APPROVED,
            actor_id       = actor_id,
            source_service = "entity_service",
        )
        upsert_governance_state(
            object_type_id   = GovernedObjectType.ENTITY_ASSIGNMENT,
            object_id        = object_id,
            approval_state   = GovernanceState.AUTO_APPROVED.value,
            confidence_score = row["confidence"] if row else None,
            reviewer_id      = actor_id,
            reviewed_at      = now,
        )
    except Exception:
        logger.warning("governance logging failed for entity.assignment %s:%s", source_id, table_fqn)

    d = dict(row)
    d["evidence"]           = json.loads(d.pop("evidence_json", "[]") or "[]")
    d["competing_entities"] = json.loads(d.pop("competing_entities_json", "[]") or "[]")
    return d


def list_entity_assignments(source_id: int, user_id: str) -> list[dict] | None:
    """Return all entity assignments for source_id, ordered by entity then confidence.

    Returns None if source_id does not belong to user_id.
    evidence_json and competing_entities_json are parsed into native Python objects.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        rows = conn.execute(
            "SELECT * FROM entity_assignments "
            "WHERE source_id = ? "
            "ORDER BY entity, confidence DESC",
            (source_id,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        d["evidence"]            = json.loads(d.pop("evidence_json", "[]"))
        d["competing_entities"]  = json.loads(d.pop("competing_entities_json", "[]"))
        result.append(d)
    return result


def get_entity_summary(
    source_id: int, user_id: str, *, exclude_preliminary: bool = False,
) -> dict | None:
    """Return aggregate entity counts for source_id.

    Returns None if source_id does not belong to user_id.
    Returns a zeroed summary (no error) if no assignments have been generated yet.

    exclude_preliminary: Business Learning Trust Hardening milestone — when
    True, rows with assignment_source='preliminary_connect_time' (written by
    core.orchestrator.business_learning's bounded, blind, pre-question
    connect-time discovery pass) are excluded from the counts. Default False
    preserves every existing caller's behavior unchanged (dashboards/reports
    legitimately want to see ALL classified tables regardless of provenance).
    core.semantic.ai_interpreter.build_grounding_vocabulary is the one caller
    that passes True: its "known business vocabulary — prefer these terms"
    prompt block must only ever contain VALIDATED evidence, never an
    unverified preliminary guess (which already gets its own, separately-
    labeled "UNVALIDATED" hint section from the preliminary business map).
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        source_filter = "source_id = ?"
        params: tuple = (source_id,)
        if exclude_preliminary:
            source_filter += " AND assignment_source != 'preliminary_connect_time'"

        # Deterministic Semantic Planning — "ORDER BY cnt DESC" alone leaves
        # a tied count's relative order unspecified; entity name breaks the
        # tie so this summary (feeds core.semantic.ai_interpreter.build_
        # grounding_vocabulary's AI-prompt vocabulary hint, among other
        # consumers) is byte-identical across repeated calls for unchanged
        # data, instead of varying with whatever incidental row order the
        # engine returns for a tie.
        count_rows = conn.execute(
            "SELECT entity, COUNT(*) AS cnt "
            "FROM entity_assignments "
            f"WHERE {source_filter} "
            "GROUP BY entity ORDER BY cnt DESC, entity ASC",
            params,
        ).fetchall()

        meta = conn.execute(
            "SELECT COUNT(*) AS total, MAX(updated_at) AS last_generated_at "
            f"FROM entity_assignments WHERE {source_filter}",
            params,
        ).fetchone()
    finally:
        conn.close()

    tables_total = meta["total"] if meta else 0
    if tables_total == 0:
        return {
            "source_id":         source_id,
            "tables_total":      0,
            "entities_assigned": 0,
            "entities_unknown":  0,
            "entity_counts":     {},
            "last_generated_at": None,
        }

    entity_counts  = {r["entity"]: r["cnt"] for r in count_rows}
    tables_unknown = entity_counts.get("Unknown", 0)

    return {
        "source_id":         source_id,
        "tables_total":      tables_total,
        "entities_assigned": tables_total - tables_unknown,
        "entities_unknown":  tables_unknown,
        "entity_counts":     entity_counts,
        "last_generated_at": meta["last_generated_at"],
    }
