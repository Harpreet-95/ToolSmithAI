from core.interpreter.task_interpreter import interpret_task
from core.execution.execution_engine import run_plan
from core.output.output_formatter import format_output
from data.audit import log_audit_event
from data.execution_history import log_execution_history

def handle_input(user_input: str, user_id: str | None = None) -> dict:
    plan = interpret_task(user_input)
    result = run_plan(plan)
    result["original_input"] = user_input
    log_audit_event(result, user_id=user_id)
    log_execution_history(plan, result, workflow_id=None, trigger_source="interpreter", user_id=user_id)
    return format_output(result)
