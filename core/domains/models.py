from __future__ import annotations

from dataclasses import dataclass, field

DOMAIN_UNKNOWN = "Unknown"

SUPPORTED_DOMAINS: tuple[str, ...] = (
    "Identity & Access",
    "Student Lifecycle",
    "Admissions",
    "Alumni",
    "Finance",
    "Communications",
    "Operations",
    "Reference Data",
    "Reporting & Analytics",
    "System / Platform",
    DOMAIN_UNKNOWN,
)


@dataclass
class DomainScore:
    domain: str
    score: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class TableDomainAssignment:
    table_fqn: str
    domain: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    competing_domains: list[DomainScore] = field(default_factory=list)


@dataclass
class DomainDetectionResult:
    assignments: list[TableDomainAssignment] = field(default_factory=list)
    tables_total: int = 0
    tables_assigned: int = 0
    tables_unknown: int = 0
