from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from core.connectors.schema import SchemaSnapshot
from core.profiling.sql.base import ProfilingQueryBuilder


@dataclass
class DataSourceConfig:
    source_type: str
    params: dict


@dataclass(frozen=True)
class ConnectivityTestResult:
    success: bool
    message: str
    latency_ms: int | None = None
    detail: str | None = None  # server-side only; never returned in API responses


class DataSourceConnector(ABC):
    source_type: ClassVar[str]
    source_category: ClassVar[str]
    supported_capabilities: ClassVar[frozenset[str]]
    config_schema_version: ClassVar[int]

    @abstractmethod
    def test_connectivity(self, config: DataSourceConfig) -> ConnectivityTestResult: ...

    @abstractmethod
    def get_config_summary(self, params: dict) -> str: ...

    @abstractmethod
    def discover_schema(self, config: DataSourceConfig) -> SchemaSnapshot: ...

    @abstractmethod
    def open_connection(self, config: DataSourceConfig, timeout_s: int | None = None):
        """Return a live DBAPI2-compatible connection. Caller is responsible for closing it.

        timeout_s overrides the connector's normal connect timeout when given —
        used by best-effort/optional callers (e.g. relationship overlap
        validation) that want a short bound instead of the default tuned for
        a user actively waiting on a real query or connection test."""
        ...

    @abstractmethod
    def get_query_builder(self) -> ProfilingQueryBuilder:
        """Return a ProfilingQueryBuilder for this connector's SQL dialect."""
        ...

    def supports_capability(self, capability: str) -> bool:
        return capability in self.supported_capabilities
