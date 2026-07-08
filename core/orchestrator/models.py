from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class IntentType(str, Enum):
    QUESTION_ANSWERING = "question_answering"
    METADATA_LOOKUP    = "metadata_lookup"
    DICTIONARY         = "dictionary"
    DOMAIN             = "domain"
    ENTITY             = "entity"
    PROFILING          = "profiling"
    GOVERNANCE         = "governance"
    RELATIONSHIP       = "relationship"
    KNOWLEDGE_GRAPH    = "knowledge_graph"
    REPORTS            = "reports"
    REPORT_GENERATION  = "report_generation"
    SQL_REQUEST        = "sql_request"
    WORKFLOW           = "workflow"
    REVIEW             = "review"
    SEMANTIC_QUERY_PLAN = "semantic_query_plan"
    EXECUTION_PLANNER  = "execution_planner"
    ENTERPRISE_ANSWER  = "enterprise_answer"
    UNKNOWN            = "unknown"


class ServiceCapability(str, Enum):
    DICTIONARY_READ       = "dictionary_read"
    DOMAIN_READ           = "domain_read"
    ENTITY_READ           = "entity_read"
    PROFILING_READ        = "profiling_read"
    GOVERNANCE_READ       = "governance_read"
    RELATIONSHIP_READ     = "relationship_read"
    KNOWLEDGE_GRAPH_READ  = "knowledge_graph_read"
    LINEAGE_READ          = "lineage_read"
    SEMANTIC_READ         = "semantic_read"
    BUSINESS_CONTEXT_READ = "business_context_read"
    REPORT_READ           = "report_read"
    WORKFLOW_READ         = "workflow_read"
    SCHEMA_READ           = "schema_read"
    SEARCH_READ           = "search_read"
    LIVE_METADATA         = "live_metadata"
    LIVE_SCHEMA           = "live_schema"
    LIVE_TABLES           = "live_tables"
    LIVE_COLUMNS          = "live_columns"
    LIVE_RELATIONSHIPS    = "live_relationships"
    LIVE_QUERY            = "live_query"
    SEMANTIC_QUERY_PLAN   = "semantic_query_plan"
    EXECUTION_PLANNER     = "execution_planner"
    ENTERPRISE_ANSWER     = "enterprise_answer"


@dataclass
class OrchestratorRequest:
    """Incoming request to the Enterprise Orchestrator."""
    query: str
    source_id: Optional[int] = None
    user_id: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ResolvedIntent:
    """Structured intent object produced by IntentResolver."""
    intent_type: IntentType
    confidence: float                          # 0.0–1.0
    required_capabilities: List[ServiceCapability]
    secondary_intents: List[IntentType] = field(default_factory=list)
    extracted_params: Dict[str, Any] = field(default_factory=dict)
    keywords_matched: List[str] = field(default_factory=list)


@dataclass
class ServiceDescriptor:
    """Read-only metadata descriptor for a registered enterprise service."""
    service_id: str
    name: str
    description: str
    module_path: str
    primary_functions: List[str]
    capabilities: List[ServiceCapability]
    requires_source_id: bool = False
    requires_user_id: bool = False
    is_read_only: bool = True


@dataclass
class ServiceCallRecord:
    """Audit record for a single service invocation attempt."""
    service_id: str
    function_name: str
    called_at: datetime
    duration_ms: float
    succeeded: bool
    error: Optional[str] = None


@dataclass
class EvidenceItem:
    """Single normalized piece of evidence collected from a service."""
    evidence_id: str
    source_service: str
    source_function: str
    capability: ServiceCapability
    data: Any
    timestamp: datetime
    confidence: float                          # 0.0–1.0
    governance_state: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidencePackage:
    """Complete evidence collection returned by the Orchestrator."""
    request_id: str
    query: str
    intent: ResolvedIntent
    evidence: List[EvidenceItem]
    service_calls: List[ServiceCallRecord]
    built_at: datetime
    source_id: Optional[int]
    errors: List[str]
    total_evidence_items: int
    services_attempted: int
    services_succeeded: int

    @classmethod
    def empty(
        cls,
        request: OrchestratorRequest,
        intent: ResolvedIntent,
    ) -> EvidencePackage:
        return cls(
            request_id=request.request_id,
            query=request.query,
            intent=intent,
            evidence=[],
            service_calls=[],
            built_at=datetime.utcnow(),
            source_id=request.source_id,
            errors=[],
            total_evidence_items=0,
            services_attempted=0,
            services_succeeded=0,
        )
