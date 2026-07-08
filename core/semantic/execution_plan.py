from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ConceptStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass
class ConceptMatch:
    term: str
    status: ConceptStatus
    matched_tables: list[str] = field(default_factory=list)
    matched_columns: list[dict] = field(default_factory=list)
    matched_domains: list[str] = field(default_factory=list)
    matched_entities: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class SemanticContext:
    """Everything known about the source relevant to this question.

    Each section is the (already-existing) reused service's own return
    shape — this class does not reinterpret or re-derive them, only
    collects them under one object.
    """
    metadata: Optional[dict] = None
    dictionary: Optional[dict] = None
    domains: Optional[dict] = None
    entities: Optional[dict] = None
    relationships: Optional[dict] = None
    knowledge_graph: Optional[dict] = None
    governance: Optional[dict] = None
    business_rules: dict = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    question: str
    concepts: list[ConceptMatch] = field(default_factory=list)
    relevant_tables: list[str] = field(default_factory=list)
    relevant_columns: dict = field(default_factory=dict)
    relationships: dict = field(default_factory=dict)
    required_filters: list[dict] = field(default_factory=list)
    governance_restricted: bool = False
    governance_checks: list[dict] = field(default_factory=list)
    recommended_query_strategy: dict = field(default_factory=dict)
    semantic_context: Optional[SemanticContext] = None
    confidence: int = 0
    warnings: list[dict] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for concept in d["concepts"]:
            concept["status"] = ConceptStatus(concept["status"]).value
        return d
