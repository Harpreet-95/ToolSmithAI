from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.orchestrator.interfaces import IContextBuilder
from core.orchestrator.models import (
    EvidenceItem,
    EvidencePackage,
    OrchestratorRequest,
    ResolvedIntent,
    ServiceCallRecord,
    ServiceCapability,
    ServiceDescriptor,
)

logger = logging.getLogger(__name__)

_Adapter = Callable[[OrchestratorRequest], Any]


# ---------------------------------------------------------------------------
# Service adapters — thin wrappers that call existing service functions.
# Each adapter accepts OrchestratorRequest and returns the raw service output.
# Returns None when required parameters (source_id, user_id) are absent.
# All underlying services manage their own DB connections via data.db.get_connection().
# ---------------------------------------------------------------------------

def _dictionary(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.dictionary_service import list_dictionary_tables
    return list_dictionary_tables(source_id=req.source_id, user_id=req.user_id)


def _domain(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.domain_service import get_domain_summary
    return get_domain_summary(source_id=req.source_id, user_id=req.user_id)


def _entity(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.entity_service import get_entity_summary
    return get_entity_summary(source_id=req.source_id, user_id=req.user_id)


def _profiling(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.profiling_service import get_latest_profile
    return get_latest_profile(source_id=req.source_id, user_id=req.user_id)


def _governance(req: OrchestratorRequest) -> Any:
    from data.governance_service import governance_readiness_summary
    return governance_readiness_summary(source_id=req.source_id)


def _relationship(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.relationship_service import get_relationship_summary
    return get_relationship_summary(source_id=req.source_id, user_id=req.user_id)


def _knowledge_graph(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.knowledge_graph_service import knowledge_graph_summary
    return knowledge_graph_summary(source_id=req.source_id, user_id=req.user_id)


def _lineage(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.lineage_service import lineage_summary
    return lineage_summary(source_id=req.source_id, user_id=req.user_id)


def _semantic_layer(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.semantic_layer_service import semantic_summary
    return semantic_summary(source_id=req.source_id, user_id=req.user_id)


def _business_knowledge(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.business_knowledge_service import get_business_summary
    return get_business_summary(source_id=req.source_id, user_id=req.user_id)


def _reports(req: OrchestratorRequest) -> Any:
    if req.user_id is None:
        return None
    from data.report_service import list_reports_for_user
    return list_reports_for_user(user_id=req.user_id)


def _workflow(req: OrchestratorRequest) -> Any:
    if req.user_id is None:
        return None
    from data.workflow_service import list_workflows
    return list_workflows(user_id=req.user_id)


def _schema(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from data.schema_service import get_latest_snapshot
    return get_latest_snapshot(source_id=req.source_id, user_id=req.user_id)


def _search(req: OrchestratorRequest) -> Any:
    from data.search_service import search_metadata
    return search_metadata(q=req.query, limit=20, source_id=req.source_id)


def _live_metadata(req: OrchestratorRequest) -> Any:
    if req.source_id is None or req.user_id is None:
        return None
    from core.live.connection_resolver import LiveConnectionResolver
    from core.live.health_service import ConnectionHealthService
    from core.live.metadata_provider import LiveMetadataProvider
    from core.live.models import ResolutionStatus

    resolution = LiveConnectionResolver().resolve(
        req.source_id, req.user_id, required_capability="schema_discovery"
    )
    if resolution.status != ResolutionStatus.RESOLVED:
        return {"connection_state": resolution.status.value, "message": resolution.message}

    health = ConnectionHealthService().check(resolution.context)
    metadata = LiveMetadataProvider().get_metadata(resolution.context)
    metadata["connection_state"] = health.state.value
    return metadata


def _live_query(req: OrchestratorRequest) -> Any:
    sql = req.params.get("sql")
    if req.source_id is None or req.user_id is None or not sql:
        return None
    from core.live.query_engine import LiveQueryEngine

    result = LiveQueryEngine().execute(
        req.source_id, req.user_id, sql,
        params=req.params.get("sql_params"),
        row_limit=req.params.get("row_limit"),
        timeout_s=req.params.get("timeout_s"),
        page=req.params.get("page", 1),
        page_size=req.params.get("page_size"),
        max_payload_bytes=req.params.get("max_payload_bytes"),
    )
    return result.to_dict()


def _semantic_query_plan(req: OrchestratorRequest) -> Any:
    question = req.params.get("question") or req.query
    if req.source_id is None or req.user_id is None or not question:
        return None
    from core.semantic.planner import SemanticQueryPlanner

    plan = SemanticQueryPlanner().plan(
        req.source_id, req.user_id, question,
        filters=req.params.get("filters"),
    )
    return plan.to_dict() if plan is not None else None


def _execution_planner(req: OrchestratorRequest) -> Any:
    question = req.params.get("question") or req.query
    if not question:
        return None
    from core.execution.planner import ExecutionPlanner

    strategy = ExecutionPlanner().plan(question, req.source_id, req.user_id)
    return strategy.to_dict()


def _enterprise_answer(req: OrchestratorRequest) -> Any:
    question = req.params.get("question") or req.query
    if not question:
        return None
    from core.answering.answer_planner import AnswerPlanner
    from core.execution.planner import ExecutionPlanner
    from core.orchestrator.orchestrator import EnterpriseOrchestrator

    # Reuses the existing, unmodified intent-resolver-driven process() for
    # the Evidence Package input — this is "Execution Planner ↓ Answer
    # Generation Layer" using the same evidence the normal chat flow would
    # gather, not a second, differently-selected evidence-gathering pass.
    # Safe from re-entrancy: ENTERPRISE_ANSWER is never wired into
    # intent_resolver.py, so process() can never re-select this adapter.
    package = EnterpriseOrchestrator().process(req)
    strategy = ExecutionPlanner().plan(question, req.source_id, req.user_id)
    answer = AnswerPlanner().build(strategy, package)
    return {"execution_strategy": strategy.to_dict(), "enterprise_answer": answer.to_dict()}


_ADAPTERS: Dict[str, _Adapter] = {
    "dictionary":        _dictionary,
    "domain":            _domain,
    "entity":            _entity,
    "profiling":         _profiling,
    "governance":        _governance,
    "relationship":      _relationship,
    "knowledge_graph":   _knowledge_graph,
    "lineage":           _lineage,
    "semantic_layer":    _semantic_layer,
    "business_knowledge": _business_knowledge,
    "reports":           _reports,
    "workflow":          _workflow,
    "schema":            _schema,
    "search":            _search,
    "live_metadata":     _live_metadata,
    "live_query":        _live_query,
    "semantic_query_plan": _semantic_query_plan,
    "execution_planner":   _execution_planner,
    "enterprise_answer":   _enterprise_answer,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _primary_capability(descriptor: ServiceDescriptor) -> ServiceCapability:
    return descriptor.capabilities[0] if descriptor.capabilities else ServiceCapability.SEARCH_READ


def _evidence_confidence(data: Any) -> float:
    """Confidence that the evidence item contains useful data."""
    if data is None:
        return 0.0
    if isinstance(data, (list, dict)) and not data:
        return 0.5
    return 1.0


def _extract_governance_state(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        return data.get("governance_state") or data.get("state")
    return None


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class ContextBuilder(IContextBuilder):
    """
    Collects read-only evidence from registered enterprise services and
    packages it into an EvidencePackage.

    No AI, no summaries, no explanation generation, no writes.
    Service failures are tracked inside the package — they never raise.
    """

    def build(
        self,
        request: OrchestratorRequest,
        intent: ResolvedIntent,
        services: List[ServiceDescriptor],
    ) -> EvidencePackage:
        evidence: List[EvidenceItem] = []
        call_records: List[ServiceCallRecord] = []
        errors: List[str] = []
        succeeded = 0

        for descriptor in services:
            adapter = _ADAPTERS.get(descriptor.service_id)
            if adapter is None:
                msg = f"No adapter registered for service '{descriptor.service_id}'"
                logger.warning("ContextBuilder: %s", msg)
                errors.append(msg)
                continue

            func_name = (
                descriptor.primary_functions[0]
                if descriptor.primary_functions
                else descriptor.service_id
            )
            called_at = datetime.utcnow()
            t0 = time.perf_counter()

            try:
                data = adapter(request)
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

                call_records.append(ServiceCallRecord(
                    service_id=descriptor.service_id,
                    function_name=func_name,
                    called_at=called_at,
                    duration_ms=elapsed_ms,
                    succeeded=True,
                ))

                evidence.append(EvidenceItem(
                    evidence_id=str(uuid.uuid4()),
                    source_service=descriptor.service_id,
                    source_function=func_name,
                    capability=_primary_capability(descriptor),
                    data=data,
                    timestamp=called_at,
                    confidence=_evidence_confidence(data),
                    governance_state=_extract_governance_state(data),
                ))
                succeeded += 1

            except Exception as exc:  # noqa: BLE001
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                error_msg = f"{descriptor.service_id}/{func_name}: {type(exc).__name__}: {exc}"
                logger.warning("ContextBuilder service call failed — %s", error_msg)

                call_records.append(ServiceCallRecord(
                    service_id=descriptor.service_id,
                    function_name=func_name,
                    called_at=called_at,
                    duration_ms=elapsed_ms,
                    succeeded=False,
                    error=error_msg,
                ))
                errors.append(error_msg)

        return EvidencePackage(
            request_id=request.request_id,
            query=request.query,
            intent=intent,
            evidence=evidence,
            service_calls=call_records,
            built_at=datetime.utcnow(),
            source_id=request.source_id,
            errors=errors,
            total_evidence_items=len(evidence),
            services_attempted=len(services),
            services_succeeded=succeeded,
        )
