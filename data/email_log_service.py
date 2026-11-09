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


# ---------------------------------------------------------------------------
# Governance helpers — mirrors export_log_service pattern
# ---------------------------------------------------------------------------

def _build_email_log_conditions(
    email_type: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    user_id: str | None,
) -> tuple[list[str], list]:
    """Build shared WHERE conditions for list and count queries."""
    conditions: list[str] = []
    params: list = []
    if email_type is not None:
        conditions.append("email_type = ?")
        params.append(email_type)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if date_from is not None:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to is not None:
        conditions.append("created_at <= ?")
        params.append(date_to)
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    return conditions, params


def list_all_email_logs(
    limit: int = 100,
    offset: int = 0,
    email_type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user_id: str | None = None,
) -> list[dict]:
    conditions, params = _build_email_log_conditions(
        email_type, status, date_from, date_to, user_id
    )
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT id, user_id, report_id, recipient_email, subject,
                   status, attempt_count, error_reason, sent_at, created_at, email_type
            FROM email_logs {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_all_email_logs(
    email_type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user_id: str | None = None,
) -> int:
    """Return total rows matching the given filters (no limit/offset)."""
    conditions, params = _build_email_log_conditions(
        email_type, status, date_from, date_to, user_id
    )
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM email_logs {where}", params
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def get_email_log_summary() -> dict:
    """Aggregate email activity across all users and types."""
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM email_logs").fetchone()[0]
        sent = conn.execute(
            "SELECT COUNT(*) FROM email_logs WHERE status = 'sent'"
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM email_logs WHERE status = 'failed'"
        ).fetchone()[0]
        simulated = conn.execute(
            "SELECT COUNT(*) FROM email_logs WHERE status = 'simulated'"
        ).fetchone()[0]
        success_rate = round(sent / total * 100, 1) if total > 0 else 0.0
        by_type_rows = conn.execute(
            "SELECT email_type, COUNT(*) AS cnt "
            "FROM email_logs "
            "GROUP BY email_type "
            "ORDER BY cnt DESC"
        ).fetchall()
        return {
            "total_emails": total,
            "sent_emails": sent,
            "failed_emails": failed,
            "simulated_emails": simulated,
            "success_rate": success_rate,
            "emails_by_type": {row["email_type"]: row["cnt"] for row in by_type_rows},
        }
    finally:
        conn.close()
