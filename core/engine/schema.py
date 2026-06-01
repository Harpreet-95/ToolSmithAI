"""
Schema validation for the Dynamic Tool Creation Engine.

Converts raw dicts into validated, fully-constructed dataclasses.
Raises SchemaValidationError with a field-level message on any violation.

Imports only from stdlib and core.engine.contracts.
No side effects.
"""

from __future__ import annotations

from typing import Any, List, Set, Type, TypeVar
from uuid import uuid4

from core.engine.contracts import (
    ActionEdge,
    ActionNode,
    AISpec,
    ApprovalLevel,
    ApprovalSpec,
    ExecutionGraph,
    FailureMode,
    InputSpec,
    OutputSpec,
    RBACSpec,
    RetryConfig,
    ScheduleSpec,
    SchemaValidationError,
    ToolDefinition,
    ToolMetadata,
    ToolStatus,
    TriggerSpec,
    TriggerType,
)

_E = TypeVar("_E")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_tool_definition(raw: dict) -> ToolDefinition:
    """
    Validate a raw dict and return a fully-constructed ToolDefinition.

    Required fields: name, version, trigger, graph (with at least one node).
    All other fields default safely when absent.
    Raises SchemaValidationError with a field_name attribute on any violation.
    """
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Tool definition must be a dict, got {type(raw).__name__}"
        )

    tool_id = raw.get("id") or str(uuid4())
    if not isinstance(tool_id, str):
        raise SchemaValidationError("Field 'id' must be a string", "id")

    name    = _require_str(raw, "name")
    version = _require_str(raw, "version")
    description = _optional_str(raw, "description")
    status  = _parse_enum(raw, "status", ToolStatus, default=ToolStatus.DRAFT)

    trigger_raw = raw.get("trigger")
    if trigger_raw is None:
        raise SchemaValidationError("Field 'trigger' is required", "trigger")
    trigger = _build_trigger_spec(trigger_raw)

    graph_raw = raw.get("graph")
    if graph_raw is None:
        raise SchemaValidationError("Field 'graph' is required", "graph")
    graph = _build_execution_graph(graph_raw)

    inputs   = _build_input_specs(raw.get("inputs") or [])
    outputs  = _build_output_specs(raw.get("outputs") or [])
    approval = _build_approval_spec(raw.get("approval") or {})
    rbac     = _build_rbac_spec(raw.get("rbac") or {})
    schedule = _build_schedule_spec(raw.get("schedule") or {})
    ai       = _build_ai_spec(raw.get("ai") or {})
    metadata = _build_metadata(raw.get("metadata") or {})

    tool = ToolDefinition(
        id=tool_id,
        name=name,
        version=version,
        description=description,
        status=status,
        trigger=trigger,
        inputs=inputs,
        graph=graph,
        outputs=outputs,
        approval=approval,
        rbac=rbac,
        schedule=schedule,
        ai=ai,
        metadata=metadata,
    )

    # Structural graph checks run after the full object is built so
    # error messages can reference node ids rather than raw-dict indices.
    validate_execution_graph(tool.graph)
    return tool


def validate_execution_graph(graph: ExecutionGraph) -> None:
    """
    Validate the structural integrity of an ExecutionGraph.

    Checks:
      - At least one node exists.
      - All node ids are non-empty and unique within the graph.
      - Every node has a non-empty action_type.
      - entry_node_id refers to an existing node id.
      - Every edge endpoint (from_node_id, to_node_id) refers to an existing node.

    Does NOT detect cycles — that is graph.py's responsibility.
    Raises SchemaValidationError on the first violation found.
    """
    if not graph.nodes:
        raise SchemaValidationError(
            "Execution graph must contain at least one node", "graph.nodes"
        )

    seen_ids: Set[str] = set()
    for i, node in enumerate(graph.nodes):
        if not node.id or not node.id.strip():
            raise SchemaValidationError(
                f"Node at index {i} has an empty 'id'",
                f"graph.nodes[{i}].id",
            )
        if node.id in seen_ids:
            raise SchemaValidationError(
                f"Duplicate node id '{node.id}'",
                f"graph.nodes[{i}].id",
            )
        seen_ids.add(node.id)

        if not node.action_type or not node.action_type.strip():
            raise SchemaValidationError(
                f"Node '{node.id}' has an empty 'action_type'",
                f"graph.nodes[{i}].action_type",
            )

    if graph.entry_node_id not in seen_ids:
        raise SchemaValidationError(
            f"entry_node_id '{graph.entry_node_id}' does not match any node id",
            "graph.entry_node_id",
        )

    for i, edge in enumerate(graph.edges):
        if edge.from_node_id not in seen_ids:
            raise SchemaValidationError(
                f"Edge[{i}] references unknown from_node_id '{edge.from_node_id}'",
                f"graph.edges[{i}].from_node_id",
            )
        if edge.to_node_id not in seen_ids:
            raise SchemaValidationError(
                f"Edge[{i}] references unknown to_node_id '{edge.to_node_id}'",
                f"graph.edges[{i}].to_node_id",
            )


# ---------------------------------------------------------------------------
# Internal builders — each converts a raw value into its contract dataclass
# ---------------------------------------------------------------------------

def _build_trigger_spec(raw: Any) -> TriggerSpec:
    if isinstance(raw, TriggerSpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Field 'trigger' must be a dict, got {type(raw).__name__}", "trigger"
        )
    trigger_type = _parse_enum(raw, "type", TriggerType, default=TriggerType.MANUAL, prefix="trigger")
    return TriggerSpec(
        type=trigger_type,
        schedule_cron=raw.get("schedule_cron"),
        webhook_path=raw.get("webhook_path"),
        event_name=raw.get("event_name"),
    )


def _build_execution_graph(raw: Any) -> ExecutionGraph:
    if isinstance(raw, ExecutionGraph):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Field 'graph' must be a dict, got {type(raw).__name__}", "graph"
        )

    nodes_raw = raw.get("nodes", [])
    if not isinstance(nodes_raw, list):
        raise SchemaValidationError("Field 'graph.nodes' must be a list", "graph.nodes")
    if not nodes_raw:
        raise SchemaValidationError(
            "Execution graph must contain at least one node", "graph.nodes"
        )

    edges_raw = raw.get("edges", [])
    if not isinstance(edges_raw, list):
        raise SchemaValidationError("Field 'graph.edges' must be a list", "graph.edges")

    nodes = [_build_action_node(n, i) for i, n in enumerate(nodes_raw)]
    edges = [_build_action_edge(e, i) for i, e in enumerate(edges_raw)]

    entry_node_id = raw.get("entry_node_id", "")
    if not isinstance(entry_node_id, str):
        raise SchemaValidationError(
            "Field 'graph.entry_node_id' must be a string", "graph.entry_node_id"
        )
    # Default to the first node's id when not explicitly provided.
    if not entry_node_id.strip() and nodes:
        entry_node_id = nodes[0].id

    return ExecutionGraph(
        entry_node_id=entry_node_id.strip(),
        nodes=nodes,
        edges=edges,
    )


def _build_action_node(raw: Any, index: int) -> ActionNode:
    if isinstance(raw, ActionNode):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Node at index {index} must be a dict, got {type(raw).__name__}",
            f"graph.nodes[{index}]",
        )

    node_id = raw.get("id", "")
    if not isinstance(node_id, str) or not node_id.strip():
        raise SchemaValidationError(
            f"Node at index {index} must have a non-empty 'id'",
            f"graph.nodes[{index}].id",
        )
    node_id = node_id.strip()

    action_type = raw.get("action_type", "")
    if not isinstance(action_type, str) or not action_type.strip():
        raise SchemaValidationError(
            f"Node '{node_id}' must have a non-empty 'action_type'",
            f"graph.nodes[{index}].action_type",
        )

    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise SchemaValidationError(
            f"Node '{node_id}' field 'params' must be a dict",
            f"graph.nodes[{index}].params",
        )

    template_refs = raw.get("template_refs", {})
    if not isinstance(template_refs, dict):
        raise SchemaValidationError(
            f"Node '{node_id}' field 'template_refs' must be a dict",
            f"graph.nodes[{index}].template_refs",
        )

    timeout_ms = raw.get("timeout_ms", 30_000)
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise SchemaValidationError(
            f"Node '{node_id}' field 'timeout_ms' must be a positive integer",
            f"graph.nodes[{index}].timeout_ms",
        )

    condition = raw.get("condition")
    if condition is not None and not isinstance(condition, str):
        raise SchemaValidationError(
            f"Node '{node_id}' field 'condition' must be a string or null",
            f"graph.nodes[{index}].condition",
        )

    on_failure = _parse_enum(
        raw, "on_failure", FailureMode,
        default=FailureMode.FAIL_GRAPH,
        prefix=f"graph.nodes[{index}]",
    )

    retry_raw = raw.get("retry", {})
    if not isinstance(retry_raw, dict):
        raise SchemaValidationError(
            f"Node '{node_id}' field 'retry' must be a dict",
            f"graph.nodes[{index}].retry",
        )
    retry = _build_retry_config(retry_raw, node_id, index)

    return ActionNode(
        id=node_id,
        label=str(raw.get("label", "")),
        action_type=action_type.strip(),
        params=params,
        template_refs=template_refs,
        condition=condition,
        retry=retry,
        timeout_ms=timeout_ms,
        on_failure=on_failure,
    )


def _build_action_edge(raw: Any, index: int) -> ActionEdge:
    if isinstance(raw, ActionEdge):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Edge at index {index} must be a dict, got {type(raw).__name__}",
            f"graph.edges[{index}]",
        )

    from_id = raw.get("from_node_id", "")
    if not isinstance(from_id, str) or not from_id.strip():
        raise SchemaValidationError(
            f"Edge[{index}] must have a non-empty 'from_node_id'",
            f"graph.edges[{index}].from_node_id",
        )

    to_id = raw.get("to_node_id", "")
    if not isinstance(to_id, str) or not to_id.strip():
        raise SchemaValidationError(
            f"Edge[{index}] must have a non-empty 'to_node_id'",
            f"graph.edges[{index}].to_node_id",
        )

    condition = raw.get("condition")
    if condition is not None and not isinstance(condition, str):
        raise SchemaValidationError(
            f"Edge[{index}] field 'condition' must be a string or null",
            f"graph.edges[{index}].condition",
        )

    return ActionEdge(
        from_node_id=from_id.strip(),
        to_node_id=to_id.strip(),
        condition=condition,
    )


def _build_retry_config(raw: dict, node_id: str, node_index: int) -> RetryConfig:
    prefix = f"graph.nodes[{node_index}].retry"

    max_attempts = raw.get("max_attempts", 1)
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise SchemaValidationError(
            f"Node '{node_id}' retry.max_attempts must be an integer >= 1",
            f"{prefix}.max_attempts",
        )

    backoff_ms = raw.get("backoff_ms", 1_000)
    if not isinstance(backoff_ms, int) or backoff_ms < 0:
        raise SchemaValidationError(
            f"Node '{node_id}' retry.backoff_ms must be a non-negative integer",
            f"{prefix}.backoff_ms",
        )

    retryable_errors = raw.get("retryable_errors", [])
    if not isinstance(retryable_errors, list):
        raise SchemaValidationError(
            f"Node '{node_id}' retry.retryable_errors must be a list",
            f"{prefix}.retryable_errors",
        )

    return RetryConfig(
        max_attempts=max_attempts,
        backoff_ms=backoff_ms,
        retryable_errors=[str(e) for e in retryable_errors],
    )


def _build_input_specs(raw: Any) -> List[InputSpec]:
    if not isinstance(raw, list):
        raise SchemaValidationError("Field 'inputs' must be a list", "inputs")
    return [_build_input_spec(item, i) for i, item in enumerate(raw)]


def _build_input_spec(raw: Any, index: int) -> InputSpec:
    if isinstance(raw, InputSpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Input at index {index} must be a dict", f"inputs[{index}]"
        )
    name = raw.get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise SchemaValidationError(
            f"Input at index {index} must have a non-empty 'name'",
            f"inputs[{index}].name",
        )
    return InputSpec(
        name=name.strip(),
        type=str(raw.get("type", "string")),
        required=bool(raw.get("required", True)),
        default=raw.get("default"),
    )


def _build_output_specs(raw: Any) -> List[OutputSpec]:
    if not isinstance(raw, list):
        raise SchemaValidationError("Field 'outputs' must be a list", "outputs")
    return [_build_output_spec(item, i) for i, item in enumerate(raw)]


def _build_output_spec(raw: Any, index: int) -> OutputSpec:
    if isinstance(raw, OutputSpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError(
            f"Output at index {index} must be a dict", f"outputs[{index}]"
        )
    name = raw.get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise SchemaValidationError(
            f"Output at index {index} must have a non-empty 'name'",
            f"outputs[{index}].name",
        )
    return OutputSpec(
        name=name.strip(),
        type=str(raw.get("type", "string")),
        source_ref=str(raw.get("source_ref", "")),
    )


def _build_approval_spec(raw: Any) -> ApprovalSpec:
    if isinstance(raw, ApprovalSpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError("Field 'approval' must be a dict", "approval")
    level = _parse_enum(raw, "level", ApprovalLevel, default=ApprovalLevel.ADMIN, prefix="approval")
    return ApprovalSpec(
        required=bool(raw.get("required", True)),
        level=level,
        approvers=list(raw.get("approvers") or []),
    )


def _build_rbac_spec(raw: Any) -> RBACSpec:
    if isinstance(raw, RBACSpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError("Field 'rbac' must be a dict", "rbac")
    return RBACSpec(
        allowed_roles=list(raw.get("allowed_roles") or []),
        allowed_users=list(raw.get("allowed_users") or []),
    )


def _build_schedule_spec(raw: Any) -> ScheduleSpec:
    if isinstance(raw, ScheduleSpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError("Field 'schedule' must be a dict", "schedule")
    return ScheduleSpec(
        enabled=bool(raw.get("enabled", False)),
        cron=str(raw.get("cron", "")),
        timezone=str(raw.get("timezone", "UTC")),
    )


def _build_ai_spec(raw: Any) -> AISpec:
    if isinstance(raw, AISpec):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError("Field 'ai' must be a dict", "ai")
    return AISpec(
        allow_enrichment=bool(raw.get("allow_enrichment", False)),
        enrichment_model=str(raw.get("enrichment_model", "")),
    )


def _build_metadata(raw: Any) -> ToolMetadata:
    if isinstance(raw, ToolMetadata):
        return raw
    if not isinstance(raw, dict):
        raise SchemaValidationError("Field 'metadata' must be a dict", "metadata")
    return ToolMetadata(
        author_id=str(raw.get("author_id", "")),
        org_id=raw.get("org_id"),
        tags=list(raw.get("tags") or []),
        category=str(raw.get("category", "")),
        source=str(raw.get("source", "user_defined")),
        # created_at / updated_at / last_run_at always default to utcnow()
        # in the dataclass; the persistence layer owns those timestamps.
    )


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _require_str(raw: dict, key: str) -> str:
    val = raw.get(key)
    if val is None:
        raise SchemaValidationError(f"Field '{key}' is required", key)
    if not isinstance(val, str):
        raise SchemaValidationError(
            f"Field '{key}' must be a string, got {type(val).__name__}", key
        )
    if not val.strip():
        raise SchemaValidationError(f"Field '{key}' must not be empty", key)
    return val.strip()


def _optional_str(raw: dict, key: str, default: str = "") -> str:
    val = raw.get(key, default)
    if val is None:
        return default
    if not isinstance(val, str):
        raise SchemaValidationError(
            f"Field '{key}' must be a string, got {type(val).__name__}", key
        )
    return val


def _parse_enum(
    raw: dict,
    key: str,
    enum_class: Type[_E],
    *,
    default: _E,
    prefix: str = "",
) -> _E:
    full_key = f"{prefix}.{key}" if prefix else key
    val = raw.get(key)
    if val is None:
        return default
    try:
        return enum_class(val)  # type: ignore[call-arg]
    except (ValueError, KeyError):
        valid = [e.value for e in enum_class]  # type: ignore[attr-defined]
        raise SchemaValidationError(
            f"Field '{full_key}' must be one of {valid}, got '{val}'", full_key
        )
