import json

# ---------------------------------------------------------------------------
# Built-in tool definitions — the canonical source for the three static tools.
# These are always present regardless of DB state or ENABLE_DYNAMIC_TOOLS.
# DB rows for built-ins are informational only; the entries below are
# authoritative and cannot be overridden by the database.
# ---------------------------------------------------------------------------
_BUILTIN_REGISTRY: dict = {
    "email_sender": {
        "name": "email_sender",
        "slug": "email_sender",
        "description": "Sends emails to a specified recipient.",
        "enabled": True,
        "approved": True,
        "source": "builtin",
        "operations": ["send_email"],
        "required_params": {
            "send_email": ["to", "subject", "body_template"]
        },
    },
    "data_fetcher": {
        "name": "data_fetcher",
        "slug": "data_fetcher",
        "description": "Fetches data from the SQLite database.",
        "enabled": True,
        "approved": True,
        "source": "builtin",
        "operations": ["fetch_report_data"],
        "required_params": {
            "fetch_report_data": ["source", "table"]
        },
    },
    "notifier": {
        "name": "notifier",
        "slug": "notifier",
        "description": "Sends in-app or push notifications.",
        "enabled": True,
        "approved": True,
        "source": "builtin",
        "operations": ["send_notification"],
        "required_params": {
            "send_notification": ["channel", "message", "priority"]
        },
    },
}


def _load_registry() -> dict:
    """Return the merged tool registry.

    Always starts with built-ins. When ENABLE_DYNAMIC_TOOLS is true, also
    reads approved+enabled rows from the DB and merges them in. Built-in
    names can never be overridden by DB rows.

    Any DB or JSON error is silently swallowed so startup is never blocked.
    """
    from core.config import ENABLE_DYNAMIC_TOOLS

    merged: dict = dict(_BUILTIN_REGISTRY)

    if not ENABLE_DYNAMIC_TOOLS:
        return merged

    try:
        from data.db import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT name, slug, config_json FROM tools "
                "WHERE enabled = 1 AND approved = 1"
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            name = row["name"]
            if name in _BUILTIN_REGISTRY:
                # Built-ins are authoritative; DB rows for them are ignored here
                continue
            try:
                config = json.loads(row["config_json"] or "{}")
                if not isinstance(config, dict):
                    continue
                if "operations" not in config or "required_params" not in config:
                    continue
                merged[name] = {
                    "name": name,
                    "slug": row["slug"] or name,
                    "description": config.get("description", ""),
                    "enabled": True,
                    "approved": True,
                    "source": "dynamic",
                    "operations": config["operations"],
                    "required_params": config["required_params"],
                    # Stored so the primitive executor can act without a DB round-trip
                    "primitive_type": config.get("primitive_type"),
                    "primitive_config": config.get("config") or {},
                }
            except Exception:
                pass  # skip malformed rows silently

    except Exception:
        pass  # DB unavailable — built-ins are still returned

    return merged


# Public registry — populated once at module import.
# With ENABLE_DYNAMIC_TOOLS=false (default) this is identical to _BUILTIN_REGISTRY.
# A server restart is required to pick up registry changes from the DB.
TOOL_REGISTRY: dict = _load_registry()


# ---------------------------------------------------------------------------
# Public API — unchanged from original; all callers continue to work as-is.
# ---------------------------------------------------------------------------

def get_tool(tool_name: str) -> dict | None:
    return TOOL_REGISTRY.get(tool_name)


def is_valid_tool(tool_name: str) -> bool:
    tool = get_tool(tool_name)
    return tool is not None and tool["enabled"]


def is_valid_operation(tool_name: str, operation: str) -> bool:
    tool = get_tool(tool_name)
    if tool is None or not tool["enabled"]:
        return False
    return operation in tool["operations"]


def validate_step(step: dict) -> bool:
    tool_name = step.get("tool")
    operation = step.get("operation")
    params = step.get("params") or {}

    if not is_valid_tool(tool_name):
        raise ValueError(f"Unknown or disabled tool: '{tool_name}'")

    if not is_valid_operation(tool_name, operation):
        raise ValueError(
            f"Operation '{operation}' is not allowed for tool '{tool_name}'. "
            f"Allowed: {TOOL_REGISTRY[tool_name]['operations']}"
        )

    required = TOOL_REGISTRY[tool_name]["required_params"].get(operation, [])
    missing = [p for p in required if p not in params]
    if missing:
        raise ValueError(
            f"Step '{step.get('step_id', '?')}' is missing required params "
            f"for '{tool_name}.{operation}': {missing}"
        )

    return True


def list_tools() -> list[str]:
    return [name for name, tool in TOOL_REGISTRY.items() if tool["enabled"]]
