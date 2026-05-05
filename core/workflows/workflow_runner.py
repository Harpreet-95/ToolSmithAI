import uuid

from data.workflow_service import get_workflow_by_id, get_workflow_by_name
from core.execution.execution_engine import run_plan
from data.execution_history import log_execution_history


def _build_plan(workflow: dict) -> dict:
    return {
        "plan_id": str(uuid.uuid4()),
        "intent": workflow["name"],
        "steps": workflow["definition"]["steps"],
    }


def run_workflow_by_name(name: str, user_id: str | None = None) -> dict:
    workflow = get_workflow_by_name(name)
    if workflow is None:
        raise ValueError(f"No workflow found with name: '{name}'")
    plan = _build_plan(workflow)
    result = run_plan(plan)
    log_execution_history(plan, result, workflow_id=workflow["id"], trigger_source="workflow_api", user_id=user_id)
    return result


def run_workflow_by_id(workflow_id: int, user_id: str | None = None) -> dict:
    workflow = get_workflow_by_id(workflow_id)
    if workflow is None:
        raise ValueError(f"No workflow found with id: {workflow_id}")
    plan = _build_plan(workflow)
    result = run_plan(plan)
    log_execution_history(plan, result, workflow_id=workflow["id"], trigger_source="workflow_api", user_id=user_id)
    return result
