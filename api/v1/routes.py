from datetime import datetime, timedelta, timezone
import hashlib
import io
import math
import secrets

import pandas as pd

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

from auth.api_key import AuthenticatedUser, require_api_key, require_role
from auth.jwt_auth import create_access_token, require_auth, require_jwt
from auth.password import hash_password, verify_password
from core.email import send_verification_email
from core.errors.error_response import build_error_response
from core.input.input_handler import handle_input
from core.security.encryption import decrypt
from core.output.output_formatter import format_output
from core.workflows.workflow_runner import run_workflow_by_name
from data.dataset_service import create_dataset_summary, list_datasets_for_user, delete_dataset, rename_dataset, get_dataset_by_id
from data.scheduled_workflow_service import (
    create_scheduled_workflow,
    list_scheduled_workflows,
    delete_scheduled_workflow,
    pause_scheduled_workflow,
    resume_scheduled_workflow,
    get_schedule_health,
)
from core.interpreter.task_interpreter import interpret_task
from data.workflow_service import create_workflow, list_workflows, delete_workflow
from core.config import ENABLE_REAL_EMAIL, RETENTION_DAYS
from data.audit import delete_audit_log_entries, purge_old_audit_db, purge_old_audit_log_file
from data.db import get_connection
from data.execution_history import enrich_execution_record, get_execution_by_id, get_repeated_intent_suggestions, get_workflow_success_insights, purge_old_execution_history
from data.usage_service import count_usage_events, list_usage_events


def _compute_date_profile(
    df: pd.DataFrame,
    numeric_cols: list,
    categorical_cols: list,
) -> dict:
    """
    Detect date columns in a DataFrame and compute trend insights.
    Runs at upload time when the full DataFrame is available.
    Never raises — returns empty structure on any failure.
    """
    DATE_THRESHOLD = 0.70
    date_columns: list[dict] = []

    for col in categorical_cols:
        try:
            series = df[col].dropna()
            if len(series) == 0:
                continue
            parsed = pd.to_datetime(series, errors="coerce")
            valid = int(parsed.notna().sum())
            if valid == 0 or valid / len(series) < DATE_THRESHOLD:
                continue
            valid_dates = parsed.dropna().sort_values()
            date_columns.append({
                "column":      col,
                "earliest":    valid_dates.iloc[0].isoformat(),
                "latest":      valid_dates.iloc[-1].isoformat(),
                "valid_count": valid,
                "range_days":  int((valid_dates.iloc[-1] - valid_dates.iloc[0]).days),
            })
        except Exception:
            continue

    trend_insights: list[dict] = []
    if date_columns and numeric_cols:
        primary_col = date_columns[0]["column"]
        try:
            df_s = df.copy()
            df_s["__date__"] = pd.to_datetime(df_s[primary_col], errors="coerce")
            df_s = df_s.dropna(subset=["__date__"]).sort_values("__date__").reset_index(drop=True)
            n = len(df_s)
            if n >= 4:
                half = n // 2
                for num_col in numeric_cols[:3]:
                    try:
                        f_mean = df_s.iloc[:half][num_col].dropna().mean()
                        s_mean = df_s.iloc[half:][num_col].dropna().mean()
                        if pd.isna(f_mean) or pd.isna(s_mean) or f_mean == 0:
                            continue
                        pct = round(((s_mean - f_mean) / abs(f_mean)) * 100, 1)
                        if abs(pct) < 5:
                            trend, symbol = "stable", "→"
                        elif pct > 0:
                            trend, symbol = "increasing", "↑"
                        else:
                            trend, symbol = "decreasing", "↓"
                        trend_insights.append({
                            "column":           num_col,
                            "trend":            trend,
                            "pct_change":       pct,
                            "symbol":           symbol,
                            "first_half_mean":  round(float(f_mean), 4),
                            "second_half_mean": round(float(s_mean), 4),
                        })
                    except Exception:
                        continue
        except Exception:
            pass

    return {"date_columns": date_columns, "trend_insights": trend_insights}


WORKFLOW_TEMPLATES: list[dict] = [
    {
        "id":          "weekly-dataset-report",
        "name":        "Weekly Dataset Report",
        "description": "Generate a structured report from your uploaded dataset every week.",
        "category":    "Reporting",
        "intent":      "generate a weekly dataset report",
        "frequency":   "weekly",
    },
    {
        "id":          "email-executive-summary",
        "name":        "Email Executive Summary",
        "description": "Generate and email a concise executive summary of your dataset metrics.",
        "category":    "Email",
        "intent":      "email me an executive summary of the dataset",
        "frequency":   "weekly",
    },
    {
        "id":          "monthly-kpi-report",
        "name":        "Monthly KPI Report",
        "description": "Create a detailed KPI analysis report from your dataset metrics each month.",
        "category":    "Reporting",
        "intent":      "generate a monthly KPI report from the dataset",
        "frequency":   "monthly",
    },
    {
        "id":          "daily-operations-digest",
        "name":        "Daily Operations Digest",
        "description": "Get a daily summary of key operational metrics from your current dataset.",
        "category":    "Operations",
        "intent":      "generate a daily operations digest from the dataset",
        "frequency":   "daily",
    },
    {
        "id":          "dataset-health-monitor",
        "name":        "Dataset Health Monitor",
        "description": "Check your dataset for anomalies, missing values, and data quality issues.",
        "category":    "Monitoring",
        "intent":      "analyze the dataset for data quality and missing values",
        "frequency":   "daily",
    },
    {
        "id":          "scheduled-sales-summary",
        "name":        "Scheduled Sales Summary",
        "description": "Generate a recurring sales performance summary from your uploaded sales data.",
        "category":    "Sales",
        "intent":      "generate a sales summary report from the dataset",
        "frequency":   "weekly",
    },
]


router = APIRouter()


# ---------------------------------------------------------------------------
# Auth request models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "user"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/register")
def register(request: RegisterRequest) -> dict:
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (request.email,)
        ).fetchone()
        if existing:
            return JSONResponse(
                status_code=409,
                content=build_error_response("Email already registered"),
            )

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        token_expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        now = datetime.now(timezone.utc).isoformat()

        cursor = conn.execute(
            "INSERT INTO users"
            " (name, email, password_hash, role, is_active, is_verified,"
            "  verification_token_hash, verification_token_expires_at, created_at)"
            " VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?)",
            (
                request.name,
                request.email,
                hash_password(request.password),
                request.role,
                token_hash,
                token_expires_at,
                now,
            ),
        )
        conn.commit()
        user_id = cursor.lastrowid
    finally:
        conn.close()

    send_verification_email(request.email, raw_token)

    token = create_access_token({"sub": str(user_id), "email": request.email, "role": request.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "name": request.name,
            "email": request.email,
            "role": request.role,
        },
    }


@router.post("/auth/login")
def login(request: LoginRequest) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, email, password_hash, role, is_active, is_verified FROM users WHERE email = ?",
            (request.email,),
        ).fetchone()

        if row is None or not verify_password(request.password, row["password_hash"]):
            return JSONResponse(
                status_code=401,
                content=build_error_response("Invalid email or password"),
            )

        if not row["is_active"]:
            return JSONResponse(
                status_code=403,
                content=build_error_response("Account is inactive"),
            )

        if ENABLE_REAL_EMAIL and not row["is_verified"]:
            return JSONResponse(
                status_code=403,
                content=build_error_response("Please verify your email before signing in."),
            )

        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    token = create_access_token({"sub": str(row["id"]), "email": row["email"], "role": row["role"]})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
        },
    }


@router.get("/auth/verify-email")
def verify_email(token: str) -> dict:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, verification_token_expires_at FROM users WHERE verification_token_hash = ?",
            (token_hash,),
        ).fetchone()

        if row is None:
            return JSONResponse(
                status_code=400,
                content=build_error_response("Invalid or already used token"),
            )

        if datetime.fromisoformat(row["verification_token_expires_at"]) < datetime.now(timezone.utc):
            return JSONResponse(
                status_code=400,
                content=build_error_response("Verification link expired"),
            )

        conn.execute(
            "UPDATE users SET is_verified = 1, verification_token_hash = NULL,"
            " verification_token_expires_at = NULL WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    return {"status": "success", "message": "Email verified"}


# ---------------------------------------------------------------------------
# Existing request models
# ---------------------------------------------------------------------------

class InterpretRequest(BaseModel):
    input: str
    dataset_id: int | None = None
    recipient: str | None = None


class WorkflowRunRequest(BaseModel):
    name: str


class CreateWorkflowRequest(BaseModel):
    name: str
    definition: dict


class DeleteMyDataRequest(BaseModel):
    confirm: bool = False


class CreateScheduledWorkflowRequest(BaseModel):
    input_text: str
    dataset_id: int | None = None


@router.post("/interpret")
def interpret(request: InterpretRequest, user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    if not request.input.strip():
        return JSONResponse(status_code=400, content=build_error_response("Input cannot be empty"))
    try:
        return handle_input(request.input, user_id=user.user_id, dataset_id=request.dataset_id, recipient=request.recipient)
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/workflows")
def list_workflows_route(user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    try:
        return {"status": "success", "data": list_workflows(str(user.user_id))}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.delete("/workflows/{workflow_id}")
def delete_workflow_route(workflow_id: int, user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    try:
        deleted = delete_workflow(workflow_id, str(user.user_id))
        if not deleted:
            return JSONResponse(status_code=404, content=build_error_response("Workflow not found"))
        return {"status": "success", "data": {"deleted_id": workflow_id}}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/workflows")
def create_workflow_route(request: CreateWorkflowRequest, user: AuthenticatedUser = Depends(require_auth)) -> dict:
    try:
        workflow_id = create_workflow(request.name, request.definition, str(user.user_id))
        return {
            "status": "success",
            "data": {
                "workflow_id": workflow_id,
                "name": request.name,
            },
        }
    except ValueError as e:
        return JSONResponse(status_code=400, content=build_error_response("Invalid workflow", str(e)))
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/workflows/run")
def run_workflow(request: WorkflowRunRequest, user: AuthenticatedUser = Depends(require_auth)) -> dict:
    try:
        result = run_workflow_by_name(request.name, user_id=user.user_id)
        return format_output(result)
    except ValueError as e:
        return JSONResponse(status_code=404, content=build_error_response("Not found", str(e)))
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/workflows/{workflow_id}/run")
def run_workflow_by_id_route(workflow_id: int, user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    from core.workflows.workflow_runner import run_workflow_by_id
    try:
        result = run_workflow_by_id(workflow_id, user_id=user.user_id)
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
def get_my_data(user: AuthenticatedUser = Depends(require_auth)) -> dict:
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
            "SELECT eh.id, eh.plan_id, eh.workflow_id, eh.trigger_source,"
            " eh.task_type, eh.intent, eh.status,"
            " eh.started_at, eh.finished_at, eh.duration_ms,"
            " eh.step_count, eh.failed_step_id, eh.failed_tool, eh.error_message,"
            " eh.dataset_id,"
            " d.filename  AS dataset_name,"
            " d.row_count AS dataset_row_count"
            " FROM execution_history eh"
            " LEFT JOIN datasets d ON d.id = eh.dataset_id"
            " WHERE eh.user_id = ?"
            " ORDER BY eh.id DESC",
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
                "execution_history": [enrich_execution_record(dict(row)) for row in execution_rows],
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
def insights(user: AuthenticatedUser = Depends(require_auth)) -> dict:
    try:
        return {"status": "success", "data": get_workflow_success_insights(str(user.user_id))}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/recommendations")
def recommendations(user: AuthenticatedUser = Depends(require_auth)) -> dict:
    try:
        return {"status": "success", "data": get_repeated_intent_suggestions(str(user.user_id))}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/usage")
def usage(user: AuthenticatedUser = Depends(require_auth)) -> dict:
    try:
        return {
            "status": "success",
            "data": {
                "total_events": count_usage_events(user.user_id),
                "by_event_type": {
                    "interpret":     count_usage_events(user.user_id, event_type="interpret"),
                    "workflow_run":  count_usage_events(user.user_id, event_type="workflow_run"),
                },
                "recent_events": list_usage_events(user.user_id, limit=10),
            },
        }
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/datasets/upload")
async def upload_dataset(
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    fname_lower = (file.filename or "").lower()
    if not any(fname_lower.endswith(ext) for ext in (".csv", ".xlsx", ".xls")):
        return JSONResponse(
            status_code=400,
            content=build_error_response("Only CSV and Excel files (.csv, .xlsx, .xls) are supported"),
        )
    try:
        contents = await file.read()
        if fname_lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        elif fname_lower.endswith(".xlsx"):
            df = pd.read_excel(io.BytesIO(contents), engine="openpyxl")
        else:
            df = pd.read_excel(io.BytesIO(contents), engine="xlrd")
    except Exception as e:
        reason = str(e)[:300]
        fmt = "CSV" if fname_lower.endswith(".csv") else "Excel"
        return JSONResponse(
            status_code=400,
            content=build_error_response(f"Could not parse {fmt} file: {reason}"),
        )
    def _safe_float(val):
        try:
            v = float(val)
            return None if not math.isfinite(v) else round(v, 4)
        except (TypeError, ValueError, OverflowError):
            return None

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    categorical_cols = df.select_dtypes(exclude="number").columns.tolist()

    numeric_profile: dict = {}
    for col in numeric_cols:
        series = df[col].dropna()
        if len(series) == 0:
            numeric_profile[col] = {"min": None, "max": None, "mean": None, "sum": None}
        else:
            numeric_profile[col] = {
                "min":  _safe_float(series.min()),
                "max":  _safe_float(series.max()),
                "mean": _safe_float(series.mean()),
                "sum":  _safe_float(series.sum()),
            }

    missing_values = {col: int(count) for col, count in df.isnull().sum().items()}

    categorical_profile: dict = {}
    for col in categorical_cols:
        top = df[col].value_counts().head(5)
        categorical_profile[col] = [
            {"value": str(v), "count": int(c)}
            for v, c in zip(top.index, top.values)
        ]

    date_profile = _compute_date_profile(df, numeric_cols, categorical_cols)

    dataset_id = create_dataset_summary(
        user_id=str(user.user_id),
        filename=file.filename,
        row_count=len(df),
        column_count=len(df.columns),
        columns=df.columns.tolist(),
        numeric_profile=numeric_profile,
        missing_values=missing_values,
        categorical_profile=categorical_profile,
        date_profile=date_profile,
    )

    sample_rows = df.head(5).fillna("").to_dict(orient="records")
    return {
        "status": "success",
        "data": {
            "dataset_id": dataset_id,
            "filename": file.filename,
            "row_count": len(df),
            "column_count": len(df.columns),
            "columns": df.columns.tolist(),
            "numeric_columns": numeric_cols,
            "sample_rows": sample_rows,
            "numeric_profile": numeric_profile,
            "missing_values": missing_values,
            "categorical_profile": categorical_profile,
        },
    }


@router.get("/datasets/{dataset_id}")
def get_dataset_detail_route(dataset_id: int, user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    import json as _json
    row = get_dataset_by_id(dataset_id)
    if row is None or str(row.get("user_id", "")) != str(user.user_id):
        return JSONResponse(status_code=404, content=build_error_response("Dataset not found"))
    numeric_profile     = _json.loads(row["numeric_profile_json"])
    missing_values      = _json.loads(row["missing_values_json"])
    categorical_profile = _json.loads(row["categorical_profile_json"])
    return {
        "status": "success",
        "data": {
            "dataset_id":          row["id"],
            "filename":            row["filename"],
            "row_count":           row["row_count"],
            "column_count":        row["column_count"],
            "columns":             _json.loads(row["columns_json"]),
            "numeric_columns":     list(numeric_profile.keys()),
            "numeric_profile":     numeric_profile,
            "missing_values":      missing_values,
            "categorical_profile": categorical_profile,
            "sample_rows":         [],
        },
    }


@router.get("/datasets")
def list_datasets(user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    try:
        datasets = list_datasets_for_user(str(user.user_id))
        return {"status": "success", "data": datasets}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


class PatchDatasetRequest(BaseModel):
    filename: str


@router.patch("/datasets/{dataset_id}")
def rename_dataset_route(dataset_id: int, request: PatchDatasetRequest, user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    filename = request.filename.strip()
    if not filename:
        return JSONResponse(status_code=400, content=build_error_response("filename must not be empty"))
    try:
        updated = rename_dataset(dataset_id, str(user.user_id), filename)
        if not updated:
            return JSONResponse(status_code=404, content=build_error_response("Dataset not found"))
        return {"status": "success", "data": {"dataset_id": dataset_id, "filename": filename}}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.delete("/datasets/{dataset_id}")
def delete_dataset_route(dataset_id: int, user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    try:
        deleted = delete_dataset(dataset_id, str(user.user_id))
        if not deleted:
            return JSONResponse(status_code=404, content=build_error_response("Dataset not found"))
        return {"status": "success", "data": {"deleted_id": dataset_id}}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/scheduled-workflows")
def create_scheduled_workflow_route(
    request: CreateScheduledWorkflowRequest,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    if not request.input_text.strip():
        return JSONResponse(status_code=400, content=build_error_response("input_text cannot be empty"))
    try:
        plan = interpret_task(request.input_text)
        schedule = plan.get("schedule")
        if not schedule or not schedule.get("frequency"):
            return JSONResponse(
                status_code=400,
                content=build_error_response(
                    "No recurring schedule detected. "
                    "Include a frequency such as 'daily', 'weekly', or 'monthly'."
                ),
            )
        frequency = schedule["frequency"]
        day_of_week = plan.get("metadata", {}).get("entities", {}).get("day_of_week")
        task_type = plan.get("task_type", "unknown")
        entry = create_scheduled_workflow(
            user_id=str(user.user_id),
            dataset_id=request.dataset_id,
            input_text=request.input_text,
            task_type=task_type,
            frequency=frequency,
            day_of_week=day_of_week,
        )
        return {"status": "success", "data": entry}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/scheduled-workflows")
def list_scheduled_workflows_route(user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    try:
        return {"status": "success", "data": list_scheduled_workflows(str(user.user_id))}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.patch("/scheduled-workflows/{workflow_id}/pause")
def pause_scheduled_workflow_route(
    workflow_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        updated = pause_scheduled_workflow(workflow_id, str(user.user_id))
        if not updated:
            return JSONResponse(status_code=404, content=build_error_response("Scheduled workflow not found"))
        return {"status": "success", "data": updated}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.patch("/scheduled-workflows/{workflow_id}/resume")
def resume_scheduled_workflow_route(
    workflow_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        updated = resume_scheduled_workflow(workflow_id, str(user.user_id))
        if not updated:
            return JSONResponse(status_code=404, content=build_error_response("Scheduled workflow not found"))
        return {"status": "success", "data": updated}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.delete("/scheduled-workflows/{workflow_id}")
def delete_scheduled_workflow_route(
    workflow_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    try:
        deleted = delete_scheduled_workflow(workflow_id, str(user.user_id))
        if not deleted:
            return JSONResponse(status_code=404, content=build_error_response("Scheduled workflow not found"))
        return {"status": "success", "data": {"deleted_id": workflow_id}}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/schedule-health")
def schedule_health_route(user: AuthenticatedUser = Depends(require_jwt)) -> dict:
    try:
        return {"status": "success", "data": get_schedule_health(str(user.user_id))}
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/executions/{execution_id}/retry")
def retry_execution(
    execution_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    from core.input.input_handler import handle_input
    from core.workflows.workflow_runner import run_workflow_by_id

    row = get_execution_by_id(execution_id, str(user.user_id))
    if row is None:
        return JSONResponse(status_code=404, content=build_error_response("Execution not found"))
    if row["status"] not in ("failed", "unknown"):
        return JSONResponse(
            status_code=400,
            content=build_error_response(
                f"Cannot retry an execution with status '{row['status']}'."
                " Use /rerun for completed executions."
            ),
        )
    try:
        intent = row.get("intent")
        workflow_id = row.get("workflow_id")
        if intent:
            return handle_input(intent, user_id=str(user.user_id))
        if workflow_id:
            data = run_workflow_by_id(workflow_id, user_id=str(user.user_id))
            return {"status": "success", "data": data}
        return JSONResponse(
            status_code=400,
            content=build_error_response("Cannot retry: no intent or workflow to replay."),
        )
    except ValueError as e:
        return JSONResponse(status_code=404, content=build_error_response(str(e)))
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.post("/executions/{execution_id}/rerun")
def rerun_execution(
    execution_id: int,
    user: AuthenticatedUser = Depends(require_jwt),
) -> dict:
    from core.input.input_handler import handle_input
    from core.workflows.workflow_runner import run_workflow_by_id

    row = get_execution_by_id(execution_id, str(user.user_id))
    if row is None:
        return JSONResponse(status_code=404, content=build_error_response("Execution not found"))
    if row["status"] not in ("completed", "success"):
        return JSONResponse(
            status_code=400,
            content=build_error_response(
                f"Cannot re-run an execution with status '{row['status']}'."
                " Use /retry for failed executions."
            ),
        )
    try:
        intent = row.get("intent")
        workflow_id = row.get("workflow_id")
        if intent:
            return handle_input(intent, user_id=str(user.user_id))
        if workflow_id:
            data = run_workflow_by_id(workflow_id, user_id=str(user.user_id))
            return {"status": "success", "data": data}
        return JSONResponse(
            status_code=400,
            content=build_error_response("Cannot re-run: no intent or workflow to replay."),
        )
    except ValueError as e:
        return JSONResponse(status_code=404, content=build_error_response(str(e)))
    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))


@router.get("/workflow-templates")
def workflow_templates_route(user: AuthenticatedUser = Depends(require_auth)) -> dict:
    return {"status": "success", "data": WORKFLOW_TEMPLATES}


# ---------------------------------------------------------------------------
# AI Operational Assistant
# ---------------------------------------------------------------------------

_ALLOWED_CONTEXT_TYPES = frozenset({
    "workflow_health", "schedule_health", "execution", "report",
})

_AI_ASSISTANT_SYSTEM_PROMPT = """\
You are an operational assistant for ToolSmithAI. Explain the provided operational data concisely.

Rules:
- Explain ONLY what the data shows. Do NOT suggest executing workflows, modifying data, \
creating schedules, or deleting anything.
- Be concise and actionable.
- Return exactly this JSON:
{
  "explanation": "<clear plain-English explanation, max 1000 characters>",
  "recommended_actions": ["<user-facing action>"],
  "confidence": "low" | "medium" | "high"
}
- recommended_actions: 1-5 items, each max 200 characters.
- confidence: "high" if definitive, "medium" if partial, "low" if ambiguous.
- Return ONLY the JSON. No markdown. No code fences.
"""


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _build_workflow_health_context(wf: dict) -> str:
    return "\n".join([
        f"Workflow: {wf.get('workflow_name') or ('Workflow #' + str(wf.get('workflow_id')))}",
        f"Health: {wf.get('health', '—')}",
        f"Total runs: {wf.get('total_runs', '—')}",
        f"Success rate: {round((wf.get('success_rate') or 0) * 100)}%",
        f"Consecutive failures: {wf.get('consecutive_failures', 0)}",
        f"Average duration: {_fmt_ms(wf.get('avg_duration_ms'))}",
        f"Last run: {wf.get('last_run') or '—'}",
        f"Recommendation: {wf.get('recommendation', '—')}",
    ])


def _build_schedule_health_context(sh: dict) -> str:
    return "\n".join([
        f"Task: {(sh.get('input_text') or '—')[:200]}",
        f"Frequency: {sh.get('frequency', '—')}",
        f"State: {'Enabled' if sh.get('enabled') else 'Paused'}",
        f"Health: {sh.get('health', '—')}",
        f"Overdue: {sh.get('overdue_label') or 'None'}",
        f"Next run: {sh.get('next_run_at') or '—'}",
        f"Last run: {sh.get('last_run_at') or '—'}",
        f"Last status: {sh.get('last_status') or '—'}",
        f"Recommendation: {sh.get('recommendation', '—')}",
    ])


def _build_execution_context(row: dict) -> str:
    lines = [
        f"Intent: {(row.get('intent') or '—')[:200]}",
        f"Task type: {row.get('task_type') or '—'}",
        f"Status: {row.get('status', '—')}",
        f"Duration: {row.get('duration_label') or '—'}",
        f"Source: {row.get('source_label') or row.get('trigger_source') or '—'}",
        f"Started: {row.get('started_at') or '—'}",
    ]
    if row.get("error_message"):
        lines.append(f"Error: {row['error_message'][:300]}")
    return "\n".join(lines)


def _build_report_sections_context(sections: list) -> str:
    lines: list[str] = []
    for section in sections:
        lines.append(f"{section.get('heading', '')}:")
        for item in (section.get("items") or [])[:4]:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def _deterministic_explain(context_type: str, data: dict) -> dict:
    """Return a safe, useful explanation without any AI call."""
    if context_type == "workflow_health":
        health = data.get("health", "Unknown")
        rec    = data.get("recommendation") or "Review the workflow configuration."
        explanation = f"This workflow is {health}. {rec}"
        actions = ["Review recent execution history for detailed error information."]
        if health in ("Critical", "Warning"):
            actions.append("Check that all required tools and data sources are accessible.")
        if (data.get("consecutive_failures") or 0) >= 2:
            actions.append("Consider pausing this workflow until the root cause is resolved.")

    elif context_type == "schedule_health":
        health = data.get("health", "Unknown")
        rec    = data.get("recommendation") or "Review the schedule configuration."
        explanation = f"This scheduled workflow is {health}. {rec}"
        actions = {
            "Missed":  ["Check if the server was running during the expected execution window.",
                        "Verify the scheduler service is active."],
            "Delayed": ["The scheduler should process this on the next tick.",
                        "Verify the scheduler service is running."],
            "Paused":  ["Resume the schedule when you are ready to restart automatic runs."],
            "Healthy": ["Schedule is operating normally. No action needed."],
        }.get(health, ["Review the schedule configuration."])

    elif context_type == "execution":
        status = data.get("status", "unknown")
        error  = (data.get("error_message") or "")[:200]
        if status == "failed":
            explanation = f"This execution failed.{' Error: ' + error if error else ''}"
            actions = ["Retry from the History tab.",
                       "Ensure required data (e.g. uploaded CSV) is available."]
        elif status in ("completed", "success"):
            explanation = "This execution completed successfully."
            actions = ["Re-run from the History tab if needed."]
        else:
            explanation = f"Execution status: {status}."
            actions = []

    else:  # report
        explanation = (
            "This dataset report was generated from your uploaded CSV file. "
            "Review each section for insights."
        )
        actions = [
            "Check Numeric Insights for key statistics.",
            "Check Missing Data for data quality issues.",
        ]

    return {
        "explanation":         explanation[:1000],
        "recommended_actions": actions[:5],
        "confidence":          "medium",
    }


def _validate_ai_explanation(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("response must be a dict")
    explanation = raw.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise ValueError("explanation must be a non-empty string")
    actions = raw.get("recommended_actions", [])
    if not isinstance(actions, list):
        raise ValueError("recommended_actions must be a list")
    confidence = raw.get("confidence", "medium")
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"
    return {
        "explanation":         explanation.strip()[:1000],
        "recommended_actions": [str(a).strip()[:200] for a in actions[:5] if str(a).strip()],
        "confidence":          confidence,
    }


def _ai_explain_context(context_summary: str, question: str | None) -> dict | None:
    """
    Ask the AI assistant to explain the provided operational context.
    The AI receives only sanitized text — no raw DB rows, secrets, or credentials.
    Returns None on any failure; caller always falls back to _deterministic_explain.
    """
    import json as _json
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    from core.config import (
        ENABLE_AI_ASSISTANT, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS,
    )
    if not ENABLE_AI_ASSISTANT or not OPENAI_API_KEY:
        return None

    try:
        import openai as _openai
    except ImportError:
        _logger.warning("[ai_assistant] openai not installed; using deterministic fallback")
        return None

    user_msg = context_summary
    if question:
        user_msg += f"\n\nQuestion: {question[:500]}"

    try:
        client = _openai.OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
        resp   = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _AI_ASSISTANT_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=500,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        raw     = _json.loads(content.strip())
        result  = _validate_ai_explanation(raw)
        _logger.info("[ai_assistant] success: confidence=%s", result["confidence"])
        return result
    except Exception as exc:
        _logger.warning(
            "[ai_assistant] failed (%s: %s); using deterministic fallback",
            type(exc).__name__, exc,
        )
        return None


class ExplainRequest(BaseModel):
    context_type: str
    context_id:   int | None = None
    question:     str | None = None


@router.post("/assistant/explain")
def assistant_explain(
    request: ExplainRequest,
    user:    AuthenticatedUser = Depends(require_jwt),
) -> dict:
    if request.context_type not in _ALLOWED_CONTEXT_TYPES:
        return JSONResponse(
            status_code=400,
            content=build_error_response(
                f"context_type must be one of: {', '.join(sorted(_ALLOWED_CONTEXT_TYPES))}"
            ),
        )

    context_data:    dict = {}
    context_summary: str  = ""

    try:
        if request.context_type == "workflow_health":
            rows   = get_workflow_success_insights(str(user.user_id))
            target = next((r for r in rows if r["workflow_id"] == request.context_id), None)
            if target is None:
                return JSONResponse(status_code=404, content=build_error_response("Workflow not found"))
            context_data    = target
            context_summary = _build_workflow_health_context(target)

        elif request.context_type == "schedule_health":
            rows   = get_schedule_health(str(user.user_id))
            target = next((r for r in rows if r["id"] == request.context_id), None)
            if target is None:
                return JSONResponse(status_code=404, content=build_error_response("Scheduled workflow not found"))
            context_data    = target
            context_summary = _build_schedule_health_context(target)

        elif request.context_type == "execution":
            row = get_execution_by_id(request.context_id, str(user.user_id))
            if row is None:
                return JSONResponse(status_code=404, content=build_error_response("Execution not found"))
            row             = enrich_execution_record(row)
            context_data    = row
            context_summary = _build_execution_context(row)

        elif request.context_type == "report":
            from data.dataset_service import get_dataset_by_id
            from core.tools.report_generator import generate_dataset_report
            if request.context_id is None:
                return JSONResponse(status_code=400, content=build_error_response("context_id required for report"))
            dataset = get_dataset_by_id(request.context_id)
            if dataset is None or str(dataset.get("user_id", "")) != str(user.user_id):
                return JSONResponse(status_code=404, content=build_error_response("Dataset not found"))
            report          = generate_dataset_report(dataset)
            context_data    = {"dataset_id": request.context_id, "filename": dataset.get("filename")}
            context_summary = _build_report_sections_context(report.get("sections", []))

    except Exception as e:
        return JSONResponse(status_code=500, content=build_error_response("Internal server error", str(e)))

    ai_result = _ai_explain_context(context_summary, request.question)
    if ai_result:
        return {"status": "success", "data": {**ai_result, "source": "ai"}}

    fallback = _deterministic_explain(request.context_type, context_data)
    return {"status": "success", "data": {**fallback, "source": "deterministic"}}


@router.get("/health")
def health() -> dict:
    return {"status": "healthy"}
