from __future__ import annotations

from dataclasses import dataclass, field

ENTITY_UNKNOWN = "Unknown"

SUPPORTED_ENTITIES: tuple[str, ...] = (
    "Student",
    "Applicant",
    "User",
    "Employee",
    "Course",
    "Program",
    "Department",
    "Payment",
    "Vendor",
    "Campaign",
    "Event",
    # Milestone M-5, Part 5 (2026-07-13): staffing/recruiting business
    # vocabulary, additive to the fixed taxonomy above (none of the
    # existing 11 values were changed or removed).
    "Client",
    "Candidate",
    "Placement",
    "Job",
    ENTITY_UNKNOWN,
)


@dataclass
class EntityScore:
    entity: str
    score: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class TableEntityAssignment:
    table_fqn: str
    entity: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    competing_entities: list[EntityScore] = field(default_factory=list)


@dataclass
class EntityDetectionResult:
    assignments: list[TableEntityAssignment] = field(default_factory=list)
    tables_total: int = 0
    tables_assigned: int = 0
    tables_unknown: int = 0
