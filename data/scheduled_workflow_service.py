import datetime
import logging
import threading

from data.db import get_connection

_scheduler_tick_lock = threading.Lock()

logger = logging.getLogger(__name__)

_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _compute_next_run_at(
    frequency: str,
    day_of_week: str | None = None,
    cron: str | None = None,
) -> str:
    """Return ISO-8601 UTC string for the next run time.

    When a cron expression is provided it takes precedence over frequency/day_of_week.
    Legacy daily/weekly/monthly paths are unchanged.
    """
    if cron:
        try:
            from core.engine.schedule_parser import compute_next_run_from_cron
            return compute_next_run_from_cron(cron).isoformat()
        except Exception as exc:
            logger.warning("cron next-run computation failed (%s); falling back to frequency", exc)

    now = datetime.datetime.now(datetime.timezone.utc)
    if frequency == "daily":
        delta = datetime.timedelta(days=1)
    elif frequency == "weekly":
        if day_of_week and day_of_week.lower() in _WEEKDAY_MAP:
            target = _WEEKDAY_MAP[day_of_week.lower()]
            days_ahead = (target - now.weekday()) % 7 or 7
            delta = datetime.timedelta(days=days_ahead)
        else:
            delta = datetime.timedelta(weeks=1)
    elif frequency == "monthly":
        delta = datetime.timedelta(days=30)
    else:
        delta = datetime.timedelta(days=1)
    return (now + delta).isoformat()


def create_scheduled_workflow(
    user_id: str,
    dataset_id: int | None,
    input_text: str,
    task_type: str,
    frequency: str,
    day_of_week: str | None,
    engine_tool_id: str | None = None,
    cron: str | None = None,
    human_label: str | None = None,
    refresh_before_run: bool = False,
) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    next_run_at = _compute_next_run_at(frequency, day_of_week, cron=cron)
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO scheduled_workflows
              (user_id, dataset_id, input_text, task_type, frequency,
               day_of_week, next_run_at, enabled, created_at, updated_at,
               engine_tool_id, cron, human_label, refresh_before_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, dataset_id, input_text, task_type, frequency,
             day_of_week, next_run_at, now, now, engine_tool_id,
             cron or None, human_label or None, 1 if refresh_before_run else 0),
        )
        conn.commit()
        row_id = cursor.lastrowid
    finally:
        conn.close()
    return get_scheduled_workflow_by_id(row_id)


def get_scheduled_workflow_by_id(workflow_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_scheduled_workflow_by_engine_tool_id(engine_tool_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_workflows WHERE engine_tool_id = ?",
            (engine_tool_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_scheduled_workflows(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM scheduled_workflows WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_due_scheduled_workflows(now_iso: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM scheduled_workflows WHERE enabled = 1 AND next_run_at <= ?",
            (now_iso,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_scheduled_workflow_run(workflow_id: int, next_run_at: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE scheduled_workflows SET next_run_at = ?, updated_at = ? WHERE id = ?",
            (next_run_at, now, workflow_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_scheduled_workflow_outcome(
    workflow_id: int,
    status: str,
    error: str | None,
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """UPDATE scheduled_workflows
               SET last_run_at = ?, last_status = ?, last_error = ?,
                   run_count = run_count + 1, updated_at = ?
               WHERE id = ?""",
            (now, status, error, now, workflow_id),
        )
        conn.commit()
    finally:
        conn.close()


def pause_scheduled_workflow(workflow_id: int, user_id: str) -> dict | None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE scheduled_workflows SET enabled = 0, updated_at = ? WHERE id = ? AND user_id = ?",
            (now, workflow_id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_scheduled_workflow_by_id(workflow_id)


def resume_scheduled_workflow(workflow_id: int, user_id: str) -> dict | None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE scheduled_workflows SET enabled = 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (now, workflow_id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_scheduled_workflow_by_id(workflow_id)


def delete_scheduled_workflow(workflow_id: int, user_id: str) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM scheduled_workflows WHERE id = ? AND user_id = ?",
            (workflow_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


_WINDOW_SECONDS: dict[str, int] = {
    "daily":   86_400,       # 1 day
    "weekly":  604_800,      # 7 days
    "monthly": 2_592_000,    # 30 days
}


def _parse_iso_utc(s: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _fmt_overdue(seconds: float) -> str:
    if seconds < 3600:
        m = max(1, int(seconds // 60))
        return f"{m}m overdue"
    if seconds < 86_400:
        h = round(seconds / 3600, 1)
        return f"{h}h overdue"
    d = round(seconds / 86_400, 1)
    return f"{d}d overdue"


def get_schedule_health(user_id: str) -> list:
    """
    Return deterministic health analysis for every scheduled workflow owned by user_id.
    Classification uses the execution window derived from the workflow's frequency.
    """
    rows = list_scheduled_workflows(user_id)
    now  = datetime.datetime.now(datetime.timezone.utc)
    results = []

    for sw in rows:
        enabled   = bool(sw["enabled"])
        frequency = sw.get("frequency") or "daily"
        next_run_str = sw.get("next_run_at")
        last_run_str = sw.get("last_run_at")
        last_status  = sw.get("last_status")
        window       = _WINDOW_SECONDS.get(frequency, 86_400)

        if not enabled:
            health          = "Paused"
            overdue_seconds = None
            overdue_label   = None
            recommendation  = "This schedule is paused and will not run automatically."
        elif not next_run_str:
            health          = "Healthy"
            overdue_seconds = None
            overdue_label   = None
            recommendation  = "Schedule appears healthy."
        else:
            try:
                next_run = _parse_iso_utc(next_run_str)
                overdue_seconds = max(0.0, (now - next_run).total_seconds())
            except Exception:
                overdue_seconds = 0.0

            if overdue_seconds == 0:
                health         = "Healthy"
                overdue_label  = None
                recommendation = "Schedule appears healthy."
            elif overdue_seconds < window * 2:
                health         = "Delayed"
                overdue_label  = _fmt_overdue(overdue_seconds)
                recommendation = "This workflow is slightly overdue. It should run on the next scheduler tick."
            else:
                health         = "Missed"
                overdue_label  = _fmt_overdue(overdue_seconds)
                recommendation = "This workflow has not run recently and may be stalled."

        # Last successful run (only when last run was actually successful)
        last_success = (
            last_run_str
            if last_run_str and last_status in ("completed", "success")
            else None
        )

        results.append({
            "id":              sw["id"],
            "input_text":      sw["input_text"],
            "frequency":       frequency,
            "day_of_week":     sw.get("day_of_week"),
            "cron":            sw.get("cron"),
            "human_label":     sw.get("human_label"),
            "enabled":         enabled,
            "health":          health,
            "next_run_at":     next_run_str,
            "last_run_at":     last_run_str,
            "last_success":    last_success,
            "last_status":     last_status,
            "overdue_seconds": overdue_seconds,
            "overdue_label":   overdue_label,
            "recommendation":  recommendation,
        })

    # Severity order: Missed → Delayed → Healthy → Paused
    _order = {"Missed": 0, "Delayed": 1, "Healthy": 2, "Paused": 3}
    results.sort(key=lambda x: _order[x["health"]])
    return results


def _classify_result(result: dict) -> tuple[str, str | None]:
    """
    Inspect a handle_input() return value and classify the business outcome.

    Returns (status, message) where status is:
    - "completed" : operation succeeded with no business-level failure
    - "warning"   : code ran without exception but a business condition was not met
    Python-level exceptions are handled by the caller and classified as "failed".
    """
    data = result.get("data", {}) if isinstance(result, dict) else {}

    dataset_error = data.get("dataset_report_error")
    if dataset_error:
        return "warning", str(dataset_error)

    email_delivery = data.get("email_delivery")
    if isinstance(email_delivery, dict) and email_delivery.get("sent") is False:
        reason = email_delivery.get("reason") or "Email was not sent."
        return "warning", str(reason)

    return "completed", None


def run_due_workflows() -> None:
    """APScheduler job — executes all due scheduled workflows."""
    if not _scheduler_tick_lock.acquire(blocking=False):
        logger.warning(
            "run_due_workflows: skipping tick — previous scheduler tick is still running"
        )
        return

    try:
        _run_due_workflows_inner()
    finally:
        _scheduler_tick_lock.release()


def _run_due_workflows_inner() -> None:
    import sqlite3 as _sqlite3
    from core.input.input_handler import handle_input

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        due = get_due_scheduled_workflows(now_iso)
    except _sqlite3.OperationalError as exc:
        logger.warning("run_due_workflows: skipping tick — database is locked (%s)", exc)
        return
    for wf in due:
        wid     = wf["id"]
        user_id = wf["user_id"]
        next_run = _compute_next_run_at(wf["frequency"], wf.get("day_of_week"), cron=wf.get("cron"))
        # Advance next_run_at BEFORE executing to prevent duplicate runs
        mark_scheduled_workflow_run(wid, next_run)

        run_id = None
        try:
            from data.scheduler_run_service import create_schedule_run
            run_id = create_schedule_run(wid, user_id, trigger_type="scheduled")
        except Exception:
            pass

        # ── Reprofile guard — isolated try/except, degrade-safe ───────────────
        reprofile_status = None
        reprofile_duration_ms = None
        if (
            wf.get("refresh_before_run")
            and wf.get("dataset_id")
            and not wf.get("engine_tool_id")
        ):
            _rp_start = datetime.datetime.now(datetime.timezone.utc)
            try:
                from data.dataset_service import reprofile_dataset
                refreshed = reprofile_dataset(wf["dataset_id"], user_id)
                reprofile_duration_ms = int(
                    (datetime.datetime.now(datetime.timezone.utc) - _rp_start).total_seconds() * 1000
                )
                reprofile_status = "skipped" if refreshed is None else "succeeded"
            except Exception as rp_exc:
                reprofile_duration_ms = int(
                    (datetime.datetime.now(datetime.timezone.utc) - _rp_start).total_seconds() * 1000
                )
                reprofile_status = "failed_degraded"
                logger.warning("Scheduled workflow %s reprofile failed (degraded): %s", wid, rp_exc)
                try:
                    from data.notification_service import create_notification
                    create_notification(
                        user_id=user_id,
                        title="Dataset refresh failed — using cached intelligence",
                        message=f"'{wf['input_text'][:80]}': reprofile error: {str(rp_exc)[:150]}",
                        type="schedule",
                        status="warn",
                    )
                except Exception:
                    pass

        try:
            if wf.get("engine_tool_id"):
                from data.engine.tool_store import get_tool
                from core.engine.runtime import execute_tool
                from core.engine.contracts import RunStatus
                _tool_def = get_tool(wf["engine_tool_id"])
                if _tool_def is None:
                    raise Exception(f"Engine tool '{wf['engine_tool_id']}' not found")
                _record = execute_tool(_tool_def, {}, user_id=user_id)
                if _record.status == RunStatus.COMPLETED:
                    status, warn_msg = "completed", None
                else:
                    status = "warning"
                    warn_msg = f"Engine tool ended with status: {_record.status.value}"
                result = {"data": {}}
            else:
                result = handle_input(
                    wf["input_text"],
                    user_id=user_id,
                    dataset_id=wf.get("dataset_id"),
                )
                status, warn_msg = _classify_result(result)

            update_scheduled_workflow_outcome(wid, status=status, error=warn_msg)
            logger.info("Scheduled workflow %s %s. Next run: %s", wid, status, next_run)

            related_report_id = None
            try:
                data = result.get("data", {}) if isinstance(result, dict) else {}
                related_report_id = data.get("report_id")
            except Exception:
                pass

            if run_id is not None:
                try:
                    from data.scheduler_run_service import complete_schedule_run
                    complete_schedule_run(
                        run_id,
                        related_report_id=related_report_id,
                        reprofile_status=reprofile_status,
                        reprofile_duration_ms=reprofile_duration_ms,
                    )
                except Exception:
                    pass

            try:
                from data.notification_service import create_notification
                if warn_msg:
                    create_notification(
                        user_id=user_id,
                        title="Scheduled run warning",
                        message=f"'{wf['input_text'][:80]}': {warn_msg[:150]}",
                        type="schedule",
                        status="warn",
                    )
                else:
                    create_notification(
                        user_id=user_id,
                        title="Scheduled run completed",
                        message=f"'{wf['input_text'][:100]}' completed successfully.",
                        type="schedule",
                        status="success",
                    )
            except Exception:
                pass

        except Exception as exc:
            err_msg = str(exc)[:500]
            update_scheduled_workflow_outcome(wid, status="failed", error=err_msg)
            logger.error("Scheduled workflow %s failed: %s", wid, exc)

            if run_id is not None:
                try:
                    from data.scheduler_run_service import fail_schedule_run
                    fail_schedule_run(
                        run_id,
                        error_message=err_msg,
                        reprofile_status=reprofile_status,
                        reprofile_duration_ms=reprofile_duration_ms,
                    )
                except Exception:
                    pass

            try:
                from data.notification_service import create_notification
                create_notification(
                    user_id=user_id,
                    title="Scheduled run failed",
                    message=f"'{wf['input_text'][:80]}' failed: {err_msg[:150]}",
                    type="schedule",
                    status="error",
                )
            except Exception:
                pass
