"""
Approval lifecycle state machine for ToolDefinition.

Transitions:
  draft            → pending_approval  (submit_for_approval)
  pending_approval → approved           (approve_tool)
  pending_approval → draft              (reject_tool  — logs REJECTED; no REJECTED ToolStatus)
  approved         → draft              (revoke_tool  — logs REVOKED;  no REVOKED  ToolStatus)
  approved         → deprecated         (deprecate_tool)

Every transition:
  1. Validates the current status (raises EngineError on invalid transition).
  2. Updates the stored status via tool_store.update_tool_status.
  3. Appends an ApprovalEvent via approval_store.log_approval_event.

No RBAC checks. No runtime execution. No scheduling.
"""

import logging

from core.engine.contracts import (
    ApprovalEvent,
    ApprovalEventType,
    ApprovalRequiredError,
    EngineError,
    ToolDefinition,
    ToolStatus,
)
from data.engine.tool_store import get_tool, update_tool_status
from data.engine.approval_store import log_approval_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_or_raise(tool_id: str) -> ToolDefinition:
    tool = get_tool(tool_id)
    if tool is None:
        raise EngineError(f"Tool '{tool_id}' not found")
    return tool


def _emit(
    tool: ToolDefinition,
    event_type: ApprovalEventType,
    actor_id: str,
    notes: str | None,
) -> None:
    log_approval_event(ApprovalEvent(
        tool_id=tool.id,
        tool_version=tool.version,
        event_type=event_type,
        actor_id=actor_id,
        notes=notes,
    ))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit_for_approval(tool_id: str, actor_id: str, notes: str | None = None) -> None:
    """Transition draft → pending_approval. Raises EngineError if not in draft."""
    tool = _load_or_raise(tool_id)
    if tool.status != ToolStatus.DRAFT:
        raise EngineError(
            f"Cannot submit tool '{tool_id}' for approval: "
            f"expected status 'draft', got '{tool.status.value}'"
        )
    update_tool_status(tool_id, ToolStatus.PENDING_APPROVAL)
    _emit(tool, ApprovalEventType.SUBMITTED, actor_id, notes)
    logger.info("Tool '%s' submitted for approval by '%s'", tool_id, actor_id)


def approve_tool(tool_id: str, actor_id: str, notes: str | None = None) -> None:
    """Transition pending_approval → approved. Raises EngineError if not pending."""
    tool = _load_or_raise(tool_id)
    if tool.status != ToolStatus.PENDING_APPROVAL:
        raise EngineError(
            f"Cannot approve tool '{tool_id}': "
            f"expected status 'pending_approval', got '{tool.status.value}'"
        )
    update_tool_status(tool_id, ToolStatus.APPROVED)
    _emit(tool, ApprovalEventType.APPROVED, actor_id, notes)
    logger.info("Tool '%s' approved by '%s'", tool_id, actor_id)


def reject_tool(tool_id: str, actor_id: str, notes: str | None = None) -> None:
    """
    Transition pending_approval → draft.

    ToolStatus has no REJECTED value, so rejection resets the tool to draft
    so the author can revise and resubmit. The REJECTED event is appended to
    the audit trail regardless.
    """
    tool = _load_or_raise(tool_id)
    if tool.status != ToolStatus.PENDING_APPROVAL:
        raise EngineError(
            f"Cannot reject tool '{tool_id}': "
            f"expected status 'pending_approval', got '{tool.status.value}'"
        )
    update_tool_status(tool_id, ToolStatus.DRAFT)
    _emit(tool, ApprovalEventType.REJECTED, actor_id, notes)
    logger.info("Tool '%s' rejected by '%s', reset to draft", tool_id, actor_id)


def revoke_tool(tool_id: str, actor_id: str, notes: str | None = None) -> None:
    """
    Transition approved → draft.

    ToolStatus has no REVOKED value, so revocation resets the tool to draft,
    requiring resubmission before it can run again. The REVOKED event is
    appended to the audit trail regardless.
    """
    tool = _load_or_raise(tool_id)
    if tool.status != ToolStatus.APPROVED:
        raise EngineError(
            f"Cannot revoke tool '{tool_id}': "
            f"expected status 'approved', got '{tool.status.value}'"
        )
    update_tool_status(tool_id, ToolStatus.DRAFT)
    _emit(tool, ApprovalEventType.REVOKED, actor_id, notes)
    logger.info("Tool '%s' revoked by '%s', reset to draft", tool_id, actor_id)


def deprecate_tool(tool_id: str, actor_id: str, notes: str | None = None) -> None:
    """Transition approved → deprecated. Raises EngineError if not approved."""
    tool = _load_or_raise(tool_id)
    if tool.status != ToolStatus.APPROVED:
        raise EngineError(
            f"Cannot deprecate tool '{tool_id}': "
            f"expected status 'approved', got '{tool.status.value}'"
        )
    update_tool_status(tool_id, ToolStatus.DEPRECATED)
    _emit(tool, ApprovalEventType.DEPRECATED, actor_id, notes)
    logger.info("Tool '%s' deprecated by '%s'", tool_id, actor_id)


def ensure_tool_approved(tool_def: ToolDefinition) -> None:
    """
    Raise ApprovalRequiredError unless the tool's status is approved.

    Call this at the start of any execution path. Deprecated tools are also
    blocked — they are not executable even though they were once approved.
    """
    if tool_def.status != ToolStatus.APPROVED:
        raise ApprovalRequiredError(tool_def.id, tool_def.status)
