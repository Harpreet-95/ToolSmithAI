from datetime import datetime, timezone

from data.db import get_connection


def create_export_log(
    *,
    user_id: str,
    report_id: int | None,
    export_format: str,
    filename: str | None = None,
    file_size_bytes: int | None = None,
    status: str = "success",
    error_reason: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    exported_at = now if status != "failed" else None
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO export_logs
                (user_id, report_id, export_format, filename, file_size_bytes,
                 status, error_reason, ip_address, user_agent, exported_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                report_id,
                export_format,
                filename,
                file_size_bytes,
                status,
                error_reason,
                ip_address,
                user_agent,
                exported_at,
                now,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_export_logs_for_user(user_id: str, limit: int = 100) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, report_id, export_format, filename, file_size_bytes,
                   status, error_reason, ip_address, user_agent, exported_at, created_at
            FROM export_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _build_export_log_conditions(
    export_format: str | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    user_id: str | None,
) -> tuple[list[str], list]:
    """Build shared WHERE conditions for list and count queries."""
    conditions: list[str] = []
    params: list = []
    if export_format is not None:
        conditions.append("export_format = ?")
        params.append(export_format)
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


def list_all_export_logs(
    limit: int = 100,
    offset: int = 0,
    export_format: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user_id: str | None = None,
) -> list[dict]:
    conditions, params = _build_export_log_conditions(
        export_format, status, date_from, date_to, user_id
    )
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT id, user_id, report_id, export_format, filename, file_size_bytes,
                   status, error_reason, ip_address, user_agent, exported_at, created_at
            FROM export_logs {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def count_all_export_logs(
    export_format: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user_id: str | None = None,
) -> int:
    """Return total rows matching the given filters (no limit/offset)."""
    conditions, params = _build_export_log_conditions(
        export_format, status, date_from, date_to, user_id
    )
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM export_logs {where}", params
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def get_export_log_summary() -> dict:
    """Aggregate export activity across all users and formats."""
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM export_logs").fetchone()[0]
        successful = conn.execute(
            "SELECT COUNT(*) FROM export_logs WHERE status = 'success'"
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM export_logs WHERE status = 'failed'"
        ).fetchone()[0]
        success_rate = round(successful / total * 100, 1) if total > 0 else 0.0
        by_format_rows = conn.execute(
            "SELECT export_format, COUNT(*) AS cnt "
            "FROM export_logs "
            "GROUP BY export_format "
            "ORDER BY cnt DESC"
        ).fetchall()
        return {
            "total_exports": total,
            "successful_exports": successful,
            "failed_exports": failed,
            "success_rate": success_rate,
            "exports_by_format": {row["export_format"]: row["cnt"] for row in by_format_rows},
        }
    finally:
        conn.close()
