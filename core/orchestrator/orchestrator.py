from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime

from core.orchestrator.context_builder import ContextBuilder
from core.orchestrator.interfaces import IEnterpriseOrchestrator
from core.orchestrator.intent_resolver import IntentResolver
from core.orchestrator.models import (
    EvidenceItem,
    EvidencePackage,
    IntentType,
    OrchestratorRequest,
    ResolvedIntent,
    ServiceCallRecord,
    ServiceCapability,
)
from core.orchestrator.registry import ServiceRegistry

logger = logging.getLogger(__name__)


class EnterpriseOrchestrator(IEnterpriseOrchestrator):
    """
    Enterprise Orchestrator Foundation — Phase 1.

    Read-only. No AI, no LLM, no writes, no report generation.
    Orchestrates: intent resolution → service selection → evidence collection.

    Usage:
        orchestrator = EnterpriseOrchestrator()
        package = orchestrator.process(OrchestratorRequest(
            query="show me the dictionary for this source",
            source_id=42,
            user_id="7",
        ))
    """

    def __init__(self) -> None:
        self._registry = ServiceRegistry()
        self._intent_resolver = IntentResolver()
        self._context_builder = ContextBuilder()

    def process(self, request: OrchestratorRequest) -> EvidencePackage:
        logger.debug(
            "EnterpriseOrchestrator.process request_id=%s query=%r source_id=%s user_id=%s",
            request.request_id,
            request.query,
            request.source_id,
            request.user_id,
        )

        # 1. Resolve intent from query text
        intent = self._intent_resolver.resolve(request.query)
        logger.debug(
            "Intent resolved: type=%s confidence=%.4f keywords=%s secondary=%s",
            intent.intent_type,
            intent.confidence,
            intent.keywords_matched,
            [i.value for i in intent.secondary_intents],
        )

        # 2. Select services whose capabilities match the resolved intent
        services = self._registry.get_by_capability(intent.required_capabilities)
        logger.debug(
            "Services selected: %d for capabilities %s",
            len(services),
            [c.value for c in intent.required_capabilities],
        )

        # 3. Build evidence package — all service calls are read-only
        package = self._context_builder.build(request, intent, services)
        logger.debug(
            "EvidencePackage built: request_id=%s items=%d services=%d/%d errors=%d",
            package.request_id,
            package.total_evidence_items,
            package.services_succeeded,
            package.services_attempted,
            len(package.errors),
        )

        return package

    def run_live_query(self, request: OrchestratorRequest) -> EvidencePackage:
        """
        Explicit invocation path for the live_query capability — Phase 7.

        Bypasses keyword-based intent resolution entirely: this is a
        structured request from a trusted internal caller who already has
        SQL text to run, not a natural-language query to classify. Reuses
        IntentType.SQL_REQUEST for evidence labeling only; does not touch
        intent_resolver.py or change process()'s NL/metadata flow at all.
        """
        intent = ResolvedIntent(
            intent_type=IntentType.SQL_REQUEST,
            confidence=1.0,
            required_capabilities=[ServiceCapability.LIVE_QUERY],
        )
        services = self._registry.get_by_capability(intent.required_capabilities)
        return self._context_builder.build(request, intent, services)

    def run_execution_planning(self, request: OrchestratorRequest) -> EvidencePackage:
        """
        Explicit invocation path for the execution_planner capability —
        Phase 9. Mirrors run_live_query()/run_semantic_query_plan()'s bypass
        pattern. This is the literal "Semantic Planner -> Execution Planner
        -> Return Strategy" pipeline: the semantic planning step happens
        inside the execution_planner adapter itself (ExecutionPlanner.plan()
        calls SemanticQueryPlanner when a source is given), so this method
        only needs to select the one capability. No execution happens here
        or anywhere in core/execution/ — only a strategy is returned.
        """
        intent = ResolvedIntent(
            intent_type=IntentType.EXECUTION_PLANNER,
            confidence=1.0,
            required_capabilities=[ServiceCapability.EXECUTION_PLANNER],
        )
        services = self._registry.get_by_capability(intent.required_capabilities)
        return self._context_builder.build(request, intent, services)

    def run_enterprise_answer(self, request: OrchestratorRequest) -> EvidencePackage:
        """
        Explicit invocation path for the enterprise_answer capability —
        Phase 10. This literally is "Execution Planner -> Answer Generation
        Layer -> EnterpriseAnswer": process() (unmodified) supplies the
        Evidence Package, ExecutionPlanner (Phase 9, unmodified) supplies
        the Execution Strategy, and AnswerPlanner turns both into a
        deterministic EnterpriseAnswer — appended as one extra evidence
        item so every original evidence item process() would have produced
        is still present unchanged (fully additive, backward compatible).
        """
        from core.answering.answer_planner import AnswerPlanner
        from core.execution.planner import ExecutionPlanner

        package = self.process(request)

        strategy = ExecutionPlanner().plan(request.query, request.source_id, request.user_id)
        started = time.perf_counter()
        answer = AnswerPlanner().build(strategy, package)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        package.evidence.append(EvidenceItem(
            evidence_id=str(uuid.uuid4()),
            source_service="enterprise_answer",
            source_function="build",
            capability=ServiceCapability.ENTERPRISE_ANSWER,
            data={"execution_strategy": strategy.to_dict(), "enterprise_answer": answer.to_dict()},
            timestamp=datetime.utcnow(),
            confidence=answer.confidence / 100.0,
        ))
        package.service_calls.append(ServiceCallRecord(
            service_id="enterprise_answer",
            function_name="build",
            called_at=datetime.utcnow(),
            duration_ms=duration_ms,
            succeeded=True,
        ))
        package.total_evidence_items = len(package.evidence)
        package.services_attempted += 1
        package.services_succeeded += 1

        return package

    def run_semantic_query_plan(self, request: OrchestratorRequest) -> EvidencePackage:
        """
        Explicit invocation path for the semantic_query_plan capability —
        Phase 8. Mirrors run_live_query()'s bypass pattern for a trusted,
        structured caller that already knows it wants a plan (e.g. a future
        AI Workspace Composer backend), independent of the chat-triggerable
        keyword path also registered for this intent in intent_resolver.py.
        """
        intent = ResolvedIntent(
            intent_type=IntentType.SEMANTIC_QUERY_PLAN,
            confidence=1.0,
            required_capabilities=[ServiceCapability.SEMANTIC_QUERY_PLAN],
        )
        services = self._registry.get_by_capability(intent.required_capabilities)
        return self._context_builder.build(request, intent, services)
