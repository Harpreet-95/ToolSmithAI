from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AnswerType(str, Enum):
    DICTIONARY = "dictionary"
    METADATA = "metadata"
    DOMAIN = "domain"
    ENTITY = "entity"
    RELATIONSHIP = "relationship"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    PROFILING = "profiling"
    GOVERNANCE = "governance"
    REVIEW = "review"
    LIVE_METADATA = "live_metadata"
    LIVE_QUERY = "live_query"
    REPORT = "report"
    SEARCH = "search"
    CROSS_SOURCE_SUMMARY = "cross_source_summary"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class CitationType(str, Enum):
    METADATA_SOURCE = "metadata_source"
    TABLE = "table"
    COLUMN = "column"
    DICTIONARY_ENTRY = "dictionary_entry"
    DOMAIN = "domain"
    ENTITY = "entity"
    RELATIONSHIP = "relationship"
    REPORT = "report"
    LIVE_QUERY = "live_query"
    PROFILING = "profiling"
    KNOWLEDGE_GRAPH = "knowledge_graph"


@dataclass(frozen=True)
class Citation:
    source_type: CitationType
    reference: str
    label: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Recommendation:
    type: str
    message: str
    priority: str = "MEDIUM"


@dataclass
class EnterpriseAnswer:
    answer: str
    summary: str
    answer_type: AnswerType
    confidence: int
    citations: list[Citation] = field(default_factory=list)
    governance_warnings: list[str] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    follow_up_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    related_objects: list[str] = field(default_factory=list)
    execution_summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["answer_type"] = AnswerType(d["answer_type"]).value
        for c in d["citations"]:
            c["source_type"] = CitationType(c["source_type"]).value
        return d
