import json

from data.db import get_connection


def _normalize(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "definition": json.loads(row["definition"]),
    }


def create_workflow(name: str, definition: dict) -> int:
    if not name or not name.strip():
        raise ValueError("Workflow name must not be empty")

    steps = definition.get("steps")
    if not steps or not isinstance(steps, list):
        raise ValueError("Workflow definition must contain a non-empty steps list")

    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO workflows (name, definition) VALUES (?, ?)",
        (name.strip(), json.dumps(definition)),
    )
    conn.commit()
    workflow_id = cursor.lastrowid
    conn.close()
    return workflow_id


def get_workflow_by_name(name: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, name, definition FROM workflows WHERE name = ? LIMIT 1",
        (name,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _normalize(row)


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
