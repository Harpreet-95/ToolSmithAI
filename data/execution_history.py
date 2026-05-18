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
    user_id: str | None = None,
    dataset_id: int | None = None,
) -> None:
    step_results = result.get("step_results") or []
    failed_step = _find_failed_step(step_results)

    record = {
        "plan_id":        result.get("plan_id"),
        "workflow_id":    workflow_id,
        "dataset_id":     dataset_id,
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
        "user_id":        user_id,
    }

    try:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO execution_history (
                plan_id, workflow_id, dataset_id, trigger_source,
                task_type, intent, status,
                started_at, finished_at, duration_ms,
                step_count, failed_step_id, failed_tool, error_message,
                user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["plan_id"],
                record["workflow_id"],
                record["dataset_id"],
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
                record["user_id"],
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to write execution history: %s", e)


_FILLER_WORDS = frozenset({
    "me", "please", "the", "a", "an", "my", "i", "just",
    "for", "from", "some", "all", "any", "this", "that", "it",
})

_SYNONYM_MAP: dict[str, str] = {
    "send":       "email",
    "mail":       "email",
    "analyse":    "analyze",
    "analysis":   "analyze",
    "summary":    "summarize",
    "summarise":  "summarize",
    "generating": "generate",
    "generated":  "generate",
    "generation": "generate",
}

# Ordered longest-first so longer phrases match before sub-phrases do.
_PHRASE_MAP = (
    ("report generation", "report"),
)


def _normalize_intent(text: str) -> str:
    """Return a canonical form of an intent string for grouping similar prompts."""
    if not text:
        return ""
    s = text.lower().strip()
    for phrase, replacement in _PHRASE_MAP:
        s = s.replace(phrase, replacement)
    tokens = []
    for raw in s.split():
        word = raw.strip(".,!?;:")
        if not word or word in _FILLER_WORDS:
            continue
        tokens.append(_SYNONYM_MAP.get(word, word))
    return " ".join(tokens)


def get_repeated_intent_suggestions(user_id: str) -> list:
    try:
        conn = get_connection()
        # Fetch every unique exact-phrasing and its run count.
        # No HAVING filter here — single-run variants must be able to combine
        # with other single-run variants after normalization.
        rows = conn.execute(
            """
            SELECT intent, COUNT(*) AS count
            FROM execution_history
            WHERE trigger_source = 'interpreter'
              AND status = 'completed'
              AND intent IS NOT NULL
              AND user_id = ?
            GROUP BY intent
            ORDER BY count DESC
            """,
            (user_id,),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("Failed to query repeated intents: %s", e)
        return []

    # Group by normalized key.  Rows are already ordered by count DESC, so the
    # first time we encounter a normalized key it carries the most-seen original
    # phrasing — that becomes the display intent for the whole group.
    groups: dict[str, dict] = {}
    for row in rows:
        key = _normalize_intent(row["intent"])
        if not key:
            continue
        if key not in groups:
            groups[key] = {"intent": row["intent"], "count": row["count"]}
        else:
            groups[key]["count"] += row["count"]

    return [
        {
            "intent": g["intent"],
            "count":  g["count"],
            "suggestion": "You've done this multiple times. Consider creating a workflow.",
        }
        for g in sorted(groups.values(), key=lambda x: x["count"], reverse=True)
        if g["count"] >= 2
    ]


def get_workflow_success_insights(user_id: str) -> list:
    """
    Return health insights for all workflows run by this user.
    Workflows with fewer than MIN_RUNS executions are omitted to avoid
    noise for new users with limited history.
    """
    MIN_RUNS = 3
    try:
        conn = get_connection()
        # Fetch per-run rows with workflow name from a LEFT JOIN.
        # Ordered DESC per workflow so most-recent runs come first.
        rows = conn.execute(
            """
            SELECT eh.workflow_id, eh.status, eh.started_at, eh.duration_ms,
                   w.name AS workflow_name
            FROM execution_history eh
            LEFT JOIN workflows w ON w.id = eh.workflow_id
            WHERE eh.workflow_id IS NOT NULL
              AND eh.user_id = ?
            ORDER BY eh.workflow_id ASC, eh.started_at DESC
            """,
            (user_id,),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.error("Failed to query workflow success insights: %s", e)
        return []

    # Group individual runs by workflow_id (rows already DESC within each group)
    groups: dict[int, list] = {}
    for row in rows:
        wid = row["workflow_id"]
        if wid not in groups:
            groups[wid] = []
        groups[wid].append(dict(row))

    results = []
    for workflow_id, runs in groups.items():
        total = len(runs)
        if total < MIN_RUNS:
            continue

        success_count = sum(
            1 for r in runs if r["status"] in ("completed", "success")
        )
        success_rate  = round(success_count / total, 2)
        failure_rate  = round(1.0 - success_rate, 2)

        valid_durations = [r["duration_ms"] for r in runs if r["duration_ms"] is not None]
        avg_duration_ms = (
            round(sum(valid_durations) / len(valid_durations))
            if valid_durations else None
        )

        last_run      = runs[0]["started_at"]   # most recent (DESC order)
        workflow_name = runs[0].get("workflow_name") or f"Workflow #{workflow_id}"

        # Count consecutive failures starting from the most recent run
        consecutive_failures = 0
        for r in runs:
            if r["status"] in ("completed", "success"):
                break
            consecutive_failures += 1

        # Health classification
        if consecutive_failures >= 2 or success_rate < 0.60:
            health = "Critical"
        elif success_rate < 0.90 or consecutive_failures == 1:
            health = "Warning"
        else:
            health = "Healthy"

        # Operational recommendation
        if health == "Critical":
            if consecutive_failures >= 2:
                recommendation = (
                    f"This workflow has failed {consecutive_failures} times consecutively"
                    " and needs immediate review."
                )
            else:
                recommendation = "This workflow may need review due to repeated failures."
        elif health == "Warning":
            if consecutive_failures == 1:
                recommendation = "Last run failed. Monitor closely for further failures."
            else:
                recommendation = "This workflow is generally reliable but shows occasional failures."
        else:
            recommendation = "Workflow appears stable."

        results.append({
            "workflow_id":          workflow_id,
            "workflow_name":        workflow_name,
            "total_runs":           total,
            "success_runs":         success_count,
            "success_rate":         success_rate,
            "failure_rate":         failure_rate,
            "avg_duration_ms":      avg_duration_ms,
            "consecutive_failures": consecutive_failures,
            "last_run":             last_run,
            "health":               health,
            "recommendation":       recommendation,
            "insight":              recommendation,   # backwards-compatible alias
        })

    # Sort: Critical → Warning → Healthy; within the same band, lowest success rate first
    _order = {"Critical": 0, "Warning": 1, "Healthy": 2}
    results.sort(key=lambda x: (_order[x["health"]], x["success_rate"]))
    return results


_SOURCE_LABELS: dict[str, str] = {
    "interpreter":  "Manual",
    "workflow_api": "Workflow",
    "scheduler":    "Scheduler",
    "composer":     "Composer",
}

_STATUS_LABELS: dict[str, str] = {
    "completed": "Completed",
    "success":   "Completed",
    "failed":    "Failed",
    "running":   "Running",
}

_TASK_SUMMARIES: dict[str, str] = {
    "generate_dataset_report": "Dataset report generated successfully",
    "email_dataset_report":    "Dataset report emailed successfully",
    "send_email":              "Email sent successfully",
    "generate_report":         "Report generated successfully",
    "set_reminder":            "Reminder scheduled successfully",
}


def _format_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    return f"{m}m {int(s % 60)}s"


def _build_summary(row: dict) -> str:
    status    = row.get("status", "")
    task_type = row.get("task_type") or ""
    intent    = row.get("intent") or ""
    error     = row.get("error_message") or ""

    if status == "failed":
        if error:
            short = error[:80] + ("…" if len(error) > 80 else "")
            return f"Execution failed: {short}"
        return "Execution failed"

    if task_type in _TASK_SUMMARIES:
        return _TASK_SUMMARIES[task_type]

    if intent:
        return intent if len(intent) <= 60 else intent[:60] + "…"

    return "Workflow completed"


def enrich_execution_record(row: dict) -> dict:
    """Add display-ready fields to a raw execution_history row dict."""
    out = dict(row)
    out["duration_label"] = _format_duration(row.get("duration_ms"))
    out["source_label"]   = _SOURCE_LABELS.get(row.get("trigger_source", ""), row.get("trigger_source") or "")
    out["status_label"]   = _STATUS_LABELS.get(row.get("status", ""), (row.get("status") or "").capitalize())
    out["summary"]        = _build_summary(row)
    return out


def get_execution_by_id(execution_id: int, user_id: str) -> dict | None:
    """Fetch a single execution_history row owned by user_id. Returns None if not found."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM execution_history WHERE id = ? AND user_id = ?",
            (execution_id, user_id),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error("Failed to fetch execution by id: %s", e)
        return None


def purge_old_execution_history(cutoff: str) -> int:
    """Delete execution_history rows whose started_at is before cutoff.

    cutoff must be an ISO 8601 string (e.g. '2026-02-03T14:00:00+00:00').
    Returns count of deleted rows.
    """
    try:
        conn = get_connection()
        cur = conn.execute(
            "DELETE FROM execution_history WHERE started_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception as e:
        logger.error("Failed to purge old execution_history rows: %s", e)
        return 0
