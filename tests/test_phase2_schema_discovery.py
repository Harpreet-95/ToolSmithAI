"""
Smoke tests for Phase 2 Step 1 — Schema Discovery.

Run from the project root:
    venv/Scripts/pytest tests/test_phase2_schema_discovery.py -v
"""

import os

from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

# Connector side-effect imports — populate the ConnectorRegistry.
import core.connectors.relational.mssql       # noqa: F401
import core.connectors.relational.postgresql  # noqa: F401
import core.connectors.relational.mysql       # noqa: F401

from core.connectors.base import DataSourceConfig
from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.relational.mysql import MySQLConnector
from core.connectors.relational.postgresql import PostgreSQLConnector
from core.connectors.schema import (
    ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo, normalize_data_type,
)


# ── 1. normalize_data_type — SQL Server type mapping ─────────────────────────

def test_normalize_integer_types():
    for raw in ('int', 'bigint', 'smallint', 'tinyint', 'integer'):
        assert normalize_data_type(raw) == 'INTEGER', raw

def test_normalize_decimal_types():
    for raw in ('decimal', 'numeric', 'float', 'real', 'money', 'smallmoney'):
        assert normalize_data_type(raw) == 'DECIMAL', raw

def test_normalize_text_types():
    for raw in ('char', 'varchar', 'nchar', 'nvarchar', 'text', 'ntext', 'uniqueidentifier', 'sysname'):
        assert normalize_data_type(raw) == 'TEXT', raw

def test_normalize_datetime_types():
    for raw in ('date', 'datetime', 'datetime2', 'datetimeoffset', 'smalldatetime', 'time'):
        assert normalize_data_type(raw) == 'DATETIME', raw

def test_normalize_boolean_type():
    assert normalize_data_type('bit') == 'BOOLEAN'

def test_normalize_binary_types():
    for raw in ('binary', 'varbinary', 'image'):
        assert normalize_data_type(raw) == 'BINARY', raw

def test_normalize_json_types():
    for raw in ('xml', 'json', 'jsonb'):
        assert normalize_data_type(raw) == 'JSON', raw

def test_normalize_unknown_type_returns_other():
    for raw in ('geography', 'geometry', 'hierarchyid', 'sql_variant', 'cursor'):
        assert normalize_data_type(raw) == 'OTHER', raw

def test_normalize_empty_string_returns_other():
    assert normalize_data_type('') == 'OTHER'

def test_normalize_is_case_insensitive():
    assert normalize_data_type('NVARCHAR') == 'TEXT'
    assert normalize_data_type('INT') == 'INTEGER'
    assert normalize_data_type('DateTime2') == 'DATETIME'
    assert normalize_data_type('BIT') == 'BOOLEAN'

def test_normalize_strips_length_suffix():
    assert normalize_data_type('nvarchar(255)') == 'TEXT'
    assert normalize_data_type('nvarchar(max)') == 'TEXT'
    assert normalize_data_type('decimal(18,2)') == 'DECIMAL'
    assert normalize_data_type('varchar(50)') == 'TEXT'
    assert normalize_data_type('varbinary(max)') == 'BINARY'


# ── 2. SchemaSnapshot count properties ───────────────────────────────────────

def _col(name: str) -> ColumnInfo:
    return ColumnInfo(
        column_name=name, ordinal_position=1,
        data_type='INTEGER', raw_type='int',
        is_nullable=False, is_primary_key=False, is_identity=False,
    )

def test_snapshot_empty_counts_are_zero():
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    assert snap.table_count == 0
    assert snap.view_count == 0
    assert snap.column_count == 0

def test_snapshot_table_and_view_counts():
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    schema = SchemaInfo(schema_name='dbo')
    schema.tables.append(TableInfo(table_name='orders',    schema_name='dbo', table_fqn='dbo.orders',    table_type='TABLE'))
    schema.tables.append(TableInfo(table_name='customers', schema_name='dbo', table_fqn='dbo.customers', table_type='TABLE'))
    schema.tables.append(TableInfo(table_name='vw_summary',schema_name='dbo', table_fqn='dbo.vw_summary',table_type='VIEW'))
    snap.schemas.append(schema)
    assert snap.table_count == 2
    assert snap.view_count == 1

def test_snapshot_column_count():
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    schema = SchemaInfo(schema_name='dbo')
    t = TableInfo(table_name='orders', schema_name='dbo', table_fqn='dbo.orders', table_type='TABLE')
    t.columns.extend([_col('id'), _col('amount'), _col('status')])
    schema.tables.append(t)
    snap.schemas.append(schema)
    assert snap.column_count == 3

def test_snapshot_counts_across_multiple_schemas():
    snap = SchemaSnapshot(source_id=1, source_type='mssql', discovered_at='2026-06-18T00:00:00+00:00')
    for schema_name in ('dbo', 'sales', 'hr'):
        s = SchemaInfo(schema_name=schema_name)
        t = TableInfo(table_name='t1', schema_name=schema_name, table_fqn=f'{schema_name}.t1', table_type='TABLE')
        t.columns.extend([_col('a'), _col('b')])
        s.tables.append(t)
        snap.schemas.append(s)
    assert snap.table_count == 3
    assert snap.column_count == 6


# ── 3. PostgreSQL discover_schema stub ────────────────────────────────────────

def test_postgresql_discover_schema_returns_schema_snapshot():
    result = PostgreSQLConnector().discover_schema(
        DataSourceConfig(source_type='postgresql', params={})
    )
    assert isinstance(result, SchemaSnapshot)

def test_postgresql_discover_schema_empty_schemas():
    result = PostgreSQLConnector().discover_schema(
        DataSourceConfig(source_type='postgresql', params={})
    )
    assert result.schemas == []
    assert result.table_count == 0
    assert result.view_count == 0
    assert result.column_count == 0

def test_postgresql_discover_schema_has_warning():
    result = PostgreSQLConnector().discover_schema(
        DataSourceConfig(source_type='postgresql', params={})
    )
    assert len(result.warnings) >= 1
    assert 'postgresql' in result.warnings[0].lower()


# ── 4. MySQL discover_schema stub ─────────────────────────────────────────────

def test_mysql_discover_schema_returns_schema_snapshot():
    result = MySQLConnector().discover_schema(
        DataSourceConfig(source_type='mysql', params={})
    )
    assert isinstance(result, SchemaSnapshot)

def test_mysql_discover_schema_empty_schemas():
    result = MySQLConnector().discover_schema(
        DataSourceConfig(source_type='mysql', params={})
    )
    assert result.schemas == []
    assert result.table_count == 0

def test_mysql_discover_schema_has_warning():
    result = MySQLConnector().discover_schema(
        DataSourceConfig(source_type='mysql', params={})
    )
    assert len(result.warnings) >= 1
    assert 'mysql' in result.warnings[0].lower()


# ── 5–8. schema_service imports and callability ───────────────────────────────

def test_schema_service_imports_successfully():
    from data.schema_service import (
        run_discovery, get_latest_snapshot,
        list_snapshot_versions, _save_snapshot,
    )
    assert True  # import itself is the assertion

def test_run_discovery_is_callable():
    from data.schema_service import run_discovery
    assert callable(run_discovery)

def test_get_latest_snapshot_is_callable():
    from data.schema_service import get_latest_snapshot
    assert callable(get_latest_snapshot)

def test_list_snapshot_versions_is_callable():
    from data.schema_service import list_snapshot_versions
    assert callable(list_snapshot_versions)


# ── 9. mssql connector has discover_schema ───────────────────────────────────

def test_mssql_has_discover_schema_method():
    assert hasattr(SQLServerConnector, 'discover_schema')
    assert callable(getattr(SQLServerConnector, 'discover_schema'))

def test_mssql_discover_schema_without_pyodbc_returns_snapshot():
    import core.connectors.relational.mssql as m
    original = m._PYODBC_AVAILABLE
    try:
        m._PYODBC_AVAILABLE = False
        result = SQLServerConnector().discover_schema(
            DataSourceConfig(source_type='mssql', params={'_source_id': 0})
        )
        assert isinstance(result, SchemaSnapshot)
        assert result.table_count == 0
        assert result.schemas == []
        assert any('pyodbc' in w.lower() for w in result.warnings)
    finally:
        m._PYODBC_AVAILABLE = original
