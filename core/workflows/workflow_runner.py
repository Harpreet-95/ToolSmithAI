import datetime
import uuid

from data.workflow_service import get_workflow_by_id, get_workflow_by_name, ALLOWED_MULTI_STEP_TYPES
from core.execution.execution_engine import run_plan
from data.execution_history import log_execution_history
from data.usage_service import log_usage_event


def run_dataset_report_plan(plan: dict, user_id: str | None, dataset_id: int | None = None) -> dict:
    from data.dataset_service import get_latest_dataset_for_user, get_dataset_by_id
    from core.tools.report_generator import generate_dataset_report

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    base = {
        "plan_id": plan["plan_id"],
        "status": "completed",
        "task_type": "generate_dataset_report",
        "started_at": now,
        "finished_at": now,
        "step_results": [],
        "error": None,
    }

    if user_id is None:
        return {**base, "dataset_report": None,
                "dataset_report_error": "Authentication required to access dataset."}

    if dataset_id is not None:
        dataset = get_dataset_by_id(dataset_id)
        if dataset and str(dataset["user_id"]) != str(user_id):
            dataset = None
    else:
        dataset = get_latest_dataset_for_user(user_id)

    if dataset is None:
        return {**base, "dataset_report": None,
                "dataset_report_error": "No uploaded dataset found. Please upload a CSV file first."}

    report = generate_dataset_report(dataset)

    report_id = None
    report_save_warning = None
    try:
        from data.report_service import save_report
        title = "{} — {}".format(
            dataset["filename"],
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        )
        report_id = save_report(
            user_id=user_id,
            title=title,
            task_type="generate_dataset_report",
            content=report,
            status="completed",
            dataset_id=dataset["id"],
        )
    except Exception as exc:
        report_save_warning = f"Report generated but could not be saved: {exc}"

    return {
        **base,
        "dataset_report": report,
        "dataset_report_error": None,
        "report_id": report_id,
        "report_save_warning": report_save_warning,
    }


def run_email_dataset_report_plan(plan: dict, user_id: str | None, dataset_id: int | None = None, ctx: dict | None = None, recipient: str | None = None) -> dict:
    from data.dataset_service import get_latest_dataset_for_user, get_dataset_by_id, get_user_email
    from core.tools.report_generator import generate_dataset_report, format_report_as_email_body
    from core.email import send_real_email

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    base = {
        "plan_id": plan["plan_id"],
        "status": "completed",
        "task_type": "email_dataset_report",
        "started_at": now,
        "finished_at": now,
        "step_results": [],
        "error": None,
    }

    if user_id is None:
        return {**base, "dataset_report": None, "email_delivery": None,
                "dataset_report_error": "Authentication required."}

    if dataset_id is not None:
        dataset = get_dataset_by_id(dataset_id)
        if dataset and str(dataset["user_id"]) != str(user_id):
            dataset = None
    else:
        dataset = get_latest_dataset_for_user(user_id)

    if dataset is None:
        return {**base, "dataset_report": None, "email_delivery": None,
                "dataset_report_error": "No uploaded dataset found. Please upload a CSV file first."}

    # Reuse report from context if a prior step already generated it;
    # otherwise generate fresh — this keeps the step self-contained when run standalone.
    report_from_ctx = bool((ctx or {}).get("dataset_report"))
    report = (ctx or {}).get("dataset_report") or generate_dataset_report(dataset)
    body = format_report_as_email_body(report, dataset["filename"])
    subject = f"Dataset Report — {dataset['filename']}"

    to_address = recipient or get_user_email(user_id)
    if to_address is None:
        email_delivery = {
            "sent": False,
            "reason": "No recipient email address. Please provide a recipient or ensure your account has an email.",
        }
    else:
        email_delivery = send_real_email(to=to_address, subject=subject, body=body)

    # Only save when the report was freshly generated — if it came from ctx it was
    # already saved by the preceding generate_dataset_report step.
    report_id = None
    report_save_warning = None
    if not report_from_ctx:
        try:
            from data.report_service import save_report
            title = "{} — {}".format(
                dataset["filename"],
                datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
            )
            report_id = save_report(
                user_id=user_id,
                title=title,
                task_type="email_dataset_report",
                content=report,
                status="completed",
                dataset_id=dataset["id"],
            )
        except Exception as exc:
            report_save_warning = f"Report generated but could not be saved: {exc}"

    return {
        **base,
        "dataset_report": report,
        "dataset_report_error": None,
        "email_delivery": email_delivery,
        "report_id": report_id,
        "report_save_warning": report_save_warning,
    }


def run_analyze_dataset_plan(plan: dict, user_id: str | None, dataset_id: int | None = None) -> dict:
    from data.dataset_service import get_latest_dataset_for_user, get_dataset_by_id
    from core.tools.report_generator import generate_dataset_report

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    base = {
        "plan_id": plan["plan_id"],
        "status": "completed",
        "task_type": "analyze_dataset",
        "started_at": now,
        "finished_at": now,
        "step_results": [],
        "error": None,
    }

    if user_id is None:
        return {**base, "dataset_analysis": None,
                "dataset_analysis_error": "Authentication required to access dataset."}

    if dataset_id is not None:
        dataset = get_dataset_by_id(dataset_id)
        if dataset and str(dataset["user_id"]) != str(user_id):
            dataset = None
    else:
        dataset = get_latest_dataset_for_user(user_id)

    if dataset is None:
        return {**base, "dataset_analysis": None,
                "dataset_analysis_error": "No uploaded dataset found. Please upload a CSV file first."}

    return {**base, "dataset_analysis": generate_dataset_report(dataset),
            "dataset_analysis_error": None}


def run_send_notification_plan(plan: dict, user_id: str | None, dataset_id: int | None = None) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "plan_id": plan["plan_id"],
        "status": "completed",
        "task_type": "send_notification",
        "started_at": now,
        "finished_at": now,
        "step_results": [],
        "notification_sent": True,
        "channel": "in_app",
        "message": f"Workflow step completed: {plan.get('intent', 'send_notification')}",
        "error": None,
    }


_STEP_RUNNERS = {
    "generate_dataset_report": run_dataset_report_plan,
    "analyze_dataset":         run_analyze_dataset_plan,
    "send_notification":       run_send_notification_plan,
    # email_dataset_report is dispatched separately so ctx can be forwarded
}


def _update_context(ctx: dict, step_type: str, result: dict) -> None:
    """Store step outputs that downstream steps may reuse."""
    if step_type == "analyze_dataset":
        ctx["dataset_analysis"] = result.get("dataset_analysis")
    elif step_type == "generate_dataset_report":
        ctx["dataset_report"] = result.get("dataset_report")


def _mark_remaining_skipped(step_statuses: list, from_index: int) -> None:
    for j in range(from_index, len(step_statuses)):
        step_statuses[j]["status"] = "skipped"


def run_multi_step_workflow(workflow: dict, user_id: str | None = None) -> dict:
    workflow_steps = workflow["definition"]["workflow_steps"]
    plan_id = str(uuid.uuid4())
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    step_statuses = [
        {
            "step_id": s.get("id", str(i + 1)),
            "type":    s["type"],
            "label":   s.get("label", s["type"]),
            "status":  "pending",
        }
        for i, s in enumerate(workflow_steps)
    ]

    completed_results: dict = {}
    ctx: dict = {}
    final_status = "completed"
    error_msg = None

    for i, step_def in enumerate(workflow_steps):
        step_id   = step_def.get("id", str(i + 1))
        step_type = step_def["type"]

        if step_type not in ALLOWED_MULTI_STEP_TYPES:
            step_statuses[i]["status"] = "failed"
            final_status = "failed"
            error_msg = f"Unknown step type: '{step_type}'"
            _mark_remaining_skipped(step_statuses, i + 1)
            break

        step_statuses[i]["status"] = "running"
        step_plan = {
            "plan_id":   plan_id,
            "intent":    step_def.get("label", step_type),
            "task_type": step_type,
            "steps":     [],
        }

        try:
            if step_type == "email_dataset_report":
                result = run_email_dataset_report_plan(step_plan, user_id=user_id, ctx=ctx)
            else:
                result = _STEP_RUNNERS[step_type](step_plan, user_id=user_id)

            if result.get("status") in ("failed", "error"):
                step_statuses[i]["status"] = "failed"
                completed_results[step_id]  = result
                final_status = "failed"
                error_msg = result.get("error") or f"Step '{step_type}' failed"
                _mark_remaining_skipped(step_statuses, i + 1)
                break

            step_statuses[i]["status"] = "completed"
            completed_results[step_id] = result
            _update_context(ctx, step_type, result)

        except Exception as exc:
            step_statuses[i]["status"] = "failed"
            final_status = "failed"
            error_msg = str(exc)
            _mark_remaining_skipped(step_statuses, i + 1)
            break

    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # step_results as list — compatible with log_execution_history._find_failed_step
    step_results_list = [
        {
            "step_id": s["step_id"],
            "tool":    s["type"],
            "status":  s["status"],
            "result":  completed_results.get(s["step_id"]),
        }
        for s in step_statuses
    ]

    return {
        "plan_id":        plan_id,
        "status":         final_status,
        "task_type":      "multi_step",
        "started_at":     started_at,
        "finished_at":    finished_at,
        "workflow_steps": step_statuses,
        "step_results":   step_results_list,
        "error":          error_msg,
    }


def _build_plan(workflow: dict) -> dict:
    return {
        "plan_id": str(uuid.uuid4()),
        "intent": workflow["name"],
        "steps": workflow["definition"]["steps"],
    }


def _log_and_charge(plan: dict, result: dict, workflow_id: int, user_id: str | None) -> None:
    log_execution_history(plan, result, workflow_id=workflow_id, trigger_source="workflow_api", user_id=user_id)
    if user_id is not None:
        log_usage_event(user_id, "workflow_run", "workflow_api", reference_id=str(workflow_id))


def run_workflow_by_name(name: str, user_id: str | None = None) -> dict:
    workflow = get_workflow_by_name(name, user_id=user_id)
    if workflow is None:
        raise ValueError(f"No workflow found with name: '{name}'")

    if workflow["definition"].get("workflow_steps"):
        result = run_multi_step_workflow(workflow, user_id=user_id)
        plan = {"plan_id": result["plan_id"], "intent": workflow["name"],
                "task_type": "multi_step", "steps": workflow["definition"]["workflow_steps"]}
        _log_and_charge(plan, result, workflow["id"], user_id)
        return result

    intent = workflow["definition"].get("intent")
    if intent:
        from core.input.input_handler import handle_input
        return handle_input(intent, user_id=user_id)["data"]

    plan = _build_plan(workflow)
    result = run_plan(plan)
    _log_and_charge(plan, result, workflow["id"], user_id)
    return result


def run_workflow_by_id(workflow_id: int, user_id: str | None = None) -> dict:
    workflow = get_workflow_by_id(workflow_id)
    if workflow is None:
        raise ValueError(f"No workflow found with id: {workflow_id}")

    if workflow["definition"].get("workflow_steps"):
        result = run_multi_step_workflow(workflow, user_id=user_id)
        plan = {"plan_id": result["plan_id"], "intent": workflow["name"],
                "task_type": "multi_step", "steps": workflow["definition"]["workflow_steps"]}
        _log_and_charge(plan, result, workflow["id"], user_id)
        return result

    plan = _build_plan(workflow)
    result = run_plan(plan)
    _log_and_charge(plan, result, workflow["id"], user_id)
    return result
