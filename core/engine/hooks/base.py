"""
Base hook interface for the Dynamic Tool Creation Engine.

Hooks are the only sanctioned extension points for cross-cutting concerns
(RBAC, AI enrichment, scheduling, observability, etc.).

All methods are no-ops by default. Subclasses override only what they need.
The runtime calls each lifecycle method in order; a hook must never raise
unless it intends to abort the operation it is guarding.

Imports only from stdlib and core.engine.contracts.
No side effects.
"""

from __future__ import annotations

from core.engine.contracts import ExecutionContext, RunRecord, ToolDefinition


class EngineHook:
    """
    Lifecycle hook base class for the Dynamic Tool Creation Engine.

    Lifecycle order for a plan + run cycle:
      1. before_plan  — intent received, planning is about to start
      2. after_plan   — ToolDefinition built, not yet persisted or executed
      3. before_run   — approved tool is about to execute
      4. after_run    — execution finished (completed or failed)
      5. on_failure   — called when a node or full run fails

    All methods are no-ops. Subclasses override what they need.
    """

    def before_plan(self, intent: str, context: dict) -> None:
        """Called before the planner processes a user intent string."""

    def after_plan(self, tool_def: ToolDefinition, context: dict) -> None:
        """Called after a ToolDefinition is planned but before it is saved or executed."""

    def before_run(self, tool_def: ToolDefinition, context: ExecutionContext) -> None:
        """Called immediately before the runtime begins executing a tool graph."""

    def after_run(self, run_record: RunRecord, context: ExecutionContext) -> None:
        """Called after the runtime finishes executing a tool graph (any outcome)."""

    def on_failure(self, error: Exception, context: ExecutionContext) -> None:
        """Called when a node execution or overall run fails."""
