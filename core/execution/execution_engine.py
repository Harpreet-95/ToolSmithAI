import datetime
import time

from core.optimization.performance_tracker import PerformanceTracker
from core.optimization.workflow_optimizer import WorkflowOptimizer


# ---------------------------------------------------------------------------
# Simulated tool handlers
# ---------------------------------------------------------------------------

def handle_fetch_report_data(params: dict) -> dict:
    return {
        "source": params.get("source", "sqlite"),
        "table": params.get("table", "unknown"),
        "rows_fetched": 10,
        "message": "Report data fetched successfully (simulated)",
    }


def handle_send_email(params: dict) -> dict:
    return {
        "to": params.get("to"),
        "subject": params.get("subject"),
        "message": "Email sent successfully (simulated)",
    }


def handle_send_notification(params: dict) -> dict:
    return {
        "channel": params.get("channel"),
        "priority": params.get("priority"),
        "message": "Notification delivered successfully (simulated)",
    }


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

DISPATCH_TABLE = {
    "data_fetcher.fetch_report_data": handle_fetch_report_data,
    "email_sender.send_email": handle_send_email,
    "notifier.send_notification": handle_send_notification,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dispatch(step: dict) -> dict:
    key = f"{step['tool']}.{step['operation']}"
    handler = DISPATCH_TABLE.get(key)
    if handler is None:
        raise ValueError(
            f"No handler registered for '{key}'. "
            f"Registered handlers: {list(DISPATCH_TABLE.keys())}"
        )
    return handler(step.get("params") or {})


def _run_step(step: dict) -> dict:
    base = {
        "step_id": step.get("step_id"),
        "tool": step.get("tool"),
        "operation": step.get("operation"),
    }
    try:
        output = _dispatch(step)
        return {**base, "status": "success", "output": output, "error": None}
    except Exception as exc:
        return {**base, "status": "failed", "output": None, "error": str(exc)}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def run_plan(plan: dict) -> dict:
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    step_results = []
    plan_id = plan.get("plan_id")

    tracker = PerformanceTracker()
    tracker.start_timer(plan_id)

    for step in plan.get("steps", []):
        step_id = step.get("step_id")
        step_start = time.perf_counter()
        result = _run_step(step)
        step_elapsed_ms = (time.perf_counter() - step_start) * 1000
        print(f"[PerformanceTracker] step_id='{step_id}' completed in {step_elapsed_ms:.2f} ms", flush=True)
        result["duration_ms"] = step_elapsed_ms
        step_results.append(result)
        if result["status"] == "failed":
            tracker.end_timer(plan_id)
            return {
                "plan_id": plan_id,
                "status": "failed",
                "started_at": started_at,
                "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "step_results": step_results,
                "error": result["error"],
            }

    tracker.end_timer(plan_id)
    WorkflowOptimizer().analyze_steps(step_results)
    return {
        "plan_id": plan_id,
        "status": "completed",
        "started_at": started_at,
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "step_results": step_results,
        "error": None,
    }
