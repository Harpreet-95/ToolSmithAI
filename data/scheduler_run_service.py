from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _calc_duration_ms(started_at: str, finished_at: str) -> int | None:
    try:
        s = datetime.fromisoformat(started_at)
        f = datetime.fromisoformat(finished_at)
        return max(0, int((f - s).total_seconds() * 1000))
    except Exception:
        return None


def create_schedule_run(
    schedule_id: int,
    user_id: str,
    trigger_type: str = "scheduled",
) -> int:
    """Insert a new run row with status='running'. Returns the new run id."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO scheduled_workflow_runs
              (schedule_id, user_id, status, started_at, trigger_type)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (schedule_id, user_id, _now(), trigger_type),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def complete_schedule_run(
    run_id: int,
    related_execution_id: int | None = None,
    related_report_id: int | None = None,
    reprofile_status: str | None = None,
    reprofile_duration_ms: int | None = None,
) -> None:
    """Mark a run as completed, record finish time and duration."""
    finished_at = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT started_at FROM scheduled_workflow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        duration_ms = _calc_duration_ms(row["started_at"], finished_at) if row else None
        conn.execute(
            """UPDATE scheduled_workflow_runs
               SET status = 'completed', finished_at = ?, duration_ms = ?,
                   related_execution_id = ?, related_report_id = ?,
                   reprofile_status = ?, reprofile_duration_ms = ?
               WHERE id = ?""",
            (finished_at, duration_ms, related_execution_id, related_report_id,
             reprofile_status, reprofile_duration_ms, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_schedule_run(
    run_id: int,
    error_message: str | None = None,
    related_execution_id: int | None = None,
    reprofile_status: str | None = None,
    reprofile_duration_ms: int | None = None,
) -> None:
    """Mark a run as failed, record finish time, duration, and error."""
    finished_at = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT started_at FROM scheduled_workflow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        duration_ms = _calc_duration_ms(row["started_at"], finished_at) if row else None
        conn.execute(
            """UPDATE scheduled_workflow_runs
               SET status = 'failed', finished_at = ?, duration_ms = ?,
                   error_message = ?, related_execution_id = ?,
                   reprofile_status = ?, reprofile_duration_ms = ?
               WHERE id = ?""",
            (finished_at, duration_ms, (error_message or "")[:500], related_execution_id,
             reprofile_status, reprofile_duration_ms, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_runs_for_schedule(
    schedule_id: int,
    user_id: str,
    limit: int = 25,
) -> list[dict]:
    """Return runs for one schedule, newest first, ownership enforced via user_id."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, schedule_id, user_id, status, started_at, finished_at,
                   duration_ms, trigger_type, error_message,
                   related_execution_id, related_report_id,
                   reprofile_status, reprofile_duration_ms
            FROM scheduled_workflow_runs
            WHERE schedule_id = ? AND user_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (schedule_id, user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_recent_runs_for_user(user_id: str, limit: int = 50) -> list[dict]:
    """Return recent runs across all schedules for a user, newest first.

    Joins scheduled_workflows to include schedule_input_text and schedule_frequency
    for display in the activity tab without a second query per row.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.schedule_id, r.user_id, r.status,
                   r.started_at, r.finished_at, r.duration_ms,
                   r.trigger_type, r.error_message,
                   r.related_execution_id, r.related_report_id,
                   r.reprofile_status, r.reprofile_duration_ms,
                   sw.input_text  AS schedule_input_text,
                   sw.frequency   AS schedule_frequency
            FROM scheduled_workflow_runs r
            LEFT JOIN scheduled_workflows sw ON sw.id = r.schedule_id
            WHERE r.user_id = ?
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
