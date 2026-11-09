import json
from datetime import datetime, timezone

from data.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_workspace_draft(
    user_id: str,
    intent_text: str,
    dataset_id: int | None = None,
) -> int:
    """Insert an ai_workspaces row with status='draft'. Returns the new id."""
    now = _now()
    title = intent_text.strip()[:80] if intent_text and intent_text.strip() else "Untitled Workspace"
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO ai_workspaces
              (user_id, title, status, intent_text, dataset_id, created_at, updated_at)
            VALUES (?, ?, 'draft', ?, ?, ?, ?)
            """,
            (user_id, title, intent_text, dataset_id, now, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def attach_workspace_proposal(
    workspace_id: int,
    user_id: str,
    proposal: dict,
    proposal_source: str,
) -> dict | None:
    """Attach a proposal blob to a workspace and advance status to 'proposed'.

    Returns the updated workspace dict, or None if not found / not owned.
    """
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE ai_workspaces
               SET proposal_json   = ?,
                   proposal_source = ?,
                   proposed_at     = ?,
                   status          = 'proposed',
                   updated_at      = ?
             WHERE id = ? AND user_id = ?
            """,
            (json.dumps(proposal), proposal_source, now, now, workspace_id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_workspace_by_id(workspace_id, user_id)


def attach_workspace_execution(
    workspace_id: int,
    user_id: str,
    execution_summary: dict,
    report_id: int | None = None,
    selected_sections: list | None = None,
) -> dict | None:
    """Record execution result on a workspace and advance status to 'executed'.

    execution_summary: lightweight dict summarising the run outcome.
    Returns the updated workspace dict, or None if not found / not owned.
    """
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE ai_workspaces
               SET status                  = 'executed',
                   execution_summary_json  = ?,
                   selected_sections_json  = ?,
                   report_id               = ?,
                   executed_at             = ?,
                   updated_at              = ?
             WHERE id = ? AND user_id = ?
            """,
            (
                json.dumps(execution_summary),
                json.dumps(selected_sections) if selected_sections is not None else None,
                report_id,
                now,
                now,
                workspace_id,
                user_id,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_workspace_by_id(workspace_id, user_id)


def save_workspace_db(workspace_id: int, user_id: str) -> dict | None:
    """Advance status to 'saved'. Returns updated workspace or None if not owned."""
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE ai_workspaces
               SET status     = 'saved',
                   saved_at   = ?,
                   updated_at = ?
             WHERE id = ? AND user_id = ?
            """,
            (now, now, workspace_id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_workspace_by_id(workspace_id, user_id)


def list_workspaces_for_user(user_id: str) -> list[dict]:
    """Return lightweight workspace rows (no proposal_json) for list views.

    Joins datasets and reports to include filenames. Ordered newest-first.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                w.id,
                w.title,
                w.status,
                w.intent_text,
                w.dataset_id,
                d.filename   AS dataset_filename,
                w.report_id,
                w.workflow_id,
                w.proposal_source,
                w.proposed_at,
                w.executed_at,
                w.saved_at,
                w.created_at,
                w.updated_at
            FROM ai_workspaces w
            LEFT JOIN datasets d ON d.id = w.dataset_id
            WHERE w.user_id = ?
            ORDER BY w.updated_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def link_workspace_workflow(workspace_id: int, user_id: str, workflow_id: int) -> dict | None:
    """Link a saved workflow draft to a workspace.

    Returns the updated workspace dict, or None if not found / not owned.
    """
    now = _now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE ai_workspaces
               SET workflow_id = ?,
                   updated_at  = ?
             WHERE id = ? AND user_id = ?
            """,
            (workflow_id, now, workspace_id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_workspace_by_id(workspace_id, user_id)


def get_workspace_by_id(workspace_id: int, user_id: str) -> dict | None:
    """Return a single workspace including the decoded proposal and execution summary.

    Returns None if not found or belongs to a different user.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT
                w.id,
                w.title,
                w.status,
                w.intent_text,
                w.dataset_id,
                d.filename   AS dataset_filename,
                w.proposal_json,
                w.proposal_source,
                w.proposed_at,
                w.report_id,
                w.workflow_id,
                w.execution_summary_json,
                w.selected_sections_json,
                w.executed_at,
                w.saved_at,
                w.created_at,
                w.updated_at
            FROM ai_workspaces w
            LEFT JOIN datasets d ON d.id = w.dataset_id
            WHERE w.id = ? AND w.user_id = ?
            """,
            (workspace_id, user_id),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        raw_proposal = record.pop("proposal_json", None)
        record["proposal"] = json.loads(raw_proposal) if raw_proposal else None
        raw_exec = record.pop("execution_summary_json", None)
        record["execution_summary"] = json.loads(raw_exec) if raw_exec else None
        raw_sections = record.pop("selected_sections_json", None)
        record["selected_sections"] = json.loads(raw_sections) if raw_sections else None
        return record
    finally:
        conn.close()
