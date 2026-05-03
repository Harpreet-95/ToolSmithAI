from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.api_key import AuthenticatedUser, require_api_key, require_role
from core.errors.error_response import build_error_response
from core.input.input_handler import handle_input
from core.security.encryption import decrypt
from core.output.output_formatter import format_output
from core.workflows.workflow_runner import run_workflow_by_name
from data.db import get_connection
from data.execution_history import get_repeated_intent_suggestions, get_workflow_success_insights

router = APIRouter()


class InterpretRequest(BaseModel):
    input: str


class WorkflowRunRequest(BaseModel):
    name: str


@router.post("/interpret")
def interpret(request: InterpretRequest, user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    if not request.input.strip():
        return JSONResponse(status_code=400, content=build_error_response("Input cannot be empty"))
    try:
        return handle_input(request.input)
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/workflows/run")
def run_workflow(request: WorkflowRunRequest, user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        result = run_workflow_by_name(request.name)
        return format_output(result)
    except ValueError as e:
        return JSONResponse(status_code=404, content=build_error_response("Not found", str(e)))
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/audit")
def get_audit_logs(
    user: AuthenticatedUser = Depends(require_role("admin")),
    limit: int = 10,
    task_type: str | None = None,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
) -> dict:
    try:
        conn = get_connection()
        conditions = []
        params = []
        if task_type is not None:
            conditions.append("task_type = ?")
            params.append(task_type)
        if from_timestamp is not None:
            conditions.append("timestamp >= ?")
            params.append(from_timestamp)
        if to_timestamp is not None:
            conditions.append("timestamp <= ?")
            params.append(to_timestamp)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, timestamp, task_type, original_input, status FROM audit_logs {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        conn.close()
        data = []
        for row in rows:
            d = dict(row)
            d["original_input"] = decrypt(d["original_input"])
            data.append(d)
        return {"status": "success", "data": data}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/insights")
def insights(user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        return format_output(get_workflow_success_insights())
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/recommendations")
def recommendations(user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        return format_output(get_repeated_intent_suggestions())
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/health")
def health() -> dict:
    return {"status": "healthy"}
