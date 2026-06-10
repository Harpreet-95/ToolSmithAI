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


def list_all_export_logs(
    limit: int = 100,
    offset: int = 0,
    export_format: str | None = None,
    status: str | None = None,
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if export_format is not None:
        conditions.append("export_format = ?")
        params.append(export_format)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
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
