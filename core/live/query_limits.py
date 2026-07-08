from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from data.query_execution_service import (
    DEFAULT_QUERY_TIMEOUT_S,
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
)

# New ceilings this phase adds — not present in data.query_execution_service,
# which never accepted caller-requested timeouts/pagination/payload caps.
MAX_TIMEOUT_S: int = 60
DEFAULT_PAGE_SIZE: int = 100
MAX_PAGE_SIZE: int = 1_000
DEFAULT_MAX_PAYLOAD_BYTES: int = 5_000_000


@dataclass(frozen=True)
class QueryLimits:
    row_limit: int
    timeout_s: int
    max_payload_bytes: int
    page: int
    page_size: int


def resolve_limits(
    row_limit: Optional[int] = None,
    timeout_s: Optional[int] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    max_payload_bytes: Optional[int] = None,
) -> QueryLimits:
    """Clamp every caller-requested value to its ceiling. Never trusts
    caller input above the max; missing values fall back to the default."""
    resolved_row_limit = (
        min(int(row_limit), MAX_ROW_LIMIT) if row_limit else DEFAULT_ROW_LIMIT
    )
    resolved_timeout = (
        min(int(timeout_s), MAX_TIMEOUT_S) if timeout_s else DEFAULT_QUERY_TIMEOUT_S
    )
    resolved_page = max(1, int(page)) if page else 1
    resolved_page_size = (
        min(int(page_size), MAX_PAGE_SIZE) if page_size else DEFAULT_PAGE_SIZE
    )
    resolved_payload_cap = (
        min(int(max_payload_bytes), DEFAULT_MAX_PAYLOAD_BYTES)
        if max_payload_bytes else DEFAULT_MAX_PAYLOAD_BYTES
    )

    return QueryLimits(
        row_limit=resolved_row_limit,
        timeout_s=resolved_timeout,
        max_payload_bytes=resolved_payload_cap,
        page=resolved_page,
        page_size=resolved_page_size,
    )
