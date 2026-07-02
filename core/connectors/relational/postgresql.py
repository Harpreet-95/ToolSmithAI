from datetime import datetime, timezone

from core.connectors.base import ConnectivityTestResult, DataSourceConfig, DataSourceConnector
from core.connectors.registry import register
from core.connectors.schema import SchemaSnapshot
from core.profiling.sql.base import ProfilingQueryBuilder


class _PostgreSQLProfilingQueryBuilder(ProfilingQueryBuilder):
    _MSG = "PostgreSQL profiling query builder not implemented yet."

    def build_row_count_query(self, table_fqn: str) -> str:
        raise NotImplementedError(self._MSG)

    def build_date_range_query(self, table_fqn: str, column_name: str) -> str:
        raise NotImplementedError(self._MSG)

    def build_column_stats_query(
        self, table_fqn: str, column_name: str, data_type: str
    ) -> str:
        raise NotImplementedError(self._MSG)

    def build_top_values_query(
        self, table_fqn: str, column_name: str, limit: int = 20
    ) -> str:
        raise NotImplementedError(self._MSG)

    def build_sample_values_query(
        self,
        table_fqn: str,
        column_name: str,
        limit: int = 10,
        sample_percent: int = 5,
    ) -> str:
        raise NotImplementedError(self._MSG)

    def build_percentile_query(self, table_fqn: str, column_name: str) -> str:
        raise NotImplementedError(self._MSG)

    def build_null_count_query(self, table_fqn: str, column_name: str) -> str:
        raise NotImplementedError(self._MSG)

    def build_distinct_count_query(self, table_fqn: str, column_name: str) -> str:
        raise NotImplementedError(self._MSG)

    def quote_identifier(self, identifier: str) -> str:
        raise NotImplementedError(self._MSG)


@register
class PostgreSQLConnector(DataSourceConnector):
    source_type = "postgresql"
    source_category = "relational_db"
    supported_capabilities = frozenset({
        "connection_test",
        "schema_discovery",
        "sql_query",
        "row_limit",
        "execution_plan",
    })
    config_schema_version = 1

    def test_connectivity(self, config: DataSourceConfig) -> ConnectivityTestResult:
        return ConnectivityTestResult(
            success=False,
            message="PostgreSQL connector is not yet implemented.",
        )

    def get_config_summary(self, params: dict) -> str:
        host = params.get("host", "?")
        port = params.get("port", 5432)
        database = params.get("database", "?")
        return f"postgresql://{host}:{port}/{database}"

    def open_connection(self, config: DataSourceConfig):
        raise NotImplementedError(
            "PostgreSQL live connections are not implemented yet."
        )

    def get_query_builder(self) -> _PostgreSQLProfilingQueryBuilder:
        return _PostgreSQLProfilingQueryBuilder()

    def discover_schema(self, config: DataSourceConfig) -> SchemaSnapshot:
        return SchemaSnapshot(
            source_id=config.params.get("_source_id", 0),
            source_type=self.source_type,
            discovered_at=datetime.now(timezone.utc).isoformat(),
            warnings=["Schema discovery is not yet implemented for PostgreSQL."],
        )
