from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_email_log(
    recipient_email: str,
    subject: str,
    email_type: str = "report",
    user_id: str | None = None,
    report_id: int | None = None,
) -> int:
    """Insert a pending email_log row. Returns the new row id."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO email_logs
              (user_id, report_id, recipient_email, subject, status,
               attempt_count, error_reason, sent_at, created_at, email_type)
            VALUES (?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?)
            """,
            (user_id, report_id, recipient_email, subject, _now(), email_type),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_email_log_status(
    log_id: int,
    status: str,
    attempt_count: int,
    error_reason: str | None = None,
    sent_at: str | None = None,
) -> None:
    """Update status, attempt_count, error_reason, and sent_at for a log row."""
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE email_logs
            SET status = ?, attempt_count = ?, error_reason = ?, sent_at = ?
            WHERE id = ?
            """,
            (status, attempt_count, error_reason, sent_at, log_id),
        )
        conn.commit()
    finally:
        conn.close()
