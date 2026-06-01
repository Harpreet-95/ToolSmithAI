import datetime
import uuid

from core.interpreter.task_interpreter import (
    interpret_task,
    _build_dataset_report_plan,
    _build_email_dataset_report_plan,
)
from core.execution.execution_engine import run_plan
from core.output.output_formatter import format_output
from core.workflows.workflow_runner import (
    run_dataset_report_plan,
    run_email_dataset_report_plan,
    run_composed_workflow_proposal,
)
from data.audit import log_audit_event
from data.execution_history import log_execution_history
from data.usage_service import log_usage_event

# These signals intentionally bias business intelligence requests toward dataset
# reporting paths (Path 1 / Path 2) instead of generic workflow execution or
# the legacy interpreter. Any word here with a dataset attached routes directly
# to dataset report generation unless a compound multi-step signal is also present.
_REPORT_HINT_WORDS = frozenset({
    # Core report vocabulary
    "report", "summary", "analyze", "analyse",
    "summarize", "summarise", "insight", "insights",
    "analysis", "analytics", "overview", "data",
    # Business performance
    "revenue", "sales", "profit", "margin", "growth",
    "trend", "trends", "performance", "forecast", "projection",
    "dashboard", "metrics", "kpi", "kpis",
    # Segmentation and ranking
    "breakdown", "segment", "segmentation", "top", "best", "worst",
    # Operations
    "logistics", "shipment", "delay", "delivery", "inventory",
    "warehouse", "operations", "throughput", "sla", "utilization",
    # Risk and compliance
    "risk", "risks", "anomaly", "anomalies", "fraud", "incident",
    "compliance", "churn", "retention", "failure",
    # Business entities
    "product", "products", "customer", "customers",
    "client", "clients", "region", "regions",
    "store", "stores", "category", "categories",
})
_EMAIL_HINT_WORDS = frozenset({"email", "send", "mail", "share"})

# Signals that indicate a multi-step compound intent.
# When present alongside report signals, Path 1 (dataset shortcut) yields to
# the composer bridge so multi-step plans are built correctly.
_MULTI_STEP_SIGNALS: frozenset = frozenset({
    "notify", "notification", "notif", "alert", "ping",
})

# These signals intentionally bias business intelligence requests toward dataset
# reporting paths (Path 2 composer bridge) instead of generic workflow execution.
# Pure notification/email/reminder intents are NOT included — they stay on the
# legacy path. The composer is only canonical for analysis/report/dataset work.
_COMPOSER_ELIGIBLE_SIGNALS: frozenset = frozenset([
    # Core analysis vocabulary
    "report", "analyze", "analyse", "analysis",
    "summarize", "summarise", "summary", "overview",
    "digest", "insights", "intelligence", "breakdown",
    "kpi", "kpis", "dataset", "analytics",
    # Anomaly and monitoring
    "anomaly", "anomalies", "monitor", "drift", "outlier", "spike",
    # Business performance
    "revenue", "sales", "profit", "margin", "growth",
    "trend", "trends", "performance", "forecast", "projection",
    "metrics", "segment", "segmentation",
    # Operations
    "logistics", "shipment", "delay", "delivery", "inventory",
    "warehouse", "operations", "throughput", "sla", "utilization",
    # Risk and compliance
    "risk", "risks", "fraud", "incident", "compliance",
    "churn", "retention", "failure",
    # Business entities
    "product", "products", "customer", "customers",
    "client", "clients", "region", "regions",
    "store", "stores", "category", "categories",
    # Comparative ranking
    "top", "best", "worst",
])


def _is_composer_eligible(lowered: str) -> bool:
    return any(sig in lowered for sig in _COMPOSER_ELIGIBLE_SIGNALS)


def _try_compose(user_input: str, dataset_id: int | None) -> dict | None:
    """Call compose_from_intent safely; returns None on any failure."""
    try:
        from core.composer.intent_composer import compose_from_intent
        return compose_from_intent(user_input, dataset_id=dataset_id)
    except Exception:
        return None


def _dynamic_tool_safe_response(user_input: str) -> dict:
    """Return a non-executing safe response for dynamic_tool proposals (Fix 3)."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "plan_id":        str(uuid.uuid4()),
        "status":         "pending_approval",
        "task_type":      "dynamic_tool_proposal",
        "started_at":     now,
        "finished_at":    now,
        "step_results":   [],
        "error":          None,
        "message": (
            "This request requires a dynamic tool proposal and approval before execution. "
            "Open the AI Workspace to review and approve this proposal."
        ),
        "planner_source": "composer",
        "fallback_used":  False,
        "proposal_type":  "dynamic_tool",
        "executed_steps": [],
        "original_input": user_input,
    }


def _log_result(plan: dict, result: dict, user_id: str | None, dataset_id: int | None) -> None:
    log_audit_event(result, user_id=user_id)
    log_execution_history(
        plan, result,
        workflow_id=None, trigger_source="interpreter",
        user_id=user_id, dataset_id=dataset_id,
    )
    if user_id is not None:
        log_usage_event(user_id, "interpret", "api", reference_id=result.get("plan_id"))


def handle_input(
    user_input: str,
    user_id: str | None = None,
    dataset_id: int | None = None,
    recipient: str | None = None,
    selected_sections: list[str] | None = None,
) -> dict:
    lowered = user_input.lower()

    # ── Path 1: direct dataset shortcuts (preserved, fully tested) ────────────
    # When a dataset is attached and the input is a SIMPLE report/email-report
    # intent (no compound signals), bypass all planners and route directly to
    # the trusted dataset runner.  Compound intents (report + notify) fall
    # through to Path 2 so the composer can build multi-step plans.
    if (
        dataset_id is not None
        and any(w in lowered for w in _REPORT_HINT_WORDS)
        and not any(w in lowered for w in _MULTI_STEP_SIGNALS)
    ):
        if any(w in lowered for w in _EMAIL_HINT_WORDS):
            plan = _build_email_dataset_report_plan(user_input)
            result = run_email_dataset_report_plan(
                plan, user_id, dataset_id=dataset_id, recipient=recipient,
                selected_sections=selected_sections,
            )
        else:
            plan = _build_dataset_report_plan(user_input)
            result = run_dataset_report_plan(
                plan, user_id, dataset_id=dataset_id, selected_sections=selected_sections,
            )
        result["original_input"]  = user_input
        result["planner_source"]  = "legacy_interpreter"
        result["fallback_used"]   = False
        _log_result(plan, result, user_id=user_id, dataset_id=dataset_id)
        return format_output(result)

    # ── Path 2: composer bridge ───────────────────────────────────────────────
    # Only activated for analysis/report/dataset/anomaly-eligible intents.
    # Pure notification, email, or reminder intents stay on the legacy path.
    if _is_composer_eligible(lowered):
        proposal = _try_compose(user_input, dataset_id)

        # Dynamic tool proposals are NEVER auto-executed — return safe preview.
        if proposal is not None and proposal["proposal_type"] == "dynamic_tool":
            result = _dynamic_tool_safe_response(user_input)
            _log_result(
                {"plan_id": result["plan_id"], "intent": user_input,
                 "task_type": "dynamic_tool_proposal", "steps": []},
                result, user_id=user_id, dataset_id=dataset_id,
            )
            return format_output(result)

        # Workflow proposals with at least one step are executed via the composer.
        if (
            proposal is not None
            and proposal["proposal_type"] == "workflow"
            and proposal.get("primitives_or_steps")
        ):
            result = run_composed_workflow_proposal(
                proposal, user_id,
                dataset_id=dataset_id, recipient=recipient,
                selected_sections=selected_sections,
            )
            result["planner_source"]  = "composer"
            result["fallback_used"]   = False
            result["proposal_type"]   = "workflow"
            result["executed_steps"]  = [
                s["step_type"] for s in proposal.get("primitives_or_steps", [])
            ]
            result["original_input"]  = user_input
            composed_plan = {
                "plan_id":   result.get("plan_id", ""),
                "intent":    user_input,
                "task_type": result.get("task_type", "workflow"),
                "steps":     [],
            }
            _log_result(composed_plan, result, user_id=user_id, dataset_id=dataset_id)
            return format_output(result)

    # ── Path 3: legacy interpreter (fallback) ─────────────────────────────────
    # Reached when: (a) intent is not composer-eligible, (b) composer returned
    # no/empty proposal, or (c) proposal had no executable steps.
    plan = interpret_task(user_input)

    if plan.get("task_type") == "generate_dataset_report":
        result = run_dataset_report_plan(
            plan, user_id, dataset_id=dataset_id, selected_sections=selected_sections
        )
    elif plan.get("task_type") == "email_dataset_report":
        result = run_email_dataset_report_plan(
            plan, user_id, dataset_id=dataset_id, recipient=recipient,
            selected_sections=selected_sections,
        )
    else:
        if recipient is not None or plan.get("intent"):
            for step in plan.get("steps", []):
                if step.get("operation") == "send_email":
                    step.setdefault("params", {}).update({
                        "to":        recipient,
                        "intent":    plan.get("intent", ""),
                        "task_type": plan.get("task_type", ""),
                    })
        result = run_plan(plan)

    result["original_input"]  = user_input
    result["planner_source"]  = "legacy_interpreter"
    result["fallback_used"]   = True
    _log_result(plan, result, user_id=user_id, dataset_id=dataset_id)
    return format_output(result)
