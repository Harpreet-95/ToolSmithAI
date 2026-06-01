"""
Foundational types for the Dynamic Tool Creation Engine.

No database, framework, or domain imports.
No side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ToolStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class TriggerType(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    WEBHOOK = "webhook"
    EVENT = "event"


class FailureMode(str, Enum):
    FAIL_GRAPH = "fail_graph"       # Stop execution, mark run failed
    SKIP_NODE = "skip_node"         # Skip this node, continue to next
    CONTINUE_GRAPH = "continue_graph"  # Record failure, keep going


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApprovalLevel(str, Enum):
    NONE = "none"           # No approval required (internal tools)
    USER = "user"           # Creator self-approves
    ADMIN = "admin"         # Admin must approve
    MULTI_PARTY = "multi_party"  # Future: multiple approvers required


class ApprovalEventType(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    DEPRECATED = "deprecated"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class EngineError(Exception):
    """Base exception for all Dynamic Tool Engine errors."""


class SchemaValidationError(EngineError):
    """Raised when a ToolDefinition fails schema validation."""

    def __init__(self, message: str, field_name: Optional[str] = None) -> None:
        super().__init__(message)
        self.field_name = field_name


class CycleDetectedError(EngineError):
    """Raised when the execution graph contains a cycle."""


class ApprovalRequiredError(EngineError):
    """Raised when attempting to execute a tool that is not approved."""

    def __init__(self, tool_id: str, status: ToolStatus) -> None:
        super().__init__(
            f"Tool '{tool_id}' cannot be executed with status '{status.value}'"
        )
        self.tool_id = tool_id
        self.status = status


class ActionHandlerNotFoundError(EngineError):
    """Raised when no registered handler exists for an action_type."""

    def __init__(self, action_type: str) -> None:
        super().__init__(f"No action handler registered for type '{action_type}'")
        self.action_type = action_type


class TemplateResolutionError(EngineError):
    """Raised when a {{path.to.field}} reference cannot be resolved."""


# ---------------------------------------------------------------------------
# Config / Spec dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    max_attempts: int = 1
    backoff_ms: int = 1_000
    retryable_errors: List[str] = field(default_factory=list)


@dataclass
class TriggerSpec:
    type: TriggerType = TriggerType.MANUAL
    schedule_cron: Optional[str] = None   # Future: "0 9 * * 1"
    webhook_path: Optional[str] = None    # Future
    event_name: Optional[str] = None      # Future


@dataclass
class InputSpec:
    name: str = ""
    type: str = "string"   # string | integer | float | boolean | dataset_id | file
    required: bool = True
    default: Optional[Any] = None


@dataclass
class OutputSpec:
    name: str = ""
    type: str = "string"
    source_ref: str = ""   # e.g. "{{step_1.report_id}}"


@dataclass
class ApprovalSpec:
    required: bool = True
    level: ApprovalLevel = ApprovalLevel.ADMIN
    approvers: List[str] = field(default_factory=list)  # Future: user_ids or role names


@dataclass
class RBACSpec:
    """Future: role-based access control for tool execution."""
    allowed_roles: List[str] = field(default_factory=list)
    allowed_users: List[str] = field(default_factory=list)


@dataclass
class ScheduleSpec:
    """Future: recurring execution configuration."""
    enabled: bool = False
    cron: str = ""
    timezone: str = "UTC"


@dataclass
class AISpec:
    """Future: AI enrichment configuration for tool planning."""
    allow_enrichment: bool = False
    enrichment_model: str = ""


@dataclass
class ToolMetadata:
    author_id: str = ""
    org_id: Optional[str] = None       # Future: multi-tenant
    tags: List[str] = field(default_factory=list)
    category: str = ""                  # "reporting" | "data" | "communication"
    source: str = "user_defined"        # "rule_based" | "ai_assisted" | "user_defined"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_run_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Graph dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ActionNode:
    id: str = ""
    label: str = ""
    action_type: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    template_refs: Dict[str, str] = field(default_factory=dict)  # {"param": "{{step.field}}"}
    condition: Optional[str] = None
    retry: RetryConfig = field(default_factory=RetryConfig)
    timeout_ms: int = 30_000
    on_failure: FailureMode = FailureMode.FAIL_GRAPH


@dataclass
class ActionEdge:
    from_node_id: str = ""
    to_node_id: str = ""
    condition: Optional[str] = None


@dataclass
class ExecutionGraph:
    entry_node_id: str = ""
    nodes: List[ActionNode] = field(default_factory=list)
    edges: List[ActionEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool Definition
# ---------------------------------------------------------------------------

@dataclass
class ToolDefinition:
    """Complete, validated definition of a reusable tool."""
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""          # snake_case slug, e.g. "weekly_sales_report"
    version: str = "1.0.0"  # semver
    description: str = ""
    status: ToolStatus = ToolStatus.DRAFT
    trigger: TriggerSpec = field(default_factory=TriggerSpec)
    inputs: List[InputSpec] = field(default_factory=list)
    graph: ExecutionGraph = field(default_factory=ExecutionGraph)
    outputs: List[OutputSpec] = field(default_factory=list)
    approval: ApprovalSpec = field(default_factory=ApprovalSpec)
    rbac: RBACSpec = field(default_factory=RBACSpec)          # Future
    schedule: ScheduleSpec = field(default_factory=ScheduleSpec)  # Future
    ai: AISpec = field(default_factory=AISpec)                # Future
    metadata: ToolMetadata = field(default_factory=ToolMetadata)


# ---------------------------------------------------------------------------
# Execution result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    """Outcome returned by a single action handler."""
    status: ActionStatus = ActionStatus.COMPLETED
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0


@dataclass
class StepResult:
    """Per-node record stored inside RunRecord.step_results."""
    node_id: str = ""
    action_type: str = ""
    status: ActionStatus = ActionStatus.COMPLETED
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: int = 0


@dataclass
class RunRecord:
    """Persistent record of a single tool execution."""
    run_id: str = field(default_factory=lambda: str(uuid4()))
    tool_id: str = ""
    tool_version: str = "1.0.0"
    trigger_source: str = "manual"      # manual | scheduled | webhook
    status: RunStatus = RunStatus.RUNNING
    inputs: Dict[str, Any] = field(default_factory=dict)
    step_results: List[StepResult] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    user_id: str = ""


@dataclass
class ApprovalEvent:
    """Append-only log entry for a tool's approval lifecycle."""
    id: str = field(default_factory=lambda: str(uuid4()))
    tool_id: str = ""
    tool_version: str = "1.0.0"
    event_type: ApprovalEventType = ApprovalEventType.CREATED
    actor_id: str = ""
    notes: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExecutionContext:
    """Shared state threaded through all nodes during a single run."""
    inputs: Dict[str, Any] = field(default_factory=dict)
    node_outputs: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        """Flat dict used by resolve_template for path resolution."""
        return {"inputs": self.inputs, **self.node_outputs}


# ---------------------------------------------------------------------------
# Template resolution utility
# ---------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{\{([^}]+)\}\}")


def resolve_template(value: Any, context: Dict[str, Any]) -> Any:
    """
    Recursively resolve {{path.to.field}} references in value.

    - str: replaces all template tokens; if the entire string is a single
      token, the resolved value preserves its original type (int, bool, etc.)
    - dict / list: resolved recursively
    - all other types: returned unchanged

    Raises TemplateResolutionError if any path cannot be walked.
    """
    if isinstance(value, str):
        # Single-token shortcut: preserve the resolved type instead of str()
        full = re.fullmatch(r"\{\{([^}]+)\}\}", value.strip())
        if full:
            return _resolve_path(full.group(1).strip(), context)

        def _replace(match: re.Match) -> str:
            return str(_resolve_path(match.group(1).strip(), context))

        return _TEMPLATE_RE.sub(_replace, value)

    if isinstance(value, dict):
        return {k: resolve_template(v, context) for k, v in value.items()}

    if isinstance(value, list):
        return [resolve_template(item, context) for item in value]

    return value


def _resolve_path(path: str, context: Dict[str, Any]) -> Any:
    """Walk dot-separated path through context, raising TemplateResolutionError on failure."""
    node: Any = context
    for part in path.split("."):
        if not isinstance(node, dict):
            raise TemplateResolutionError(
                f"Cannot resolve '{{{{ {path} }}}}': "
                f"'{part}' accessed on a non-dict value"
            )
        if part not in node:
            raise TemplateResolutionError(
                f"Cannot resolve '{{{{ {path} }}}}': key '{part}' not found"
            )
        node = node[part]
    return node
