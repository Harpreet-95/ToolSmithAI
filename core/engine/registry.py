"""
Action handler registry for the Dynamic Tool Creation Engine.

Maps action_type strings to handler objects. The runtime dispatches each
graph node through dispatch_action().

Handler contract — two supported forms:
  callable:   handler(params, context) -> ActionResult | dict
  object:     handler.execute(params, context) -> ActionResult | dict

If a handler returns a plain dict it is automatically wrapped as
  ActionResult(status=COMPLETED, output=<the dict>)
so handlers do not need to import ActionResult themselves.

If a handler raises, the exception propagates to the runtime's retry loop so
the node's failure-mode policy applies.

Default handlers are registered at module import via register_default_handlers().
Custom handlers registered after import take precedence for the same action_type.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core.engine.contracts import (
    ActionHandlerNotFoundError,
    ActionResult,
    ActionStatus,
    ExecutionContext,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry implementation
# ---------------------------------------------------------------------------

class _HandlerRegistry:
    """
    Internal action_type → handler store.

    Supports two handler forms:
      - plain callable:    called as handler(params, context)
      - object with .execute: called as handler.execute(params, context)
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, Any] = {}

    def register(self, action_type: str, handler: Any) -> None:
        self._handlers[action_type] = handler
        logger.debug("Registered handler for action_type '%s'", action_type)

    def get(self, action_type: str) -> Any:
        return self._handlers.get(action_type)

    def has(self, action_type: str) -> bool:
        return action_type in self._handlers

    def clear(self) -> None:
        self._handlers.clear()
        logger.debug("All handlers cleared")

    def dispatch(
        self,
        action_type: str,
        params: Dict[str, Any],
        ctx: ExecutionContext,
    ) -> ActionResult:
        handler = self._handlers.get(action_type)
        if handler is None:
            raise ActionHandlerNotFoundError(action_type)

        # Invoke: prefer callable, fall back to .execute protocol.
        if callable(handler):
            raw = handler(params, ctx)
        elif hasattr(handler, "execute"):
            raw = handler.execute(params, ctx)
        else:
            raise ActionHandlerNotFoundError(action_type)

        # Normalise dict → ActionResult so handlers don't need to import it.
        if isinstance(raw, dict):
            return ActionResult(status=ActionStatus.COMPLETED, output=raw)

        return raw


_REGISTRY = _HandlerRegistry()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_handler(action_type: str, handler: Any) -> None:
    """
    Register a handler for action_type.

    Called once at app startup. Replaces any previously registered handler
    for the same action_type without error.
    """
    _REGISTRY.register(action_type, handler)


def get_handler(action_type: str) -> Any:
    """Return the registered handler for action_type, or None if not found."""
    return _REGISTRY.get(action_type)


def has_handler(action_type: str) -> bool:
    """Return True if a handler is registered for action_type."""
    return _REGISTRY.has(action_type)


def dispatch_action(
    action_type: str,
    params: Dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    """
    Dispatch params and context to the handler registered for action_type.

    Raises ActionHandlerNotFoundError if no handler is registered.
    All other exceptions propagate to the runtime's retry loop so the
    node's failure-mode policy applies.
    """
    return _REGISTRY.dispatch(action_type, params, ctx)


def clear_handlers() -> None:
    """
    Remove all registered handlers.

    Intended for local development and isolated test resets only. Not for
    production use. Call register_default_handlers() afterwards to restore
    the built-in set.
    """
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Default handler registration
# ---------------------------------------------------------------------------

def _make_primitive_wrapper(fn: Any) -> Any:
    """
    Factory that wraps a core.primitives.executor handler for the engine registry.

    Primitive functions take (config, params). In the engine, ActionNode.params
    holds all configuration and resolved runtime values in a single dict, so we
    pass it as both arguments.
    """
    def _handler(params: Dict[str, Any], ctx: ExecutionContext) -> Any:
        return fn(params, params)
    return _handler


def register_default_handlers() -> None:
    """
    Register built-in action handlers for all supported primitive and workflow types.

    Primitives — thin wrappers over core.primitives.executor:
      http_request      — HTTP GET/POST with SSRF protection
      transform_json    — dot-path field extraction/rename
      send_email        — SMTP email (or simulated when ENABLE_REAL_EMAIL=false)
      send_notification — in-app notification (simulated in v1)
      format_output     — safe {identifier}-only template rendering

    Workflow actions:
      generate_dataset_report — loads dataset from DB, generates report

    Utility:
      manual_review_required  — safe SKIPPED no-op; signals incomplete node config

    Safe to call multiple times — re-registration replaces entries silently.
    After clear_handlers(), call this again to restore all defaults.
    No external service calls or DB access happen during registration itself.
    """
    from core.primitives.executor import _PRIMITIVE_HANDLERS

    for action_type, prim_fn in _PRIMITIVE_HANDLERS.items():
        _REGISTRY.register(action_type, _make_primitive_wrapper(prim_fn))

    def _generate_dataset_report(params: Dict[str, Any], ctx: ExecutionContext) -> Any:
        # Prefer explicit param, fall back to run-level input.
        dataset_id = params.get("dataset_id") or ctx.inputs.get("dataset_id")
        if not dataset_id:
            return ActionResult(
                status=ActionStatus.COMPLETED,
                output={
                    "message": (
                        "generate_dataset_report: no dataset_id in params or inputs; "
                        "step completed with no output."
                    )
                },
            )
        try:
            from data.dataset_service import get_dataset_by_id
            from core.tools.report_generator import generate_dataset_report as _gen_report
            dataset = get_dataset_by_id(int(dataset_id))
            if dataset is None:
                return ActionResult(
                    status=ActionStatus.FAILED,
                    error=f"Dataset '{dataset_id}' not found.",
                )
            # Forward intent so chart/KPI selection stays business-aware.
            intent_text = ctx.inputs.get("intent_text") or ""
            report = _gen_report(dataset, intent_text=intent_text)
            return {
                "dataset_id":    int(dataset_id),
                "section_count": len(report.get("sections", [])),
                "report":        report,
            }
        except Exception as exc:
            return ActionResult(status=ActionStatus.FAILED, error=str(exc))

    _REGISTRY.register("generate_dataset_report", _generate_dataset_report)

    def _manual_review_required(params: Dict[str, Any], ctx: ExecutionContext) -> ActionResult:
        return ActionResult(
            status=ActionStatus.SKIPPED,
            output={
                "message": (
                    "This node is marked 'manual_review_required'. "
                    "Set a concrete action_type and configure params "
                    "before approving this tool for execution."
                )
            },
        )

    _REGISTRY.register("manual_review_required", _manual_review_required)

    logger.debug(
        "register_default_handlers: %d handlers registered: %s",
        len(_REGISTRY._handlers),
        sorted(_REGISTRY._handlers.keys()),
    )


# Register defaults at module import so the engine is ready without any
# explicit startup call. Custom handlers registered later take precedence.
register_default_handlers()
