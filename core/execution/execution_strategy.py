from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StrategyType(str, Enum):
    DICTIONARY_LOOKUP = "dictionary_lookup"
    METADATA_LOOKUP = "metadata_lookup"
    GOVERNANCE_CHECK = "governance_check"
    PROFILING = "profiling"
    RELATIONSHIP_LOOKUP = "relationship_lookup"
    KNOWLEDGE_GRAPH_LOOKUP = "knowledge_graph_lookup"
    EXPLAIN_ENTITY = "explain_entity"
    REPORT_GENERATION = "report_generation"
    SQL_REQUIRED = "sql_required"
    WORKFLOW = "workflow"
    GENERAL_QUESTION = "general_question"
    UNKNOWN = "unknown"


@dataclass
class ExecutionStrategy:
    """
    A decision, not an action. Describes HOW a request should be answered —
    which existing enterprise services would be involved and in what order —
    without calling, executing, or generating anything.
    """
    strategy_type: StrategyType
    required_services: list[str] = field(default_factory=list)
    execution_order: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    governance_checks: list[str] = field(default_factory=list)
    requires_live_data: bool = False
    requires_sql: bool = False
    requires_report: bool = False
    requires_search: bool = False
    requires_dictionary: bool = False
    requires_profiling: bool = False
    requires_metadata: bool = False
    requires_workflow: bool = False
    warnings: list[str] = field(default_factory=list)
    confidence: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["strategy_type"] = StrategyType(d["strategy_type"]).value
        return d
