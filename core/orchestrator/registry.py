from __future__ import annotations

from typing import Dict, List, Optional

from core.orchestrator.interfaces import IServiceRegistry
from core.orchestrator.models import ServiceCapability, ServiceDescriptor


# All 14 read-only enterprise services registered in ToolSmithAI.
# Pure metadata — no service modules imported here.
_REGISTRY: List[ServiceDescriptor] = [
    ServiceDescriptor(
        service_id="dictionary",
        name="Data Dictionary Service",
        description="Business glossary: table/column descriptions, PII flags, AI suggestions",
        module_path="data.dictionary_service",
        primary_functions=["list_dictionary_tables"],
        capabilities=[ServiceCapability.DICTIONARY_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="domain",
        name="Domain Classification Service",
        description="Business domain assignments and learned rules for source tables",
        module_path="data.domain_service",
        primary_functions=["get_domain_summary"],
        capabilities=[ServiceCapability.DOMAIN_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="entity",
        name="Entity Classification Service",
        description="Master data entity detection and entity rule inventory",
        module_path="data.entity_service",
        primary_functions=["get_entity_summary"],
        capabilities=[ServiceCapability.ENTITY_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="profiling",
        name="Data Profiling Service",
        description="Statistical column profiles, quality scores, and review tasks",
        module_path="data.profiling_service",
        primary_functions=["get_latest_profile"],
        capabilities=[ServiceCapability.PROFILING_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="governance",
        name="Governance Service",
        description="Governance readiness, policy state, and lifecycle KPIs",
        module_path="data.governance_service",
        primary_functions=["governance_readiness_summary"],
        capabilities=[ServiceCapability.GOVERNANCE_READ],
        requires_source_id=False,
        requires_user_id=False,
    ),
    ServiceDescriptor(
        service_id="relationship",
        name="Relationship Intelligence Service",
        description="Foreign key and inferred join relationships across source tables",
        module_path="data.relationship_service",
        primary_functions=["get_relationship_summary"],
        capabilities=[ServiceCapability.RELATIONSHIP_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="knowledge_graph",
        name="Knowledge Graph Service",
        description="Semantic table network: related tables and business asset discovery",
        module_path="data.knowledge_graph_service",
        primary_functions=["knowledge_graph_summary"],
        capabilities=[ServiceCapability.KNOWLEDGE_GRAPH_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="lineage",
        name="Data Lineage Service",
        description="Upstream/downstream data lineage and critical asset identification",
        module_path="data.lineage_service",
        primary_functions=["lineage_summary"],
        capabilities=[ServiceCapability.LINEAGE_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="semantic_layer",
        name="Semantic Layer Service",
        description="Join intelligence, ambiguity detection, and semantic table profiles",
        module_path="data.semantic_layer_service",
        primary_functions=["semantic_summary"],
        capabilities=[ServiceCapability.SEMANTIC_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="business_knowledge",
        name="Business Knowledge Service",
        description="Business context summaries for sources, tables, and columns",
        module_path="data.business_knowledge_service",
        primary_functions=["get_business_summary"],
        capabilities=[ServiceCapability.BUSINESS_CONTEXT_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="reports",
        name="Report Service",
        description="Report index and metadata for saved dataset analysis reports",
        module_path="data.report_service",
        primary_functions=["list_reports_for_user"],
        capabilities=[ServiceCapability.REPORT_READ],
        requires_source_id=False,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="workflow",
        name="Workflow Service",
        description="Saved workflows and execution history for the requesting user",
        module_path="data.workflow_service",
        primary_functions=["list_workflows"],
        capabilities=[ServiceCapability.WORKFLOW_READ],
        requires_source_id=False,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="schema",
        name="Schema Discovery Service",
        description="Latest structural schema snapshot for a data source",
        module_path="data.schema_service",
        primary_functions=["get_latest_snapshot"],
        capabilities=[ServiceCapability.SCHEMA_READ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="search",
        name="Metadata Search Service",
        description="Full-text search across all governed metadata assets",
        module_path="data.search_service",
        primary_functions=["search_metadata"],
        capabilities=[ServiceCapability.SEARCH_READ],
        requires_source_id=False,
        requires_user_id=False,
    ),
    ServiceDescriptor(
        service_id="live_metadata",
        name="Live Database Metadata Service",
        description="Read-only live schema/table/column/relationship discovery against enterprise databases",
        module_path="core.live.metadata_provider",
        primary_functions=["get_metadata"],
        capabilities=[
            ServiceCapability.LIVE_METADATA,
            ServiceCapability.LIVE_SCHEMA,
            ServiceCapability.LIVE_TABLES,
            ServiceCapability.LIVE_COLUMNS,
            ServiceCapability.LIVE_RELATIONSHIPS,
        ],
        requires_source_id=True,
        requires_user_id=True,
    ),
    ServiceDescriptor(
        service_id="live_query",
        name="Live Query Engine",
        description="Read-only execution of trusted, pre-validated SQL against enterprise databases",
        module_path="core.live.query_engine",
        primary_functions=["execute"],
        capabilities=[ServiceCapability.LIVE_QUERY],
        requires_source_id=True,
        requires_user_id=True,
        is_read_only=True,
    ),
    ServiceDescriptor(
        service_id="semantic_query_plan",
        name="Semantic Query Planner",
        description="Converts a business question into a structured, non-SQL execution plan",
        module_path="core.semantic.planner",
        primary_functions=["plan"],
        capabilities=[ServiceCapability.SEMANTIC_QUERY_PLAN],
        requires_source_id=True,
        requires_user_id=True,
        is_read_only=True,
    ),
    ServiceDescriptor(
        service_id="execution_planner",
        name="Execution Planner",
        description="Decides which enterprise services should answer a request and in what order — never executes anything",
        module_path="core.execution.planner",
        primary_functions=["plan"],
        capabilities=[ServiceCapability.EXECUTION_PLANNER],
        requires_source_id=False,
        requires_user_id=False,
        is_read_only=True,
    ),
    ServiceDescriptor(
        service_id="enterprise_answer",
        name="Enterprise Answer Generation Layer",
        description="Converts an Execution Strategy and Evidence Package into a deterministic, non-AI enterprise answer",
        module_path="core.answering.answer_planner",
        primary_functions=["build"],
        capabilities=[ServiceCapability.ENTERPRISE_ANSWER],
        requires_source_id=False,
        requires_user_id=False,
        is_read_only=True,
    ),
]

_BY_ID: Dict[str, ServiceDescriptor] = {s.service_id: s for s in _REGISTRY}


class ServiceRegistry(IServiceRegistry):
    """
    Read-only registry of every enterprise service available in ToolSmithAI.
    Returns descriptor metadata only — no service logic lives here.
    """

    def get_all(self) -> List[ServiceDescriptor]:
        return list(_REGISTRY)

    def get_by_capability(
        self, capabilities: List[ServiceCapability]
    ) -> List[ServiceDescriptor]:
        cap_set = set(capabilities)
        return [s for s in _REGISTRY if cap_set.intersection(s.capabilities)]

    def get_by_id(self, service_id: str) -> Optional[ServiceDescriptor]:
        return _BY_ID.get(service_id)
