from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_notification(
    user_id: str,
    title: str,
    message: str,
    type: str = "info",
    status: str = "info",
    related_report_id: int | None = None,
    related_execution_id: int | None = None,
) -> int:
    """Persist a notification for user_id. Returns the new notification id."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO notifications
              (user_id, type, title, message, status, read,
               related_report_id, related_execution_id, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                user_id,
                type,
                title,
                message,
                status,
                related_report_id,
                related_execution_id,
                _now(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_notifications_for_user(user_id: str, limit: int = 50) -> list[dict]:
    """Return notifications for user_id, newest first, up to limit rows."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, type, title, message, status, read,
                   related_report_id, related_execution_id, created_at
            FROM notifications
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_notification_read(notification_id: int, user_id: str) -> bool:
    """Mark a notification as read. Returns True if the row was found and owned by user_id."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE notifications SET read = 1 WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_notification(notification_id: int, user_id: str) -> bool:
    """Delete a notification owned by user_id. Returns True if deleted."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM notifications WHERE id = ? AND user_id = ?",
            (notification_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
