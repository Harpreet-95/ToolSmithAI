from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.connectors.base import DataSourceConfig, DataSourceConnector


class ConnectionState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    PERMISSION_DENIED = "permission_denied"
    NOT_IMPLEMENTED = "not_implemented"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    INACTIVE = "inactive"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    ERROR = "error"


@dataclass
class ConnectionContext:
    """Resolved, in-memory-only handle to a live data source.

    Holds decrypted connection params via `config`. Never serialize this
    object directly to an API response — callers must extract only safe,
    aggregate fields (e.g. via get_config_summary()).
    """
    source_id: int
    source_type: str
    source_category: str
    display_name: str
    connector_cls: type[DataSourceConnector]
    config: DataSourceConfig
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ResolutionResult:
    status: ResolutionStatus
    context: Optional[ConnectionContext]
    message: str


@dataclass(frozen=True)
class HealthCheckResult:
    state: ConnectionState
    message: str
    latency_ms: Optional[int] = None
