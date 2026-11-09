"""
RBAC hook stub for the Dynamic Tool Creation Engine.

Future responsibility:
  Enforce role-based access control at each lifecycle boundary.

  before_plan  — verify the requesting user is allowed to plan this category
                 of tool (e.g. only 'analyst' role may create report tools).
  after_plan   — attach allowed_roles / allowed_users from the org's policy
                 store to tool_def.rbac before it is persisted.
  before_run   — check that the executing user holds a role listed in
                 tool_def.rbac.allowed_roles or is in allowed_users.
                 Raise PermissionError to abort the run if not.
  after_run    — emit an access-audit event to the audit log.
  on_failure   — log the failed access attempt if failure was permission-related.

When implemented, this hook will read from the tenant's RBAC policy store
(role definitions, user-role assignments) and enforce least-privilege.
It must remain stateless between calls; all state lives in the policy store.

Currently a no-op — no permissions are enforced yet.
"""

from __future__ import annotations

from core.engine.contracts import ExecutionContext, RunRecord, ToolDefinition
from core.engine.hooks.base import EngineHook


class RBACHook(EngineHook):
    """
    Role-based access control hook.

    All methods are no-ops until the RBAC policy store is integrated.
    Override individual lifecycle methods to add permission checks without
    touching any other part of the engine.
    """

    def before_plan(self, intent: str, context: dict) -> None:
        """Future: verify requesting user may create tools of this type."""

    def after_plan(self, tool_def: ToolDefinition, context: dict) -> None:
        """Future: populate tool_def.rbac from the org's policy store."""

    def before_run(self, tool_def: ToolDefinition, context: ExecutionContext) -> None:
        """Future: assert executing user holds a role in tool_def.rbac.allowed_roles."""

    def after_run(self, run_record: RunRecord, context: ExecutionContext) -> None:
        """Future: emit access-audit event."""

    def on_failure(self, error: Exception, context: ExecutionContext) -> None:
        """Future: log failed permission attempt if error is permission-related."""
