from data.db import get_connection


def _row_to_dict(row) -> dict:
    return {
        "id":           row["id"],
        "tenant_id":    row["tenant_id"],
        "user_id":      row["user_id"],
        "event_type":   row["event_type"],
        "source":       row["source"],
        "reference_id": row["reference_id"],
        "created_at":   row["created_at"],
    }


def log_usage_event(
    user_id: str,
    event_type: str,
    source: str,
    reference_id: str | None = None,
) -> dict:
    """Insert a usage event and return the persisted record as a dict."""
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO usage_events (tenant_id, user_id, event_type, source, reference_id)
        VALUES ('', ?, ?, ?, ?)
        """,
        (user_id, event_type, source, reference_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, tenant_id, user_id, event_type, source, reference_id, created_at FROM usage_events WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_usage_events(user_id: str, limit: int = 100) -> list[dict]:
    """Return up to limit usage events for a user, most recent first."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, tenant_id, user_id, event_type, source, reference_id, created_at
        FROM usage_events
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [_row_to_dict(row) for row in rows]


def count_usage_events(user_id: str, event_type: str | None = None) -> int:
    """Return the total number of usage events for a user, optionally filtered by event_type."""
    conn = get_connection()
    if event_type is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE user_id = ? AND event_type = ?",
            (user_id, event_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM usage_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    conn.close()
    return row[0]


def count_usage_events_since(user_id: str, event_type: str, since: str) -> int:
    """Return usage event count for a user and event_type on or after the given since timestamp.

    since must be an ISO 8601 / YYYY-MM-DD HH:MM:SS string matching the format
    stored in created_at. Used for billing-period quota checks.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) FROM usage_events WHERE user_id = ? AND event_type = ? AND created_at >= ?",
        (user_id, event_type, since),
    ).fetchone()
    conn.close()
    return row[0]
