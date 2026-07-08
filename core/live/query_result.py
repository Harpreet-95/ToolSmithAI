from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class QueryStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    CANCELLED = "cancelled"


@dataclass
class QueryResult:
    execution_id: str
    status: QueryStatus
    source_id: int
    executed_at: str
    duration_ms: int
    columns: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    row_limit_applied: int = 0
    page: int = 1
    page_size: int = 0
    has_more: bool = False
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return d
