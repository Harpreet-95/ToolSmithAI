"""
Runtime executor for the Dynamic Tool Creation Engine.

execute_tool() is the single entry point. It:
  1. Guards execution behind the approval gate.
  2. Validates required inputs against the tool's InputSpec list.
  3. Builds ExecutionContext and walks the topologically sorted graph.
  4. Resolves {{...}} templates per node, dispatches to registered action handlers.
  5. Applies retry and failure-mode policies per node.
  6. Persists the RunRecord via run_store (RUNNING → COMPLETED | FAILED).
  7. Calls EngineHook lifecycle methods at the correct moments.

No real action handlers are registered here. register_handler() is the
extension point — Step 8 (registry.py) calls it at app startup.

No API routes. No planner. No AI / RBAC / scheduling behaviour.
No DB writes except to run_store.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.engine.approval import ensure_tool_approved
from core.engine.contracts import (
    ActionHandlerNotFoundError,
    ActionNode,
    ActionResult,
    ActionStatus,
    EngineError,
    ExecutionContext,
    FailureMode,
    InputSpec,
    RunRecord,
    RunStatus,
    StepResult,
    ToolDefinition,
    resolve_template,
)
from core.engine.graph import get_execution_order
from core.engine.hooks.base import EngineHook
from core.engine.registry import dispatch_action, register_handler  # noqa: F401
from data.engine.run_store import create_run, get_run, update_run_status, update_run_steps

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_inputs(specs: List[InputSpec], inputs: Dict[str, Any]) -> None:
    for spec in specs:
        if spec.required and spec.name not in inputs:
            raise EngineError(f"Missing required input '{spec.name}'")


def _collect_outputs(tool_def: ToolDefinition, ctx: ExecutionContext) -> Dict[str, Any]:
    """Resolve each OutputSpec.source_ref against the final context. Skips failures."""
    ctx_dict = ctx.as_dict()
    outputs: Dict[str, Any] = {}
    for spec in tool_def.outputs:
        if not spec.source_ref:
            continue
        try:
            outputs[spec.name] = resolve_template(spec.source_ref, ctx_dict)
        except Exception as exc:
            logger.warning("Output '%s' could not be resolved: %s", spec.name, exc)
    return outputs


def _run_node(node: ActionNode, ctx: ExecutionContext) -> ActionResult:
    """
    Dispatch one node through the registry, honouring its retry config.

    ActionHandlerNotFoundError is re-raised immediately — retrying will never
    succeed for an unregistered action type.

    All other exceptions are retried up to node.retry.max_attempts times, then
    returned as ActionResult(FAILED) so the caller applies the failure policy.
    """
    last_error = ""
    attempts = max(1, node.retry.max_attempts)

    for attempt in range(attempts):
        try:
            # Resolve params fresh each attempt so accumulated node_outputs are used.
            ctx_dict = ctx.as_dict()
            resolved_params = resolve_template(node.params, ctx_dict)
            for param_key, ref_expr in node.template_refs.items():
                resolved_params[param_key] = resolve_template(ref_expr, ctx_dict)

            t0 = time.monotonic()
            result = dispatch_action(node.action_type, resolved_params, ctx)
            result.duration_ms = int((time.monotonic() - t0) * 1000)
            return result

        except ActionHandlerNotFoundError:
            raise  # not retryable — re-raise immediately

        except Exception as exc:
            last_error = str(exc)
            remaining = attempts - attempt - 1
            if remaining > 0:
                logger.debug(
                    "Node '%s' attempt %d/%d failed (%s) — retrying in %dms",
                    node.id, attempt + 1, attempts, last_error, node.retry.backoff_ms,
                )
                time.sleep(node.retry.backoff_ms / 1_000.0)
            else:
                logger.warning(
                    "Node '%s' exhausted %d attempt(s): %s",
                    node.id, attempts, last_error,
                )

    return ActionResult(status=ActionStatus.FAILED, error=last_error)


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------

def execute_tool(
    tool_def: ToolDefinition,
    inputs: Dict[str, Any],
    user_id: str = "system",
    hooks: Optional[List[EngineHook]] = None,
) -> RunRecord:
    """
    Execute a ToolDefinition and return the final RunRecord.

    Pre-execution failures (unapproved tool, missing required inputs) raise
    before a RunRecord is created — no partial DB state is written.

    Node-level failures are handled per the node's FailureMode policy:
      FAIL_GRAPH     — abort the entire graph; run status → FAILED
      SKIP_NODE      — mark node SKIPPED, continue to remaining nodes
      CONTINUE_GRAPH — mark node FAILED, continue to remaining nodes

    Unexpected runtime errors (bugs, not node failures) update run status to
    FAILED, call on_failure hooks, then re-raise.

    Returns a RunRecord with status COMPLETED or FAILED.
    """
    hooks = hooks or []

    # 1. Approval gate — raises ApprovalRequiredError if tool is not approved.
    ensure_tool_approved(tool_def)

    # 2. Input validation — raises EngineError on any missing required input.
    _validate_inputs(tool_def.inputs, inputs)

    # 3. Build ExecutionContext.
    ctx = ExecutionContext(inputs=inputs)

    # 4. Topological execution order — raises CycleDetectedError or
    #    SchemaValidationError if the graph is invalid.
    ordered_nodes = get_execution_order(tool_def.graph)

    # 5. Persist initial run as RUNNING (before any node fires).
    run_record = RunRecord(
        tool_id=tool_def.id,
        tool_version=tool_def.version,
        trigger_source="manual",
        status=RunStatus.RUNNING,
        inputs=inputs,
        user_id=user_id,
    )
    create_run(run_record)

    # 6. before_run hooks.
    for hook in hooks:
        hook.before_run(tool_def, ctx)

    step_results: List[StepResult] = []
    graph_failed = False
    fail_error = ""

    try:
        # 7. Execute nodes in dependency-safe order.
        for node in ordered_nodes:
            # node.condition is stored but condition evaluation is deferred —
            # a safe evaluator will be added in a later step.

            step_output: Dict[str, Any] = {}
            step_error: Optional[str] = None
            step_status = ActionStatus.COMPLETED
            t_node = time.monotonic()

            try:
                result = _run_node(node, ctx)
                step_status = result.status
                step_output = result.output or {}
                step_error = result.error

                if result.status == ActionStatus.FAILED:
                    if node.on_failure == FailureMode.FAIL_GRAPH:
                        fail_error = result.error or f"Node '{node.id}' failed"
                        graph_failed = True
                    elif node.on_failure == FailureMode.SKIP_NODE:
                        step_status = ActionStatus.SKIPPED
                    # CONTINUE_GRAPH: keep step_status=FAILED, continue loop

            except ActionHandlerNotFoundError as exc:
                step_status = ActionStatus.FAILED
                step_error = str(exc)
                if node.on_failure == FailureMode.FAIL_GRAPH:
                    fail_error = step_error
                    graph_failed = True
                elif node.on_failure == FailureMode.SKIP_NODE:
                    step_status = ActionStatus.SKIPPED
                # CONTINUE_GRAPH: record FAILED, continue loop

            step_results.append(StepResult(
                node_id=node.id,
                action_type=node.action_type,
                status=step_status,
                output=step_output,
                error=step_error,
                duration_ms=int((time.monotonic() - t_node) * 1000),
            ))

            # Store output so downstream nodes can reference this node's results.
            if step_output:
                ctx.node_outputs[node.id] = step_output

            if graph_failed:
                break

        # 8. Persist step results, then finalise run status.
        update_run_steps(run_record.run_id, step_results)
        if graph_failed:
            update_run_status(run_record.run_id, RunStatus.FAILED, error=fail_error)
        else:
            update_run_status(
                run_record.run_id,
                RunStatus.COMPLETED,
                outputs=_collect_outputs(tool_def, ctx),
            )

    except Exception as exc:
        # Unexpected error in the runtime itself — not a node-level failure.
        # Persist whatever step results were collected before the error.
        update_run_steps(run_record.run_id, step_results)
        update_run_status(
            run_record.run_id,
            RunStatus.FAILED,
            error=f"Runtime error: {exc}",
        )
        for hook in hooks:
            hook.on_failure(exc, ctx)
        raise

    # Reload from DB — finished_at, duration_ms, and step_results_json are
    # now all set correctly by their respective update functions above.
    final_record = get_run(run_record.run_id) or run_record

    # 9. after_run — always called, regardless of outcome.
    for hook in hooks:
        hook.after_run(final_record, ctx)

    # 10. on_failure — additionally called when the graph failed.
    if graph_failed:
        for hook in hooks:
            hook.on_failure(EngineError(fail_error), ctx)

    return final_record
