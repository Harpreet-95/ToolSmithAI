import json
from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_summary(content: dict) -> str:
    """Return a short plain-text summary for the Reports list view.

    Priority:
      1. executive_summary.summary  — rich, business-readable single paragraph
      2. Overview text items        — factual dataset description
      3. first text section items   — any text section as last resort

    Type-safe: sections without 'type' (v1 reports) default to 'text'.
    Non-text sections (kpi, chart, etc.) are skipped when falling back.
    Capped at 200 characters to stay lightweight as a display hint.
    """
    sections = content.get("sections", [])

    # 1. Prefer the executive_summary paragraph — most business-readable
    for s in sections:
        if s.get("type") == "executive_summary":
            summary = s.get("summary", "")
            if summary:
                return str(summary)[:200]

    # 2. Fall back to Overview text items, then first text section
    text_sections = [s for s in sections if s.get("type", "text") == "text"]
    overview = next((s for s in text_sections if s.get("heading") == "Overview"), None)
    source = overview or (text_sections[0] if text_sections else None)
    if source is None:
        return ""
    items = source.get("items", [])[:2]
    text = " · ".join(str(i) for i in items)
    return text[:200]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_report(
    user_id: str,
    title: str,
    task_type: str,
    content: dict,
    status: str = "completed",
    dataset_id: int | None = None,
    exec_id: int | None = None,
    workflow_id: int | None = None,
    schedule_id: int | None = None,
    expires_at: str | None = None,
) -> int:
    """Persist a generated report. Returns the new report id."""
    now = _now()
    summary = _extract_summary(content)
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO reports
              (user_id, title, task_type, status,
               dataset_id, exec_id, workflow_id, schedule_id,
               content_json, summary_text,
               created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                task_type,
                status,
                dataset_id,
                exec_id,
                workflow_id,
                schedule_id,
                json.dumps(content),
                summary,
                now,
                now,
                expires_at,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def list_reports_for_user(user_id: str) -> list[dict]:
    """Return lightweight report rows for the list view — no content_json.

    Joins datasets to include the source filename where the dataset still exists.
    Ordered newest-first.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                r.id,
                r.title,
                r.task_type,
                r.status,
                r.dataset_id,
                d.filename AS dataset_filename,
                r.summary_text,
                r.created_at,
                r.expires_at
            FROM reports r
            LEFT JOIN datasets d ON d.id = r.dataset_id
            WHERE r.user_id = ?
            ORDER BY r.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_report_by_id(report_id: int, user_id: str) -> dict | None:
    """Return a single report including full content_json, decoded back to a dict.

    Returns None if the report does not exist or belongs to a different user.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                r.id,
                r.title,
                r.task_type,
                r.status,
                r.dataset_id,
                d.filename AS dataset_filename,
                r.exec_id,
                r.workflow_id,
                r.schedule_id,
                r.content_json,
                r.summary_text,
                r.created_at,
                r.updated_at,
                r.expires_at,
                r.share_token
            FROM reports r
            LEFT JOIN datasets d ON d.id = r.dataset_id
            WHERE r.id = ? AND r.user_id = ?
            """,
            (report_id, user_id),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["content"] = json.loads(record.pop("content_json"))
        return record
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_report(report_id: int, user_id: str) -> bool:
    """Delete a report owned by user_id. Returns True if deleted, False if not found or not owned."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM reports WHERE id = ? AND user_id = ?",
            (report_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
