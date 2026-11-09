"""
Planning layer for the Dynamic Tool Creation Engine.

plan_tool() converts a natural-language intent into a validated ToolDefinition
in DRAFT status, ready for the approval workflow.

Composer integration:
  core.composer.intent_composer.compose_from_intent is called for its
  deterministic rule stage. The AI enrichment stages inside the composer are
  gated behind the ENABLE_AI_PLANNER env flag — no additional gate is needed
  here. If the composer is unavailable or returns unusable output, plan_tool
  falls back silently to a safe draft placeholder.

Placeholder:
  When mapping fails, the returned ToolDefinition contains a single node with
  action_type="manual_review_required" so an admin can inspect and complete
  the definition before approving it.

Guarantees on the returned ToolDefinition:
  - status  = DRAFT
  - trigger = MANUAL
  - graph has at least one ActionNode

No external service calls. No DB writes. No execution.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from core.engine.contracts import ToolStatus, TriggerType
from core.engine.schema import validate_tool_definition

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_words: int = 5) -> str:
    """Convert arbitrary text to a safe snake_case slug."""
    tokens = _SLUG_RE.sub(" ", text.lower()).split()
    words = [t for t in tokens if len(t) > 1][:max_words]
    return "_".join(words) if words else "custom_tool"


def _map_proposal_to_raw(proposal: dict, intent: str) -> Optional[dict]:
    """
    Convert a compose_from_intent proposal dict to a raw ToolDefinition dict.

    Returns None when the proposal contains no usable steps, letting the
    caller fall through to the placeholder.
    """
    steps: List[dict] = proposal.get("primitives_or_steps") or []
    if not steps:
        return None

    nodes: List[dict] = []
    for i, step in enumerate(steps, 1):
        action_type = (step.get("step_type") or step.get("primitive_type") or "").strip()
        if not action_type:
            continue
        nodes.append({
            "id":          f"node_{i}",
            "label":       step.get("purpose") or action_type,
            "action_type": action_type,
        })

    if not nodes:
        return None

    # Build sequential edges: node_1 → node_2 → ... → node_n
    edges = [
        {
            "from_node_id": nodes[j]["id"],
            "to_node_id":   nodes[j + 1]["id"],
        }
        for j in range(len(nodes) - 1)
    ]

    inputs = [
        {"name": inp, "type": "string", "required": True}
        for inp in (proposal.get("required_inputs") or [])
        if isinstance(inp, str) and inp.strip()
    ]

    name = _slugify(proposal.get("suggested_name") or intent, max_words=8)

    source = proposal.get("source") or "rule_based"
    if source not in ("rule_based", "ai_assisted", "user_defined"):
        source = "rule_based"

    category = proposal.get("proposal_type") or "dynamic_tool"

    return {
        "name":        name,
        "version":     "1.0.0",
        "description": (proposal.get("interpreted_goal") or intent).strip(),
        "status":      ToolStatus.DRAFT.value,
        "trigger":     {"type": TriggerType.MANUAL.value},
        "inputs":      inputs,
        "graph": {
            "entry_node_id": nodes[0]["id"],
            "nodes":         nodes,
            "edges":         edges,
        },
        "approval": {
            "required": True,
            "level":    "admin",
        },
        "metadata": {
            "source":   source,
            "category": category,
            "tags":     [],
        },
    }


def _placeholder_raw(intent: str) -> dict:
    """
    Return a raw ToolDefinition dict for the safe fallback placeholder.

    The single node action_type "manual_review_required" signals to admins
    that the planner could not map the intent to a concrete execution plan.
    """
    return {
        "name":        _slugify(intent),
        "version":     "1.0.0",
        "description": intent.strip() or "No description provided.",
        "status":      ToolStatus.DRAFT.value,
        "trigger":     {"type": TriggerType.MANUAL.value},
        "inputs":      [],
        "graph": {
            "entry_node_id": "node_1",
            "nodes": [
                {
                    "id":          "node_1",
                    "label":       "Manual review required",
                    "action_type": "manual_review_required",
                }
            ],
            "edges": [],
        },
        "approval": {
            "required": True,
            "level":    "admin",
        },
        "metadata": {
            "source":   "user_defined",
            "category": "dynamic_tool",
            "tags":     [],
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_tool(
    intent: str,
    context: Optional[Dict[str, Any]] = None,
) -> "ToolDefinition":  # type: ignore[name-defined]  # ToolDefinition imported at runtime
    """
    Convert a natural-language intent into a validated draft ToolDefinition.

    Args:
        intent:  Natural-language description of what the tool should do.
        context: Optional dict. Recognised keys:
                   dataset_id (int | None) — forwarded to the composer so it
                   can include dataset-related inputs and avoid false
                   clarification warnings.

    Returns:
        A validated ToolDefinition with status=DRAFT, trigger=MANUAL, and at
        least one ActionNode.

    Never raises — any internal failure produces the safe placeholder.
    """
    from core.engine.contracts import ToolDefinition  # local import avoids circular at module level

    context = context or {}
    dataset_id: Optional[int] = context.get("dataset_id")

    use_fallback = False
    raw: Optional[dict] = None

    # Step 1 — call the composer (deterministic rule stage runs always;
    # AI stages are opt-in and handled inside the composer).
    try:
        from core.composer.intent_composer import compose_from_intent
        proposal = compose_from_intent(intent, dataset_id=dataset_id)
    except Exception as exc:
        logger.warning("plan_tool: composer unavailable (%s: %s); using placeholder",
                       type(exc).__name__, exc)
        proposal = None
        use_fallback = True

    # Step 2 — map the proposal to a raw ToolDefinition dict.
    if proposal is not None:
        try:
            raw = _map_proposal_to_raw(proposal, intent)
            if raw is None:
                logger.warning("plan_tool: proposal had no usable steps; using placeholder")
                use_fallback = True
        except Exception as exc:
            logger.warning("plan_tool: proposal mapping failed (%s: %s); using placeholder",
                           type(exc).__name__, exc)
            raw = None
            use_fallback = True

    # Step 3 — fall back to placeholder if nothing usable was produced.
    if use_fallback or raw is None:
        raw = _placeholder_raw(intent)

    # Step 4 — validate through schema and return.
    tool_def: ToolDefinition = validate_tool_definition(raw)

    logger.info(
        "plan_tool: '%s' | %d node(s) | source=%s | fallback=%s",
        tool_def.name,
        len(tool_def.graph.nodes),
        tool_def.metadata.source,
        use_fallback,
    )
    return tool_def
