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
    # Milestone M-5, Part 5 (2026-07-13): staffing/recruiting business
    # vocabulary, additive to the fixed taxonomy above (none of the
    # existing 10 values were changed or removed).
    "Staffing & Recruiting",
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
