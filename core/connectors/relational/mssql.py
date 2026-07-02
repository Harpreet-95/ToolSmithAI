import logging
import time
from datetime import datetime, timezone

from core.connectors.base import ConnectivityTestResult, DataSourceConfig, DataSourceConnector
from core.connectors.registry import register
from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo,
    SchemaInfo, SchemaSnapshot, TableInfo, normalize_data_type,
)
from core.profiling.sql.base import ProfilingQueryBuilder
from core.profiling.sql.mssql import (
    build_column_stats_query as _col_stats_fn,
    build_date_range_query as _date_range_fn,
    build_percentile_query as _percentile_fn,
    build_row_count_query as _row_count_fn,
    build_sample_values_query as _sample_values_fn,
    build_top_values_query as _top_values_fn,
    _fqn, _q, _nolock,
)

try:
    import pyodbc
    _PYODBC_AVAILABLE = True
except ImportError:
    _PYODBC_AVAILABLE = False

logger = logging.getLogger(__name__)

_ODBC_DRIVER = "ODBC Driver 17 for SQL Server"


class MssqlProfilingQueryBuilder(ProfilingQueryBuilder):
    """SQL Server (T-SQL) implementation of ProfilingQueryBuilder.

    Delegates the five existing query patterns to the proven module-level
    functions in core.profiling.sql.mssql, and adds the two standalone
    null/distinct queries using the same quoting helpers.
    """

    def build_row_count_query(self, table_fqn: str) -> str:
        return _row_count_fn(table_fqn)

    def build_date_range_query(self, table_fqn: str, column_name: str) -> str:
        return _date_range_fn(table_fqn, column_name)

    def build_column_stats_query(
        self, table_fqn: str, column_name: str, data_type: str
    ) -> str:
        return _col_stats_fn(table_fqn, column_name, data_type)

    def build_top_values_query(
        self, table_fqn: str, column_name: str, limit: int = 20
    ) -> str:
        return _top_values_fn(table_fqn, column_name, limit)

    def build_sample_values_query(
        self,
        table_fqn: str,
        column_name: str,
        limit: int = 10,
        sample_percent: int = 5,
    ) -> str:
        return _sample_values_fn(table_fqn, column_name, limit, sample_percent)

    def build_percentile_query(self, table_fqn: str, column_name: str) -> str:
        return _percentile_fn(table_fqn, column_name)

    def build_null_count_query(self, table_fqn: str, column_name: str) -> str:
        t = _fqn(table_fqn)
        c = _q(column_name)
        return (
            f"SELECT COUNT_BIG(*) - COUNT_BIG({c}) AS null_count\n"
            f"FROM {t} WITH (NOLOCK)"
        )

    def build_distinct_count_query(self, table_fqn: str, column_name: str) -> str:
        t = _fqn(table_fqn)
        c = _q(column_name)
        return (
            f"SELECT COUNT_BIG(DISTINCT {c}) AS distinct_count\n"
            f"FROM {t} WITH (NOLOCK)"
        )

    def quote_identifier(self, identifier: str) -> str:
        return _q(identifier)


def _build_conn_str(params: dict) -> str:
    auth_type = params.get("auth_type", "sql")
    host = params["host"]
    port = int(params.get("port", 1433))
    database = params["database"]
    encrypt = "yes" if params.get("encrypt_connection", True) else "no"
    trust_cert = "yes" if params.get("trust_server_certificate", False) else "no"

    segments = [
        f"DRIVER={{{_ODBC_DRIVER}}}",
        f"SERVER={host},{port}",
        f"DATABASE={database}",
        f"Encrypt={encrypt}",
        f"TrustServerCertificate={trust_cert}",
    ]
    if auth_type == "windows":
        segments.append("Trusted_Connection=yes")
    else:
        # Wrap PWD in braces; escape any literal } in the value per ODBC spec.
        pwd = params["password"].replace("}", "}}")
        segments.append(f"UID={params['username']}")
        segments.append(f"PWD={{{pwd}}}")

    return ";".join(segments)


@register
class SQLServerConnector(DataSourceConnector):
    source_type = "mssql"
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
        if not _PYODBC_AVAILABLE:
            return ConnectivityTestResult(
                success=False,
                message="pyodbc is not installed on this server.",
                detail="Install pyodbc and the ODBC Driver 17 for SQL Server.",
            )

        try:
            conn_str = _build_conn_str(config.params)
        except KeyError as exc:
            return ConnectivityTestResult(
                success=False,
                message="Invalid connection configuration.",
                detail=f"Missing required parameter: {exc}",
            )

        start = time.monotonic()
        conn = None
        try:
            conn = pyodbc.connect(conn_str, timeout=10)
            conn.execute("SELECT 1")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ConnectivityTestResult(
                success=True,
                message="Connection successful.",
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error("mssql connectivity test failed")
            return ConnectivityTestResult(
                success=False,
                message="Connection failed.",
                latency_ms=elapsed_ms,
                detail=str(exc),
            )
        finally:
            if conn is not None:
                conn.close()

    def get_config_summary(self, params: dict) -> str:
        host = params.get("host", "?")
        port = params.get("port", 1433)
        database = params.get("database", "?")
        return f"mssql://{host}:{port}/{database}"

    def open_connection(self, config: DataSourceConfig):
        if not _PYODBC_AVAILABLE:
            raise RuntimeError(
                "pyodbc is not installed. Cannot open a live SQL Server connection."
            )
        try:
            conn_str = _build_conn_str(config.params)
        except KeyError as exc:
            raise ValueError(
                f"Invalid SQL Server connection configuration: missing parameter {exc}"
            ) from exc
        return pyodbc.connect(conn_str, timeout=30)

    def get_query_builder(self) -> MssqlProfilingQueryBuilder:
        return MssqlProfilingQueryBuilder()

    def discover_schema(self, config: DataSourceConfig) -> SchemaSnapshot:
        if not _PYODBC_AVAILABLE:
            return SchemaSnapshot(
                source_id=config.params.get("_source_id", 0),
                source_type=self.source_type,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                warnings=["pyodbc is not installed. Cannot discover schema."],
            )

        warnings_list: list[str] = []
        start = time.monotonic()
        conn = None

        try:
            conn_str = _build_conn_str(config.params)
        except KeyError as exc:
            return SchemaSnapshot(
                source_id=config.params.get("_source_id", 0),
                source_type=self.source_type,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                warnings=[f"Invalid connection configuration: {exc}"],
            )

        try:
            conn = pyodbc.connect(conn_str, timeout=30)

            # ── Tables and views ───────────────────────────────────────
            table_rows = conn.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME"
            ).fetchall()

            # ── Columns (with identity detection fallback) ─────────────
            try:
                col_rows = conn.execute(
                    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, "
                    "IS_NULLABLE, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
                    "NUMERIC_SCALE, COLUMN_DEFAULT, "
                    "COLUMNPROPERTY(OBJECT_ID(TABLE_SCHEMA + '.' + TABLE_NAME), "
                    "COLUMN_NAME, 'IsIdentity') AS IS_IDENTITY "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
                ).fetchall()
            except Exception:
                warnings_list.append(
                    "Identity column detection unavailable; IS_IDENTITY defaults to False."
                )
                col_rows = conn.execute(
                    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, "
                    "IS_NULLABLE, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
                    "NUMERIC_SCALE, COLUMN_DEFAULT, 0 AS IS_IDENTITY "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
                ).fetchall()

            # ── Primary keys ───────────────────────────────────────────
            pk_rows = conn.execute(
                "SELECT KCU.TABLE_SCHEMA, KCU.TABLE_NAME, KCU.COLUMN_NAME, KCU.ORDINAL_POSITION "
                "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE KCU "
                "JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS TC "
                "  ON KCU.CONSTRAINT_NAME = TC.CONSTRAINT_NAME "
                "  AND KCU.TABLE_SCHEMA = TC.TABLE_SCHEMA "
                "WHERE TC.CONSTRAINT_TYPE = 'PRIMARY KEY' "
                "ORDER BY KCU.TABLE_SCHEMA, KCU.TABLE_NAME, KCU.ORDINAL_POSITION"
            ).fetchall()

            # ── Foreign keys (requires VIEW DEFINITION; degrades gracefully) ──
            fk_rows: list = []
            try:
                fk_rows = conn.execute(
                    "SELECT fk.name AS fk_name, "
                    "SCHEMA_NAME(fk.schema_id) AS from_schema, "
                    "OBJECT_NAME(fkc.parent_object_id) AS from_table, "
                    "COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS from_column, "
                    "SCHEMA_NAME(ref_t.schema_id) AS to_schema, "
                    "OBJECT_NAME(fkc.referenced_object_id) AS to_table, "
                    "COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS to_column "
                    "FROM sys.foreign_keys fk "
                    "JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id "
                    "JOIN sys.tables ref_t ON fkc.referenced_object_id = ref_t.object_id "
                    "ORDER BY from_schema, from_table, fk_name"
                ).fetchall()
            except Exception:
                warnings_list.append(
                    "Foreign key discovery unavailable; "
                    "VIEW DEFINITION permission may be required."
                )

            # ── Row count estimates (requires VIEW DATABASE STATE; degrades gracefully) ──
            rc_by_table: dict[tuple, int | None] = {}
            try:
                for r in conn.execute(
                    "SELECT SCHEMA_NAME(t.schema_id) AS table_schema, "
                    "t.name AS table_name, SUM(p.rows) AS row_count_estimate "
                    "FROM sys.tables t "
                    "JOIN sys.partitions p ON t.object_id = p.object_id "
                    "WHERE p.index_id IN (0, 1) "
                    "GROUP BY t.schema_id, t.name"
                ).fetchall():
                    rc_by_table[(r.table_schema, r.table_name)] = (
                        int(r.row_count_estimate) if r.row_count_estimate is not None else None
                    )
            except Exception:
                warnings_list.append(
                    "Row count estimates unavailable; "
                    "VIEW DATABASE STATE permission may be required."
                )

            # ── Index raw rows into lookup dicts ───────────────────────
            cols_by_table: dict[tuple, list] = {}
            for r in col_rows:
                cols_by_table.setdefault((r.TABLE_SCHEMA, r.TABLE_NAME), []).append(r)

            pk_names_by_table: dict[tuple, set] = {}
            pk_list_by_table: dict[tuple, list] = {}
            for r in pk_rows:
                key = (r.TABLE_SCHEMA, r.TABLE_NAME)
                pk_names_by_table.setdefault(key, set()).add(r.COLUMN_NAME)
                pk_list_by_table.setdefault(key, []).append(
                    PrimaryKeyInfo(column_name=r.COLUMN_NAME, key_ordinal=int(r.ORDINAL_POSITION))
                )

            fk_by_table: dict[tuple, list] = {}
            for r in fk_rows:
                fk_by_table.setdefault((r.from_schema, r.from_table), []).append(
                    ForeignKeyInfo(
                        fk_name=r.fk_name,
                        from_column=r.from_column,
                        to_schema=r.to_schema,
                        to_table=r.to_table,
                        to_column=r.to_column,
                    )
                )

            tables_by_schema: dict[str, list] = {}
            for r in table_rows:
                tables_by_schema.setdefault(r.TABLE_SCHEMA, []).append(r)

            # ── Build SchemaInfo tree ──────────────────────────────────
            schemas: list[SchemaInfo] = []
            for schema_name in sorted(tables_by_schema):
                schema_tables: list[TableInfo] = []
                for trow in tables_by_schema[schema_name]:
                    key = (trow.TABLE_SCHEMA, trow.TABLE_NAME)
                    table_type = 'VIEW' if trow.TABLE_TYPE == 'VIEW' else 'TABLE'
                    pk_col_names = pk_names_by_table.get(key, set())

                    columns: list[ColumnInfo] = []
                    for crow in cols_by_table.get(key, []):
                        raw_t = crow.DATA_TYPE or ''
                        max_len = crow.CHARACTER_MAXIMUM_LENGTH
                        columns.append(ColumnInfo(
                            column_name=crow.COLUMN_NAME,
                            ordinal_position=int(crow.ORDINAL_POSITION),
                            data_type=normalize_data_type(raw_t),
                            raw_type=raw_t,
                            is_nullable=crow.IS_NULLABLE == 'YES',
                            is_primary_key=crow.COLUMN_NAME in pk_col_names,
                            # NULL from COLUMNPROPERTY when OBJECT_ID is unresolvable
                            is_identity=bool(crow.IS_IDENTITY) if crow.IS_IDENTITY is not None else False,
                            max_length=int(max_len) if max_len is not None and max_len != -1 else None,
                            precision=int(crow.NUMERIC_PRECISION) if crow.NUMERIC_PRECISION is not None else None,
                            scale=int(crow.NUMERIC_SCALE) if crow.NUMERIC_SCALE is not None else None,
                            default_value=str(crow.COLUMN_DEFAULT) if crow.COLUMN_DEFAULT is not None else None,
                        ))

                    schema_tables.append(TableInfo(
                        table_name=trow.TABLE_NAME,
                        schema_name=trow.TABLE_SCHEMA,
                        table_fqn=f"{trow.TABLE_SCHEMA}.{trow.TABLE_NAME}",
                        table_type=table_type,
                        row_count_estimate=rc_by_table.get(key),
                        columns=columns,
                        primary_keys=pk_list_by_table.get(key, []),
                        foreign_keys=fk_by_table.get(key, []),
                    ))
                schemas.append(SchemaInfo(schema_name=schema_name, tables=schema_tables))

            elapsed_ms = int((time.monotonic() - start) * 1000)
            return SchemaSnapshot(
                source_id=config.params.get("_source_id", 0),
                source_type=self.source_type,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                schemas=schemas,
                database_name=config.params.get("database"),
                server_name=config.params.get("host"),
                connector_version=pyodbc.version,
                discovery_duration_ms=elapsed_ms,
                warnings=warnings_list,
            )

        except Exception:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error("mssql schema discovery failed")
            return SchemaSnapshot(
                source_id=config.params.get("_source_id", 0),
                source_type=self.source_type,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                discovery_duration_ms=elapsed_ms,
                warnings=["Schema discovery failed. Check connection parameters and server availability."],
            )

        finally:
            if conn is not None:
                conn.close()
