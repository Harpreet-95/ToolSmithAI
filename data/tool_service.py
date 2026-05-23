import json
from datetime import datetime, timezone

from data.db import get_connection

# Built-in tool names are seeded by init_db and must never be mutated via the API.
_BUILTIN_NAMES: frozenset = frozenset({"email_sender", "data_fetcher", "notifier"})

# Substrings that must never appear in config_json values.
# Tools are data-driven configurations — they must not contain executable code.
_FORBIDDEN_CONFIG_SUBSTRINGS: frozenset = frozenset({
    "exec(",
    "eval(",
    "__import__",
    "importlib",
    "subprocess",
    "os.system",
    "open(",
    "__builtins__",
    "__globals__",
})


def _validate_tool_config(config_json: dict) -> None:
    """Validate a tool config_json dict at create and approve time.

    Checks:
      - primitive_type present and in ALLOWED_PRIMITIVES
      - config sub-object is a dict
      - operations is a non-empty list of strings
      - required_params is a dict
      - no forbidden executable-code substrings anywhere in the payload

    Raises ValueError with a descriptive message on the first failure.
    """
    from core.primitives.executor import ALLOWED_PRIMITIVES

    if not isinstance(config_json, dict):
        raise ValueError("config_json must be a JSON object.")

    primitive_type = config_json.get("primitive_type")
    if not primitive_type or not isinstance(primitive_type, str):
        raise ValueError("config_json must contain a non-empty 'primitive_type' string.")
    if primitive_type not in ALLOWED_PRIMITIVES:
        raise ValueError(
            f"primitive_type '{primitive_type}' is not in the allowed set: "
            f"{sorted(ALLOWED_PRIMITIVES)}"
        )

    prim_config = config_json.get("config")
    if not isinstance(prim_config, dict):
        raise ValueError(
            "config_json must contain a 'config' object for primitive-specific settings."
        )

    operations = config_json.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("config_json must contain a non-empty 'operations' list.")
    if not all(isinstance(op, str) and op for op in operations):
        raise ValueError("Every entry in 'operations' must be a non-empty string.")

    required_params = config_json.get("required_params")
    if not isinstance(required_params, dict):
        raise ValueError("config_json must contain a 'required_params' object.")

    # Safety scan: reject any payload that contains executable-code strings
    raw = json.dumps(config_json)
    for fragment in _FORBIDDEN_CONFIG_SUBSTRINGS:
        if fragment in raw:
            raise ValueError(
                f"config_json contains a forbidden string '{fragment}'. "
                "Tool definitions must not contain executable code."
            )


def _serialize_row(row: dict) -> dict:
    """Parse config_json if present and return a clean dict."""
    out = dict(row)
    raw = out.get("config_json")
    if raw:
        try:
            out["config_json"] = json.loads(raw)
        except (ValueError, TypeError):
            out["config_json"] = None
    return out


def list_tools_db() -> list[dict]:
    """Return all tool rows from the DB — admin view, no approval filter."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, slug, config, config_json, enabled, approved, "
            "approved_by, approved_at, created_by, created_at "
            "FROM tools ORDER BY id"
        ).fetchall()
        return [_serialize_row(dict(r)) for r in rows]
    finally:
        conn.close()


def get_tool_by_id_db(tool_id: int) -> dict | None:
    """Return a single tool row by primary key — admin view."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, slug, config, config_json, enabled, approved, "
            "approved_by, approved_at, created_by, created_at "
            "FROM tools WHERE id = ?",
            (tool_id,),
        ).fetchone()
        return _serialize_row(dict(row)) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write — create / update / approve
# ---------------------------------------------------------------------------

_SELECT_FULL = (
    "SELECT id, name, slug, config, config_json, enabled, approved, "
    "approved_by, approved_at, created_by, created_at FROM tools WHERE id = ?"
)


def create_tool_db(
    name: str,
    slug: str | None,
    config_json: dict,
    created_by: str,
) -> int:
    """Insert a new dynamic tool as a draft (approved=0, enabled=0).

    Raises ValueError if the name clashes with a built-in or an existing tool,
    or if config_json fails validation.

    Returns the new tool's integer primary key.
    """
    name = name.strip()
    if not name:
        raise ValueError("Tool name must not be empty.")
    if name in _BUILTIN_NAMES:
        raise ValueError(f"'{name}' is a built-in tool name and cannot be used.")

    _validate_tool_config(config_json)

    effective_slug = (slug or name).strip()
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM tools WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"A tool named '{name}' already exists (id={existing[0]}).")

        cursor = conn.execute(
            "INSERT INTO tools "
            "(name, slug, config, config_json, enabled, approved, created_by, created_at) "
            "VALUES (?, ?, '{}', ?, 0, 0, ?, ?)",
            (name, effective_slug, json.dumps(config_json), created_by, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_tool_db(tool_id: int, updates: dict) -> dict | None:
    """Update editable fields on a tool.  Only allowed while approved=0.

    Accepted keys in updates: name, slug, config_json, enabled.
    Raises ValueError if the tool is already approved, is a built-in, or if
    the new config_json fails validation.

    Returns the updated tool row dict, or None if the tool does not exist.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, approved FROM tools WHERE id = ?", (tool_id,)
        ).fetchone()
        if row is None:
            return None

        row_name = row["name"]
        if row_name in _BUILTIN_NAMES:
            raise ValueError("Built-in tools cannot be modified via the API.")

        if row["approved"]:
            raise ValueError(
                "This tool has already been approved and cannot be edited. "
                "Approved tools are immutable to preserve execution safety."
            )

        allowed_fields = {"name", "slug", "config_json", "enabled"}
        set_parts: list[str] = []
        params: list = []

        for field, value in updates.items():
            if field not in allowed_fields:
                continue
            if field == "config_json":
                if not isinstance(value, dict):
                    raise ValueError("config_json must be a JSON object.")
                _validate_tool_config(value)
                set_parts.append("config_json = ?")
                params.append(json.dumps(value))
            elif field == "name":
                new_name = str(value).strip()
                if not new_name:
                    raise ValueError("Tool name must not be empty.")
                if new_name in _BUILTIN_NAMES:
                    raise ValueError(f"'{new_name}' is a reserved built-in name.")
                conflict = conn.execute(
                    "SELECT id FROM tools WHERE name = ? AND id != ?", (new_name, tool_id)
                ).fetchone()
                if conflict:
                    raise ValueError(f"A tool named '{new_name}' already exists.")
                set_parts.append("name = ?")
                params.append(new_name)
            elif field == "slug":
                set_parts.append("slug = ?")
                params.append(str(value).strip())
            elif field == "enabled":
                set_parts.append("enabled = ?")
                params.append(1 if value else 0)

        if set_parts:
            params.append(tool_id)
            conn.execute(f"UPDATE tools SET {', '.join(set_parts)} WHERE id = ?", params)
            conn.commit()

        updated = conn.execute(_SELECT_FULL, (tool_id,)).fetchone()
        return _serialize_row(dict(updated))
    finally:
        conn.close()


def approve_tool_db(tool_id: int, approved_by: str) -> dict | None:
    """Validate config_json and set approved=1, enabled=1.

    Re-runs full validation at approval time so any drift since creation is caught.
    Raises ValueError for built-ins, missing config, or invalid config.

    Returns the updated tool row dict, or None if the tool does not exist.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, config_json FROM tools WHERE id = ?", (tool_id,)
        ).fetchone()
        if row is None:
            return None

        if row["name"] in _BUILTIN_NAMES:
            raise ValueError(
                "Built-in tools are pre-approved and cannot go through the approval flow."
            )

        raw_config = row["config_json"]
        if not raw_config:
            raise ValueError(
                "Tool has no config_json. Set config_json via PATCH before approving."
            )

        try:
            config_json = json.loads(raw_config)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"config_json is not valid JSON: {exc}") from exc

        # Full re-validation at approval time
        _validate_tool_config(config_json)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE tools SET approved = 1, enabled = 1, approved_by = ?, approved_at = ? "
            "WHERE id = ?",
            (approved_by, now, tool_id),
        )
        conn.commit()

        updated = conn.execute(_SELECT_FULL, (tool_id,)).fetchone()
        return _serialize_row(dict(updated))
    finally:
        conn.close()
