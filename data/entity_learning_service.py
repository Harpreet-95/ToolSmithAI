import json
import logging
from datetime import datetime, timezone

from core.entities.learning import LearnedEntityRule, suggest_entity_rules
from data.db import get_connection

logger = logging.getLogger(__name__)

_UPSERT_SUGGESTION = """
    INSERT INTO entity_learning_rules (
        source_id, pattern_type, pattern_value, entity, confidence,
        approval_status, created_by, created_at, active
    ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, 0)
    ON CONFLICT(source_id, pattern_type, pattern_value) DO NOTHING
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owns(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def _row_to_rule(row) -> LearnedEntityRule:
    d = dict(row)
    return LearnedEntityRule(
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
    )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------

def generate_entity_rule_suggestions(source_id: int, user_id: str) -> dict | None:
    """Analyse current Unknown entity_assignments and create PENDING rule suggestions.

    Uses suggest_entity_rules() to find high-frequency patterns in Unknown
    tables, then persists each as a PENDING entity_learning_rules row.
    Re-runs are safe — ON CONFLICT DO NOTHING skips already-suggested patterns.

    Returns:
        Summary dict, or None if source not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _owns(conn, source_id, user_id):
            return None

        rows = conn.execute("""
            SELECT ea.table_fqn, ptp.table_name, ptp.schema_name,
                   ea.competing_entities_json
            FROM entity_assignments ea
            JOIN profiling_table_profiles ptp
                ON ptp.profiling_snapshot_id = ea.profiling_snapshot_id
               AND ptp.table_fqn = ea.table_fqn
            WHERE ea.source_id = ? AND ea.entity = 'Unknown'
        """, (source_id,)).fetchall()
    finally:
        conn.close()

    unknown_tables = []
    for row in rows:
        d = dict(row)
        try:
            competing = json.loads(d.get("competing_entities_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            competing = []
        unknown_tables.append({
            "table_fqn":          d["table_fqn"],
            "table_name":         d["table_name"],
            "schema_name":        d["schema_name"],
            "competing_entities": competing,
        })

    if not unknown_tables:
        return {
            "source_id":           source_id,
            "unknown_tables":      0,
            "suggestions_new":     0,
            "suggestions_skipped": 0,
            "generated_at":        _now(),
        }

    suggestions = suggest_entity_rules(unknown_tables)
    now = _now()
    new_count = skipped_count = 0

    conn = get_connection()
    try:
        for s in suggestions:
            cursor = conn.execute(
                _UPSERT_SUGGESTION,
                (
                    source_id,
                    s["pattern_type"],
                    s["pattern_value"],
                    s["suggested_entity"],
                    s["suggested_confidence"],
                    user_id,
                    now,
                ),
            )
            if cursor.rowcount > 0:
                new_count += 1
            else:
                skipped_count += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "source_id":           source_id,
        "unknown_tables":      len(unknown_tables),
        "suggestions_new":     new_count,
        "suggestions_skipped": skipped_count,
        "generated_at":        now,
    }


def list_entity_rule_suggestions(source_id: int, user_id: str) -> list[dict] | None:
    """Return PENDING rule suggestions for source_id ordered by confidence desc.

    Returns None if source not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _owns(conn, source_id, user_id):
            return None
        rows = conn.execute("""
            SELECT * FROM entity_learning_rules
            WHERE source_id = ? AND approval_status = 'PENDING'
            ORDER BY confidence DESC, pattern_type, pattern_value
        """, (source_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def approve_entity_rule(rule_id: int, user_id: str) -> dict | None:
    """Approve a PENDING rule — sets approval_status=APPROVED, active=1.

    Returns:
        Updated rule dict, or None if rule not found or user does not own
        the source.

    Raises:
        ValueError: Rule is not in PENDING state.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM entity_learning_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["approval_status"] != "PENDING":
            raise ValueError(
                f"Rule {rule_id} is already '{d['approval_status']}' "
                "and cannot be approved again."
            )
        if not _owns(conn, d["source_id"], user_id):
            return None

        conn.execute("""
            UPDATE entity_learning_rules
               SET approval_status = 'APPROVED',
                   active          = 1,
                   approved_by     = ?,
                   approved_at     = ?
             WHERE id = ?
        """, (user_id, now, rule_id))
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM entity_learning_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        _confidence = float(dict(updated).get("confidence") or 0.8)
        log_governance_event(
            object_type_id = GovernedObjectType.ENTITY_RULE,
            object_id      = str(rule_id),
            event_type     = "APPROVED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.HUMAN_APPROVED,
            actor_id       = user_id,
            source_service = "entity_learning_service",
        )
        upsert_governance_state(
            object_type_id   = GovernedObjectType.ENTITY_RULE,
            object_id        = str(rule_id),
            approval_state   = GovernanceState.HUMAN_APPROVED,
            confidence_score = _confidence,
            reviewer_id      = user_id,
            reviewed_at      = now,
        )
    except Exception:
        logger.warning("governance logging failed for entity.rule id=%s", rule_id)

    return dict(updated)


def reject_entity_rule(rule_id: int, user_id: str) -> dict | None:
    """Reject a PENDING rule — sets approval_status=REJECTED, active=0.

    Returns:
        Updated rule dict, or None if rule not found or user does not own
        the source.

    Raises:
        ValueError: Rule is not in PENDING state.
    """
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM entity_learning_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if row is None:
            return None

        d = dict(row)
        if d["approval_status"] != "PENDING":
            raise ValueError(
                f"Rule {rule_id} is already '{d['approval_status']}' "
                "and cannot be rejected again."
            )
        if not _owns(conn, d["source_id"], user_id):
            return None

        conn.execute("""
            UPDATE entity_learning_rules
               SET approval_status = 'REJECTED',
                   active          = 0,
                   approved_by     = ?,
                   approved_at     = ?
             WHERE id = ?
        """, (user_id, now, rule_id))
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM entity_learning_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    finally:
        conn.close()

    try:
        from data.governance_service import (
            GovernanceState, GovernedObjectType,
            log_governance_event, upsert_governance_state,
        )
        log_governance_event(
            object_type_id = GovernedObjectType.ENTITY_RULE,
            object_id      = str(rule_id),
            event_type     = "REJECTED",
            from_state     = GovernanceState.SUGGESTED,
            to_state       = GovernanceState.REJECTED,
            actor_id       = user_id,
            source_service = "entity_learning_service",
        )
        upsert_governance_state(
            object_type_id = GovernedObjectType.ENTITY_RULE,
            object_id      = str(rule_id),
            approval_state = GovernanceState.REJECTED,
            reviewer_id    = user_id,
            reviewed_at    = now,
        )
    except Exception:
        logger.warning("governance logging failed for entity.rule id=%s", rule_id)

    return dict(updated)


def list_entity_rules(source_id: int, user_id: str) -> list[dict] | None:
    """Return all rules for source_id across all statuses.

    Ordered: APPROVED first, then PENDING, then REJECTED; within each group
    by confidence desc.

    Returns None if source not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _owns(conn, source_id, user_id):
            return None
        rows = conn.execute("""
            SELECT * FROM entity_learning_rules
            WHERE source_id = ?
            ORDER BY
                CASE approval_status
                    WHEN 'APPROVED' THEN 0
                    WHEN 'PENDING'  THEN 1
                    ELSE                 2
                END,
                confidence DESC,
                pattern_type,
                pattern_value
        """, (source_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
