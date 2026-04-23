import uuid
import datetime

from core.registry.tool_registry import validate_step


def detect_task_type(lowered: str) -> str:
    PRIORITY = ["generate_report", "send_email", "set_reminder"]
    matched = []
    if "email" in lowered or "mail" in lowered or "message" in lowered:
        matched.append("send_email")
    if "report" in lowered or "summary" in lowered:
        matched.append("generate_report")
    if "remind" in lowered or "reminder" in lowered:
        matched.append("set_reminder")
    return next((t for t in PRIORITY if t in matched), "unknown")


def detect_frequency(lowered: str) -> str | None:
    if "daily" in lowered:
        return "daily"
    elif "weekly" in lowered:
        return "weekly"
    elif "monthly" in lowered:
        return "monthly"
    return None


def build_execution_plan(intent: str, task_type: str, frequency: str | None) -> dict:
    plan_id = str(uuid.uuid4())
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    schedule = None
    if frequency:
        unit_map = {"daily": "day", "weekly": "week", "monthly": "month"}
        schedule = {
            "frequency": frequency,
            "interval": 1,
            "unit": unit_map.get(frequency, frequency),
            "start_at": None,
            "timezone": "UTC",
        }

    if task_type == "send_email":
        steps = [
            {
                "step_id": "step_1",
                "order": 1,
                "tool": "email_sender",
                "operation": "send_email",
                "params": {
                    "to": None,
                    "subject": "Your Report",
                    "body_template": "default_email_template",
                },
                "depends_on": None,
            }
        ]
    elif task_type == "generate_report":
        steps = [
            {
                "step_id": "step_1",
                "order": 1,
                "tool": "data_fetcher",
                "operation": "fetch_report_data",
                "params": {"source": "sqlite", "table": "tasks", "filters": {}},
                "depends_on": None,
            },
            {
                "step_id": "step_2",
                "order": 2,
                "tool": "email_sender",
                "operation": "send_email",
                "params": {
                    "to": None,
                    "subject": "Your Report",
                    "body_template": "report_template_v1",
                },
                "depends_on": "step_1",
            },
        ]
    elif task_type == "set_reminder":
        steps = [
            {
                "step_id": "step_1",
                "order": 1,
                "tool": "notifier",
                "operation": "send_notification",
                "params": {
                    "channel": "in_app",
                    "message": "Your reminder",
                    "priority": "normal",
                },
                "depends_on": None,
            }
        ]
    else:
        steps = []

    for step in steps:
        validate_step(step)

    tags = ["unknown"] if task_type == "unknown" else [task_type]
    if frequency:
        tags.append(frequency)

    return {
        "plan_id": plan_id,
        "version": "1.0",
        "created_at": created_at,
        "status": "pending",
        "intent": intent,
        "task_type": task_type,
        "schedule": schedule,
        "steps": steps,
        "metadata": {
            "source": "rule_based_interpreter",
            "tags": tags,
        },
    }


def interpret_task(user_input: str) -> dict:
    lowered = user_input.lower()
    task = detect_task_type(lowered)
    frequency = detect_frequency(lowered)
    return build_execution_plan(intent=user_input, task_type=task, frequency=frequency)
