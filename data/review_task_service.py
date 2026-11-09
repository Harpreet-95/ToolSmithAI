import json
from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_review_task(
    source_id: int,
    object_type: str,
    table_fqn: str,
    column_name: str = "",
    reasoning: list[dict] | None = None,
    suggested_domain: str | None = None,
    suggested_entity: str | None = None,
    suggested_business_name: str | None = None,
    suggested_description: str | None = None,
    confidence: float | None = None,
    created_by: str = "lifecycle_engine",
) -> int | None:
    """Create a review task in the shared review queue (ai_semantic_suggestions).

    Dedupes on (source_id, object_type, table_fqn, column_name, status='PENDING') —
    identical policy to dictionary_service._insert_ai_suggestions(), so re-running
    the autonomous lifecycle on an unchanged diff never creates duplicate tasks.

    provider/model/prompt_version are left NULL — these tasks are rule-derived,
    not AI-generated, matching the "no AI calls" constraint of the autonomous
    metadata lifecycle.

    Returns the new row id, or None if a PENDING duplicate already exists.
    """
    conn = get_connection()
    try:
        existing = conn.execute(
            """SELECT id FROM ai_semantic_suggestions
               WHERE source_id = ? AND object_type = ?
               AND table_fqn = ? AND column_name = ? AND status = 'PENDING'""",
            (source_id, object_type, table_fqn, column_name),
        ).fetchone()
        if existing:
            return None

        cursor = conn.execute(
            """INSERT INTO ai_semantic_suggestions
               (source_id, object_type, table_fqn, column_name,
                suggested_business_name, suggested_description,
                suggested_domain, suggested_entity,
                ai_confidence, ai_reasoning_json, review_required,
                status, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'PENDING', ?, ?)""",
            (
                source_id, object_type, table_fqn, column_name,
                suggested_business_name,
                suggested_description,
                suggested_domain,
                suggested_entity,
                confidence,
                json.dumps(reasoning or []),
                created_by, _now(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()
