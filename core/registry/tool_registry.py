TOOL_REGISTRY = {
    "email_sender": {
        "name": "email_sender",
        "description": "Sends emails to a specified recipient.",
        "enabled": True,
        "operations": ["send_email"],
        "required_params": {
            "send_email": ["to", "subject", "body_template"]
        },
    },
    "data_fetcher": {
        "name": "data_fetcher",
        "description": "Fetches data from the SQLite database.",
        "enabled": True,
        "operations": ["fetch_report_data"],
        "required_params": {
            "fetch_report_data": ["source", "table"]
        },
    },
    "notifier": {
        "name": "notifier",
        "description": "Sends in-app or push notifications.",
        "enabled": True,
        "operations": ["send_notification"],
        "required_params": {
            "send_notification": ["channel", "message", "priority"]
        },
    },
}


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
