from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.api_key import AuthenticatedUser, require_api_key, require_role
from core.errors.error_response import build_error_response
from core.input.input_handler import handle_input
from core.security.encryption import decrypt
from core.output.output_formatter import format_output
from core.workflows.workflow_runner import run_workflow_by_name
from core.config import RETENTION_DAYS
from data.audit import delete_audit_log_entries, purge_old_audit_db, purge_old_audit_log_file
from data.db import get_connection
from data.execution_history import get_repeated_intent_suggestions, get_workflow_success_insights, purge_old_execution_history
from data.usage_service import count_usage_events, list_usage_events
from data.tenant_service import get_tenant_by_id
from core.quota import check_quota

router = APIRouter()


class InterpretRequest(BaseModel):
    input: str


class WorkflowRunRequest(BaseModel):
    name: str


class DeleteMyDataRequest(BaseModel):
    confirm: bool = False


@router.post("/interpret")
def interpret(request: InterpretRequest, user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    if not request.input.strip():
        return JSONResponse(status_code=400, content=build_error_response("Input cannot be empty"))
    try:
        tenant = get_tenant_by_id(user.tenant_id)
        plan = tenant["plan"] if tenant else "free"
        quota = check_quota(user.tenant_id, plan, "interpret")
        if not quota["allowed"]:
            return JSONResponse(
                status_code=429,
                content={
                    "status": "error",
                    "message": "Plan limit reached",
                    "details": {
                        "current_usage": quota["current_usage"],
                        "limit": quota["limit"],
                    },
                },
            )
    except Exception:
        pass  # quota check failure — allow request to proceed
    try:
        return handle_input(request.input, user_id=user.user_id, tenant_id=user.tenant_id)
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/workflows/run")
def run_workflow(request: WorkflowRunRequest, user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        tenant = get_tenant_by_id(user.tenant_id)
        plan = tenant["plan"] if tenant else "free"
        quota = check_quota(user.tenant_id, plan, "workflow_run")
        if not quota["allowed"]:
            return JSONResponse(
                status_code=429,
                content={
                    "status": "error",
                    "message": "Plan limit reached",
                    "details": {
                        "current_usage": quota["current_usage"],
                        "limit": quota["limit"],
                    },
                },
            )
    except Exception:
        pass  # quota check failure — allow request to proceed
    try:
        result = run_workflow_by_name(request.name, user_id=user.user_id, tenant_id=user.tenant_id)
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


@router.get("/me/data")
def get_my_data(user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        conn = get_connection()

        audit_rows = conn.execute(
            "SELECT id, timestamp, task_type, original_input, status"
            " FROM audit_logs"
            " WHERE user_id = ?"
            " ORDER BY id DESC",
            (user.user_id,),
        ).fetchall()

        execution_rows = conn.execute(
            "SELECT id, plan_id, workflow_id, trigger_source,"
            " task_type, intent, status,"
            " started_at, finished_at, duration_ms,"
            " step_count, failed_step_id, failed_tool, error_message"
            " FROM execution_history"
            " WHERE user_id = ?"
            " ORDER BY id DESC",
            (user.user_id,),
        ).fetchall()

        conn.close()

        audit_data = []
        for row in audit_rows:
            d = dict(row)
            d["original_input"] = decrypt(d["original_input"])
            audit_data.append(d)

        return {
            "status": "success",
            "data": {
                "audit_logs": audit_data,
                "execution_history": [dict(row) for row in execution_rows],
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.delete("/me/data")
def delete_my_data(
    request: DeleteMyDataRequest,
    user: AuthenticatedUser = Depends(require_api_key),
) -> dict:
    if not request.confirm:
        return JSONResponse(
            status_code=400,
            content=build_error_response('Confirmation required. Send {"confirm": true} to proceed.'),
        )
    try:
        conn = get_connection()
        audit_cur = conn.execute(
            "DELETE FROM audit_logs WHERE user_id = ?",
            (user.user_id,),
        )
        audit_deleted = audit_cur.rowcount
        exec_cur = conn.execute(
            "DELETE FROM execution_history WHERE user_id = ?",
            (user.user_id,),
        )
        exec_deleted = exec_cur.rowcount
        conn.commit()
        conn.close()
        log_deleted = delete_audit_log_entries(user.user_id)
        return {
            "status": "success",
            "data": {
                "deleted": {
                    "audit_logs": audit_deleted,
                    "execution_history": exec_deleted,
                    "audit_log_file": log_deleted,
                }
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/admin/purge")
def admin_purge(user: AuthenticatedUser = Depends(require_role("admin"))) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    audit_db_deleted = purge_old_audit_db(cutoff)
    exec_deleted = purge_old_execution_history(cutoff)
    log_deleted = purge_old_audit_log_file(cutoff)
    return {
        "status": "success",
        "data": {
            "retention_days": RETENTION_DAYS,
            "cutoff": cutoff,
            "deleted": {
                "audit_logs": audit_db_deleted,
                "execution_history": exec_deleted,
                "audit_log_file": log_deleted,
            },
        },
    }


@router.get("/insights")
def insights(user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        return {"status": "success", "data": get_workflow_success_insights(tenant_id=user.tenant_id)}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/recommendations")
def recommendations(user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        return {"status": "success", "data": get_repeated_intent_suggestions(tenant_id=user.tenant_id)}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/usage")
def usage(user: AuthenticatedUser = Depends(require_api_key)) -> dict:
    try:
        return {
            "status": "success",
            "data": {
                "tenant_id": user.tenant_id,
                "total_events": count_usage_events(user.tenant_id),
                "by_event_type": {
                    "interpret":     count_usage_events(user.tenant_id, event_type="interpret"),
                    "workflow_run":  count_usage_events(user.tenant_id, event_type="workflow_run"),
                },
                "recent_events": list_usage_events(user.tenant_id, limit=10),
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/health")
def health() -> dict:
    return {"status": "healthy"}
