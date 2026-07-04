import json
import logging
from datetime import datetime, timezone

from core.entities.learning import LearnedEntityRule, apply_learned_entity_rules
from core.entities.rules import detect_table_entity
from data.db import get_connection

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
        created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_id, table_fqn) DO UPDATE SET
        profiling_snapshot_id   = excluded.profiling_snapshot_id,
        entity                  = excluded.entity,
        confidence              = excluded.confidence,
        evidence_json           = excluded.evidence_json,
        competing_entities_json = excluded.competing_entities_json,
        updated_at              = excluded.updated_at
"""


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def generate_entity_assignments(source_id: int, user_id: str) -> dict | None:
    """Classify every profiled table for source_id into a business entity and persist.

    Uses the latest profiling snapshot.  Column semantic types from
    profiling_column_profiles are passed to detect_table_entity() as
    additional signal.

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

        snap_row = conn.execute(
            "SELECT id FROM profiling_snapshots "
            "WHERE source_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if snap_row is None:
            raise ValueError(
                "No profiling snapshot found. "
                "Run POST /sources/{id}/profile/structural first."
            )

        snap_id = snap_row["id"]

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


def get_entity_summary(source_id: int, user_id: str) -> dict | None:
    """Return aggregate entity counts for source_id.

    Returns None if source_id does not belong to user_id.
    Returns a zeroed summary (no error) if no assignments have been generated yet.
    """
    conn = get_connection()
    try:
        owns = conn.execute(
            "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, user_id),
        ).fetchone()
        if owns is None:
            return None

        count_rows = conn.execute(
            "SELECT entity, COUNT(*) AS cnt "
            "FROM entity_assignments "
            "WHERE source_id = ? "
            "GROUP BY entity ORDER BY cnt DESC",
            (source_id,),
        ).fetchall()

        meta = conn.execute(
            "SELECT COUNT(*) AS total, MAX(updated_at) AS last_generated_at "
            "FROM entity_assignments WHERE source_id = ?",
            (source_id,),
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
