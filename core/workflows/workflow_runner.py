import datetime
import re
import uuid

from data.workflow_service import get_workflow_by_id, get_workflow_by_name, ALLOWED_MULTI_STEP_TYPES
from core.execution.execution_engine import run_plan
from data.execution_history import log_execution_history
from data.usage_service import log_usage_event


def run_dataset_report_plan(plan: dict, user_id: str | None, dataset_id: int | None = None, selected_sections: list[str] | None = None) -> dict:
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

    # Load previous snapshot and baseline window BEFORE generating the report.
    # Both reads happen before any write so the current report can never appear
    # in its own comparison or drift baseline (timing safety).
    previous_snapshot = None
    try:
        from data.report_metric_snapshot_service import get_previous_snapshot_for_dataset
        previous_snapshot = get_previous_snapshot_for_dataset(
            user_id=user_id,
            dataset_id=dataset["id"],
        )
    except Exception:
        pass

    baseline_snapshots: list = []
    try:
        from data.report_metric_snapshot_service import get_snapshot_baseline_for_dataset
        baseline_snapshots = get_snapshot_baseline_for_dataset(
            user_id=user_id,
            dataset_id=dataset["id"],
            limit=10,
        )
    except Exception:
        pass

    report = generate_dataset_report(
        dataset,
        previous_snapshot=previous_snapshot,
        baseline_snapshots=baseline_snapshots,
        selected_sections=selected_sections,
        intent_text=plan.get("intent") or "",
    )

    report_id = None
    report_save_warning = None
    try:
        from data.report_service import save_report
        try:
            from core.intelligence.report_planner import generate_report_title as _gen_title
            _rp = report.get("report_plan") or {}
            title = _gen_title(
                intent_text=plan.get("intent") or "",
                report_style=_rp.get("report_style"),
                dataset_signals=_rp.get("dataset_signals"),
                dataset_filename=dataset["filename"],
            )
        except Exception:
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
        try:
            from data.notification_service import create_notification
            create_notification(
                user_id=user_id,
                title="Report saved",
                message=f"'{title}' was generated and saved.",
                type="report",
                status="success",
                related_report_id=report_id,
            )
        except Exception:
            pass
        try:
            from data.report_metric_snapshot_service import save_report_metric_snapshot
            save_report_metric_snapshot(
                user_id=user_id,
                report_id=report_id,
                dataset_id=dataset["id"],
                task_type="generate_dataset_report",
                report_content=report,
            )
        except Exception:
            pass
    except Exception as exc:
        report_save_warning = f"Report generated but could not be saved: {exc}"

    return {
        **base,
        "dataset_report": report,
        "dataset_report_error": None,
        "report_id": report_id,
        "report_save_warning": report_save_warning,
    }


def run_email_dataset_report_plan(plan: dict, user_id: str | None, dataset_id: int | None = None, ctx: dict | None = None, recipient: str | None = None, selected_sections: list[str] | None = None) -> dict:
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
    if report_from_ctx:
        report = ctx["dataset_report"]
    else:
        previous_snapshot = None
        try:
            from data.report_metric_snapshot_service import get_previous_snapshot_for_dataset
            previous_snapshot = get_previous_snapshot_for_dataset(
                user_id=user_id,
                dataset_id=dataset["id"],
            )
        except Exception:
            pass

        baseline_snapshots: list = []
        try:
            from data.report_metric_snapshot_service import get_snapshot_baseline_for_dataset
            baseline_snapshots = get_snapshot_baseline_for_dataset(
                user_id=user_id,
                dataset_id=dataset["id"],
                limit=10,
            )
        except Exception:
            pass

        report = generate_dataset_report(
            dataset,
            previous_snapshot=previous_snapshot,
            baseline_snapshots=baseline_snapshots,
            selected_sections=selected_sections,
            intent_text=plan.get("intent") or "",
        )
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
            try:
                from core.intelligence.report_planner import generate_report_title as _gen_title
                _rp = report.get("report_plan") or {}
                title = _gen_title(
                    intent_text=plan.get("intent") or "",
                    report_style=_rp.get("report_style"),
                    dataset_signals=_rp.get("dataset_signals"),
                    dataset_filename=dataset["filename"],
                )
            except Exception:
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
            try:
                from data.report_metric_snapshot_service import save_report_metric_snapshot
                save_report_metric_snapshot(
                    user_id=user_id,
                    report_id=report_id,
                    dataset_id=dataset["id"],
                    task_type="email_dataset_report",
                    report_content=report,
                )
            except Exception:
                pass
        except Exception as exc:
            report_save_warning = f"Report generated but could not be saved: {exc}"

    if to_address is not None:
        try:
            from data.notification_service import create_notification
            sent = email_delivery.get("sent", False)
            reason = email_delivery.get("reason", "")
            simulated = not sent and "disabled" in reason.lower()
            if sent or simulated:
                create_notification(
                    user_id=user_id,
                    title="Report emailed",
                    message=(
                        f"Report emailed to {to_address} (simulated)." if simulated
                        else f"Report emailed to {to_address}."
                    ),
                    type="email",
                    status="success",
                    related_report_id=report_id,
                )
            else:
                create_notification(
                    user_id=user_id,
                    title="Email delivery failed",
                    message=reason or "Report email could not be sent.",
                    type="email",
                    status="error",
                    related_report_id=report_id,
                )
        except Exception:
            pass

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


def _build_notification_message(ctx: dict, plan_intent: str) -> str:
    """Build a contextual notification message from prior step outputs (Fix 7)."""
    for ctx_key in ("dataset_analysis", "dataset_report"):
        data = ctx.get(ctx_key)
        if not isinstance(data, dict):
            continue
        sections = data.get("sections", [])
        for section in sections:
            if section.get("heading", "").lower() in ("overview", "executive summary"):
                items = section.get("items", [])
                if items:
                    return str(items[0])[:200]
        for section in sections:
            items = section.get("items", [])
            if items:
                return f"{section.get('heading', 'Analysis')}: {str(items[0])[:180]}"
    return plan_intent or "Workflow step completed."


def run_send_notification_plan(plan: dict, user_id: str | None, dataset_id: int | None = None, ctx: dict | None = None) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    message = _build_notification_message(ctx or {}, plan.get("intent", ""))

    notification_id = None
    notification_status = "failed"
    notification_error = None

    if user_id is not None:
        try:
            from data.notification_service import create_notification
            notification_id = create_notification(
                user_id=user_id,
                title="Workflow notification",
                message=message,
                type="workflow",
                status="info",
            )
            notification_status = "created"
        except Exception as exc:
            notification_error = str(exc)

    sent = notification_id is not None

    return {
        "plan_id":             plan["plan_id"],
        "status":              "completed",
        "task_type":           "send_notification",
        "started_at":          now,
        "finished_at":         now,
        "step_results":        [],
        "notification_sent":   sent,
        "notification_id":     notification_id,
        "notification_status": notification_status,
        "channel":             "in_app",
        "message":             message,
        "error":               notification_error if not sent else None,
    }


# ── Compiled patterns (module-level, created once) ───────────────────────────
# Matches {{step_id.field}} or {{step_id.field.subfield}}
_TEMPLATE_RE = re.compile(r'\{\{([A-Za-z0-9_]+)\.([A-Za-z0-9_.]+)\}\}')
# Matches <lhs> == <rhs>  or  <lhs> != <rhs>
_COND_RE = re.compile(r'^(.+?)\s*(==|!=)\s*(.+)$')


def _resolve_template(value: str, step_outputs: dict) -> str:
    """Replace {{step_id.field}} refs with values from step_outputs.

    step_outputs maps step_id -> result dict from that step.
    Unresolved refs (unknown step or missing field) are left as-is.
    No eval or exec — purely string substitution via regex + dict traversal.
    """
    def _sub(m: re.Match) -> str:
        node = step_outputs.get(m.group(1))
        if node is None:
            return m.group(0)
        for part in m.group(2).split("."):
            if not isinstance(node, dict):
                return m.group(0)
            node = node.get(part)
            if node is None:
                return m.group(0)
        return str(node)
    return _TEMPLATE_RE.sub(_sub, value)


def _resolve_params(params: dict, step_outputs: dict) -> dict:
    """Recursively resolve template refs in string param values.

    Non-string values pass through unchanged. Nested dicts are resolved
    recursively. Lists are not traversed (not needed for current step types).
    """
    out: dict = {}
    for k, v in params.items():
        if isinstance(v, str):
            out[k] = _resolve_template(v, step_outputs)
        elif isinstance(v, dict):
            out[k] = _resolve_params(v, step_outputs)
        else:
            out[k] = v
    return out


def _evaluate_condition(condition: str, step_outputs: dict) -> bool:
    """Evaluate a step condition against resolved template values.

    Supported forms (after template substitution):
        <value> == <value>
        <value> != <value>

    Returns True (run the step) when:
    - condition is blank.
    - form is not recognised (fail-open — unknown syntax never silently skips).
    - the comparison holds.
    """
    if not condition or not condition.strip():
        return True
    resolved = _resolve_template(condition.strip(), step_outputs)
    m = _COND_RE.match(resolved)
    if not m:
        return True  # unrecognised form → run the step
    lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
    return lhs == rhs if op == "==" else lhs != rhs


_STEP_RUNNERS = {
    "analyze_dataset": run_analyze_dataset_plan,
    # generate_dataset_report, email_dataset_report, send_notification dispatched separately
    # so ctx, dataset_id, and selected_sections all forward correctly
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


def run_multi_step_workflow(
    workflow: dict,
    user_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    workflow_steps = workflow["definition"]["workflow_steps"]
    dataset_id = workflow["definition"].get("dataset_id")  # Fix 6: forward into step runners
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

    # Maps step_id -> full result dict; used both for ctx chaining and
    # as the template resolution source for {{step_id.field}} references.
    completed_results: dict = {}
    ctx: dict = {}
    final_status = "completed"
    error_msg = None

    for i, step_def in enumerate(workflow_steps):
        step_id   = step_def.get("id", str(i + 1))
        step_type = step_def["type"]

        # Validate step type (applies in both live and dry-run modes).
        if step_type not in ALLOWED_MULTI_STEP_TYPES:
            step_statuses[i]["status"] = "failed"
            step_statuses[i]["error"]  = f"Unknown step type: '{step_type}'"
            final_status = "failed"
            error_msg = f"Unknown step type: '{step_type}'"
            _mark_remaining_skipped(step_statuses, i + 1)
            break

        condition       = step_def.get("condition", "")
        raw_params      = step_def.get("params") or {}
        resolved_params = _resolve_params(raw_params, completed_results)

        # ── Dry-run mode ──────────────────────────────────────────────────────
        if dry_run:
            has_template = bool(_TEMPLATE_RE.search(condition)) if condition else False
            step_statuses[i].update({
                "status":         "would_run",
                "condition":       condition,
                "condition_note":  (
                    "evaluated at runtime — depends on prior step output"
                    if has_template
                    else ("no condition — always runs" if not condition else condition)
                ),
                "resolved_params": resolved_params,
            })
            # Placeholder result lets later steps reference this one in conditions.
            completed_results[step_id] = {"status": "would_run"}
            continue

        # ── Conditional skip ──────────────────────────────────────────────────
        if condition and not _evaluate_condition(condition, completed_results):
            step_statuses[i].update({
                "status":      "skipped",
                "skip_reason": f"condition not met: {condition}",
            })
            # Record a skipped sentinel so downstream conditions can reference it.
            completed_results[step_id] = {"status": "skipped"}
            continue

        # ── Live execution ────────────────────────────────────────────────────
        step_statuses[i]["status"] = "running"
        if resolved_params:
            step_statuses[i]["resolved_params"] = resolved_params

        step_plan = {
            "plan_id":   plan_id,
            "intent":    step_def.get("label", step_type),
            "task_type": step_type,
            "steps":     [],
        }

        try:
            if step_type == "generate_dataset_report":
                result = run_dataset_report_plan(
                    step_plan, user_id=user_id, dataset_id=dataset_id,
                    selected_sections=step_def.get("selected_sections"),
                )
            elif step_type == "email_dataset_report":
                recipient = resolved_params.get("recipient") or step_def.get("recipient")
                result = run_email_dataset_report_plan(
                    step_plan, user_id=user_id, ctx=ctx, recipient=recipient,
                    dataset_id=dataset_id,
                    selected_sections=step_def.get("selected_sections"),
                )
            elif step_type == "send_notification":
                result = run_send_notification_plan(
                    step_plan, user_id=user_id, ctx=ctx, dataset_id=dataset_id,
                )
            else:
                result = _STEP_RUNNERS[step_type](step_plan, user_id=user_id, dataset_id=dataset_id)

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

    # ── Dry-run return (no logging, no notifications) ─────────────────────────
    if dry_run:
        return {
            "plan_id":         plan_id,
            "status":          "dry_run",
            "task_type":       "multi_step",
            "dry_run":         True,
            "dry_run_preview": step_statuses,
            "workflow_steps":  step_statuses,
            "step_results":    [],
            "error":           error_msg,
            "started_at":      started_at,
            "finished_at":     finished_at,
        }

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

    skipped_count = sum(1 for s in step_statuses if s["status"] == "skipped")

    # Orchestration-layer notification — one per execution, created only after
    # final_status is known.  Success notification fires only when all required
    # steps completed; failure notification fires only on failure.
    # The ID is captured so the frontend can confirm delivery without relying
    # solely on explicit send_notification step results.
    # Skipped when an explicit send_notification step already ran (avoid duplicate).
    _has_send_notif_step = any(
        s.get("tool") == "send_notification" for s in step_results_list
    )
    _orchestration_notif_id: int | None = None
    if user_id is not None and not _has_send_notif_step:
        try:
            from data.notification_service import create_notification
            if final_status == "failed":
                _orchestration_notif_id = create_notification(
                    user_id=user_id,
                    title="Workflow failed",
                    message=error_msg or "A workflow step did not complete successfully.",
                    type="workflow",
                    status="error",
                )
            else:
                _orchestration_notif_id = create_notification(
                    user_id=user_id,
                    title="Workflow completed",
                    message="Your workflow completed successfully.",
                    type="workflow",
                    status="success",
                )
        except Exception:
            pass

    # Hoist notification confirmation from send_notification step to top level
    # so the frontend can confirm delivery without traversing step_results.
    # Explicit send_notification step takes precedence; fall back to the
    # orchestration-layer notification created above.
    _notif_result = next(
        (
            s["result"] for s in step_results_list
            if s.get("tool") == "send_notification" and isinstance(s.get("result"), dict)
        ),
        None,
    )

    _notif_sent = _notif_result.get("notification_sent") if _notif_result else (_orchestration_notif_id is not None)
    _notif_id   = _notif_result.get("notification_id")   if _notif_result else _orchestration_notif_id

    return {
        "plan_id":           plan_id,
        "status":            final_status,
        "task_type":         "multi_step",
        "started_at":        started_at,
        "finished_at":       finished_at,
        "workflow_steps":    step_statuses,
        "step_results":      step_results_list,
        "error":             error_msg,
        "skipped_steps":     skipped_count,
        "notification_sent": _notif_sent,
        "notification_id":   _notif_id,
    }


def run_composed_workflow_proposal(
    proposal: dict,
    user_id: str | None,
    dataset_id: int | None = None,
    recipient: str | None = None,
    selected_sections: list[str] | None = None,
) -> dict:
    """Execute a composer workflow proposal using trusted runner paths.

    Single-step proposals call direct runners (preserving selected_sections / recipient).
    Multi-step proposals build a synthetic workflow dict and delegate to run_multi_step_workflow.
    """
    steps = proposal.get("primitives_or_steps", [])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if not steps:
        return {
            "plan_id":      str(uuid.uuid4()),
            "status":       "failed",
            "task_type":    "workflow",
            "started_at":   now,
            "finished_at":  now,
            "step_results": [],
            "error":        "Composer proposal has no executable steps.",
        }

    plan_id = str(uuid.uuid4())
    intent = proposal.get("interpreted_goal", "")

    if len(steps) == 1:
        step = steps[0]
        step_type = step["step_type"]
        step_sections = selected_sections or step.get("selected_sections")
        plan = {"plan_id": plan_id, "intent": intent, "task_type": step_type, "steps": []}

        if step_type == "generate_dataset_report":
            return run_dataset_report_plan(
                plan, user_id, dataset_id=dataset_id, selected_sections=step_sections
            )
        if step_type == "email_dataset_report":
            return run_email_dataset_report_plan(
                plan, user_id, dataset_id=dataset_id, recipient=recipient,
                selected_sections=step_sections,
            )
        if step_type == "analyze_dataset":
            return run_analyze_dataset_plan(plan, user_id, dataset_id=dataset_id)
        if step_type == "send_notification":
            return run_send_notification_plan(plan, user_id, dataset_id=dataset_id)
        return {
            "plan_id": plan_id, "status": "failed", "task_type": step_type,
            "started_at": now, "finished_at": now, "step_results": [],
            "error": f"Unknown step type: {step_type!r}",
        }

    # Multi-step: build synthetic workflow and delegate
    workflow = {
        "name": proposal.get("suggested_name", "composed_workflow"),
        "definition": {
            "workflow_steps": [
                {
                    "type":              s["step_type"],
                    "order":             s["order"],
                    "purpose":           s.get("purpose", ""),
                    "selected_sections": selected_sections or s.get("selected_sections"),
                }
                for s in steps
            ],
            "dataset_id": dataset_id,
            "intent":     intent,
        },
    }
    return run_multi_step_workflow(workflow, user_id=user_id)


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
