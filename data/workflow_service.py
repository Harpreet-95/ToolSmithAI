import json

from data.db import get_connection

ALLOWED_MULTI_STEP_TYPES = frozenset({
    "generate_dataset_report",
    "email_dataset_report",
    "send_notification",
    "analyze_dataset",
})

_MAX_WORKFLOW_STEPS = 10


def _validate_multi_step_definition(definition: dict) -> None:
    workflow_steps = definition.get("workflow_steps")
    if not isinstance(workflow_steps, list) or len(workflow_steps) == 0:
        raise ValueError("workflow_steps must be a non-empty list")
    if len(workflow_steps) > _MAX_WORKFLOW_STEPS:
        raise ValueError(f"Workflow may contain at most {_MAX_WORKFLOW_STEPS} steps")
    for i, step in enumerate(workflow_steps):
        if not isinstance(step, dict):
            raise ValueError(f"Step {i + 1} must be an object")
        step_type = step.get("type")
        if step_type not in ALLOWED_MULTI_STEP_TYPES:
            raise ValueError(
                f"Step {i + 1} has invalid type '{step_type}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_MULTI_STEP_TYPES))}"
            )


def _normalize(row) -> dict:
    return {
        "id":         row["id"],
        "name":       row["name"],
        "definition": json.loads(row["definition"]),
    }


def create_workflow(name: str, definition: dict, user_id: str | None = None) -> int:
    if not name or not name.strip():
        raise ValueError("Workflow name must not be empty")

    has_workflow_steps = bool(definition.get("workflow_steps"))
    has_steps = isinstance(definition.get("steps"), list) and len(definition["steps"]) > 0

    if has_workflow_steps:
        _validate_multi_step_definition(definition)
    elif not has_steps:
        raise ValueError("Workflow definition must contain a non-empty steps list")

    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO workflows (name, definition, user_id) VALUES (?, ?, ?)",
        (name.strip(), json.dumps(definition), user_id),
    )
    conn.commit()
    workflow_id = cursor.lastrowid
    conn.close()
    return workflow_id


def get_workflow_by_name(name: str, user_id: str | None = None) -> dict | None:
    conn = get_connection()
    if user_id is not None:
        row = conn.execute(
            "SELECT id, name, definition FROM workflows WHERE name = ? AND user_id = ? LIMIT 1",
            (name, user_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, name, definition FROM workflows WHERE name = ? LIMIT 1",
            (name,),
        ).fetchone()
    conn.close()
    if row is None:
        return None
    return _normalize(row)


def list_workflows(user_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, definition FROM workflows WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [_normalize(row) for row in rows]
    finally:
        conn.close()


def delete_workflow(workflow_id: int, user_id: str) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM workflows WHERE id = ? AND user_id = ?",
            (workflow_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_workflow_by_id(workflow_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, name, definition FROM workflows WHERE id = ? LIMIT 1",
        (workflow_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _normalize(row)
