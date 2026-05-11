from core.interpreter.task_interpreter import interpret_task
from core.execution.execution_engine import run_plan
from core.output.output_formatter import format_output
from data.audit import log_audit_event
from data.execution_history import log_execution_history
from data.usage_service import log_usage_event

def handle_input(user_input: str, user_id: str | None = None, tenant_id: str | None = None) -> dict:
    plan = interpret_task(user_input)
    result = run_plan(plan)
    result["original_input"] = user_input
    log_audit_event(result, user_id=user_id, tenant_id=tenant_id)
    log_execution_history(plan, result, workflow_id=None, trigger_source="interpreter", user_id=user_id, tenant_id=tenant_id)
    if tenant_id is not None:
        log_usage_event(tenant_id, user_id, "interpret", "api", reference_id=result.get("plan_id"))
    return format_output(result)
