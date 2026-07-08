from __future__ import annotations

import logging
from typing import Optional

from core.execution import decision_tree
from core.execution.execution_strategy import ExecutionStrategy

logger = logging.getLogger(__name__)


def _build_permissions(result: dict) -> list[str]:
    """
    Honest, tag-level permissions reflecting this codebase's actual RBAC
    posture (ownership-based access, admin/user roles only — no granular
    permission engine to model precisely). Not a new authorization system.
    """
    permissions = ["read"]
    if result.get("governance_checks"):
        permissions.append("pii_review")
    if result.get("requires_live_data"):
        permissions.append("live_data")
    return permissions


class ExecutionPlanner:
    """
    Decides HOW a request should be answered — which existing enterprise
    services are needed and in what order — without answering the question,
    generating SQL, or executing anything.
    """

    def plan(
        self,
        question: str,
        source_id: Optional[int] = None,
        user_id: Optional[str] = None,
    ) -> ExecutionStrategy:
        from core.orchestrator.intent_resolver import IntentResolver

        resolved_intent = IntentResolver().resolve(question)

        semantic_plan = None
        if source_id is not None and user_id is not None:
            try:
                from core.semantic.planner import SemanticQueryPlanner
                semantic_plan = SemanticQueryPlanner().plan(source_id, user_id, question)
            except Exception:  # noqa: BLE001
                logger.warning("ExecutionPlanner: semantic planning failed, continuing without it")
                semantic_plan = None

        result = decision_tree.decide(question, resolved_intent, semantic_plan)

        return ExecutionStrategy(
            strategy_type=result["strategy_type"],
            required_services=result["required_services"],
            execution_order=result["execution_order"],
            required_permissions=_build_permissions(result),
            governance_checks=result["governance_checks"],
            requires_live_data=result["requires_live_data"],
            requires_sql=result["requires_sql"],
            requires_report=result["requires_report"],
            requires_search=result["requires_search"],
            requires_dictionary=result["requires_dictionary"],
            requires_profiling=result["requires_profiling"],
            requires_metadata=result["requires_metadata"],
            requires_workflow=result["requires_workflow"],
            warnings=result["warnings"],
            confidence=result["confidence"],
        )
