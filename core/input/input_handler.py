from core.interpreter.task_interpreter import (
    interpret_task,
    _build_dataset_report_plan,
    _build_email_dataset_report_plan,
)
from core.execution.execution_engine import run_plan
from core.output.output_formatter import format_output
from core.workflows.workflow_runner import run_dataset_report_plan, run_email_dataset_report_plan
from data.audit import log_audit_event
from data.execution_history import log_execution_history
from data.usage_service import log_usage_event

_REPORT_HINT_WORDS = frozenset({
    "report", "summary", "analyze", "analyse",
    "summarize", "summarise", "insight", "insights",
})
_EMAIL_HINT_WORDS = frozenset({"email", "send", "mail", "share"})


def handle_input(user_input: str, user_id: str | None = None, dataset_id: int | None = None, recipient: str | None = None) -> dict:
    lowered = user_input.lower()

    # When a dataset is selected and the input is report-related, bypass generic
    # keyword matching and route directly to dataset-specific plan builders.
    # This prevents "generate report" from producing unrelated data_fetcher + email steps.
    if dataset_id is not None and any(w in lowered for w in _REPORT_HINT_WORDS):
        if any(w in lowered for w in _EMAIL_HINT_WORDS):
            plan = _build_email_dataset_report_plan(user_input)
        else:
            plan = _build_dataset_report_plan(user_input)
    else:
        plan = interpret_task(user_input)

    if plan.get("task_type") == "generate_dataset_report":
        result = run_dataset_report_plan(plan, user_id, dataset_id=dataset_id)
    elif plan.get("task_type") == "email_dataset_report":
        result = run_email_dataset_report_plan(plan, user_id, dataset_id=dataset_id, recipient=recipient)
    else:
        # Inject recipient and plan context into any send_email step params so the
        # handler can address and compose the email without needing the full plan.
        if recipient is not None or plan.get("intent"):
            for step in plan.get("steps", []):
                if step.get("operation") == "send_email":
                    step.setdefault("params", {}).update({
                        "to": recipient,
                        "intent": plan.get("intent", ""),
                        "task_type": plan.get("task_type", ""),
                    })
        result = run_plan(plan)

    result["original_input"] = user_input
    log_audit_event(result, user_id=user_id)
    log_execution_history(plan, result, workflow_id=None, trigger_source="interpreter", user_id=user_id, dataset_id=dataset_id)
    if user_id is not None:
        log_usage_event(user_id, "interpret", "api", reference_id=result.get("plan_id"))
    return format_output(result)
