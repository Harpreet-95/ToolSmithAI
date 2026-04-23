import logging
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _duration_ms(started_at: str, finished_at: str) -> int | None:
    try:
        delta = _parse_dt(finished_at) - _parse_dt(started_at)
        return int(delta.total_seconds() * 1000)
    except Exception:
        return None


def _find_failed_step(step_results: list) -> dict | None:
    for step in step_results:
        if step.get("status") == "failed":
            return step
    return None


def log_execution_history(
    plan: dict,
    result: dict,
    workflow_id: int | None = None,
    trigger_source: str = "interpreter",
) -> None:
    step_results = result.get("step_results") or []
    failed_step = _find_failed_step(step_results)

    record = {
        "plan_id":        result.get("plan_id"),
        "workflow_id":    workflow_id,
        "trigger_source": trigger_source,
        "task_type":      plan.get("task_type"),
        "intent":         plan.get("intent"),
        "status":         result.get("status") or "unknown",
        "started_at":     result.get("started_at"),
        "finished_at":    result.get("finished_at"),
        "duration_ms":    _duration_ms(result.get("started_at"), result.get("finished_at")),
        "step_count":     len(plan.get("steps") or []),
        "failed_step_id": failed_step.get("step_id") if failed_step else None,
        "failed_tool":    failed_step.get("tool") if failed_step else None,
        "error_message":  result.get("error"),
    }

    try:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO execution_history (
                plan_id, workflow_id, trigger_source,
                task_type, intent, status,
                started_at, finished_at, duration_ms,
                step_count, failed_step_id, failed_tool, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["plan_id"],
                record["workflow_id"],
                record["trigger_source"],
                record["task_type"],
                record["intent"],
                record["status"],
                record["started_at"],
                record["finished_at"],
                record["duration_ms"],
                record["step_count"],
                record["failed_step_id"],
                record["failed_tool"],
                record["error_message"],
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to write execution history: %s", e)


def get_repeated_intent_suggestions() -> list:
    try:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT intent, COUNT(*) AS count
            FROM execution_history
            WHERE trigger_source = 'interpreter'
              AND status = 'completed'
              AND intent IS NOT NULL
            GROUP BY intent
            HAVING COUNT(*) >= 2
            ORDER BY count DESC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("Failed to query repeated intents: %s", e)
        return []

    return [
        {
            "intent": row["intent"],
            "count": row["count"],
            "suggestion": "You've done this multiple times. Consider creating a workflow.",
        }
        for row in rows
    ]


def get_workflow_success_insights() -> list:
    try:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT
                workflow_id,
                COUNT(*) AS total_runs,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS success_runs
            FROM execution_history
            WHERE trigger_source = 'workflow_api'
              AND workflow_id IS NOT NULL
            GROUP BY workflow_id
            ORDER BY workflow_id ASC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("Failed to query workflow success insights: %s", e)
        return []

    results = []
    for row in rows:
        total = row["total_runs"]
        success = row["success_runs"]
        rate = round(success / total, 2) if total > 0 else 0.0

        if rate == 1.0:
            insight = "This workflow is highly reliable"
        elif rate >= 0.7:
            insight = "This workflow is generally reliable"
        else:
            insight = "This workflow needs improvement"

        results.append({
            "workflow_id": row["workflow_id"],
            "total_runs": total,
            "success_runs": success,
            "success_rate": rate,
            "insight": insight,
        })

    return results
