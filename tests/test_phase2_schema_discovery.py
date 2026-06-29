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


# ── 10. relationship_service — unit tests ─────────────────────────────────────

import json
import sqlite3
import tempfile
import os

from data.relationship_service import (
    _parse_fks_from_snapshot_json,
    persist_relationships,
    extract_and_persist_relationships,
    get_relationships_for_source,
    get_relationships_for_table,
    get_relationship_summary,
)


def _snapshot_json_with_fks() -> str:
    """Minimal valid snapshot_json containing two FK relationships."""
    return json.dumps({
        "source_id": 1, "source_type": "mssql",
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "schemas": [{
            "schema_name": "dbo",
            "tables": [
                {
                    "table_name": "orders",
                    "schema_name": "dbo",
                    "table_fqn": "dbo.orders",
                    "table_type": "TABLE",
                    "row_count_estimate": None,
                    "columns": [], "primary_keys": [],
                    "foreign_keys": [
                        {
                            "fk_name": "FK_orders_customers",
                            "from_column": "customer_id",
                            "to_schema": "dbo",
                            "to_table": "customers",
                            "to_column": "id",
                        },
                        {
                            "fk_name": "FK_orders_products",
                            "from_column": "product_id",
                            "to_schema": "dbo",
                            "to_table": "products",
                            "to_column": "id",
                        },
                    ],
                },
                {
                    "table_name": "customers",
                    "schema_name": "dbo",
                    "table_fqn": "dbo.customers",
                    "table_type": "TABLE",
                    "row_count_estimate": None,
                    "columns": [], "primary_keys": [], "foreign_keys": [],
                },
            ],
        }],
        "database_name": None, "server_name": None,
        "connector_version": None, "discovery_duration_ms": None, "warnings": [],
    })


def _make_rel_db(path: str) -> None:
    """Seed a temp SQLite file with the minimal tables required by relationship_service."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE data_source_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT 'test',
            source_type TEXT NOT NULL DEFAULT 'mssql',
            source_category TEXT NOT NULL DEFAULT 'RELATIONAL',
            encrypted_config_json TEXT NOT NULL DEFAULT '{}',
            config_schema_version INTEGER NOT NULL DEFAULT 1,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            source_status TEXT NOT NULL DEFAULT 'ACTIVE',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE schema_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            snapshot_version INTEGER NOT NULL DEFAULT 1,
            source_type TEXT NOT NULL DEFAULT 'mssql',
            table_count INTEGER NOT NULL DEFAULT 0,
            view_count INTEGER NOT NULL DEFAULT 0,
            column_count INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT NOT NULL,
            discovered_at TEXT NOT NULL DEFAULT '2026-01-01',
            created_at TEXT NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE table_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            snapshot_id INTEGER NOT NULL,
            from_schema TEXT NOT NULL,
            from_table TEXT NOT NULL,
            from_table_fqn TEXT NOT NULL,
            from_column TEXT NOT NULL,
            to_schema TEXT NOT NULL,
            to_table TEXT NOT NULL,
            to_table_fqn TEXT NOT NULL,
            to_column TEXT NOT NULL,
            relationship_name TEXT,
            relationship_type TEXT NOT NULL DEFAULT 'FOREIGN_KEY',
            confidence REAL NOT NULL DEFAULT 1.0,
            evidence_json TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01'
        );
        CREATE UNIQUE INDEX idx_tr_snapshot_uniq
            ON table_relationships (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
    """)
    conn.commit()
    conn.close()


def _db_conn(path: str):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# T1 — extraction produces correct rows from known FK data
def test_parse_fks_extracts_correct_rows():
    rels = _parse_fks_from_snapshot_json(_snapshot_json_with_fks(), snapshot_id=1, source_id=1)
    assert len(rels) == 2

    by_col = {r["from_column"]: r for r in rels}
    assert "customer_id" in by_col
    assert "product_id" in by_col

    r = by_col["customer_id"]
    assert r["from_schema"] == "dbo"
    assert r["from_table"] == "orders"
    assert r["from_table_fqn"] == "dbo.orders"
    assert r["to_schema"] == "dbo"
    assert r["to_table"] == "customers"
    assert r["to_table_fqn"] == "dbo.customers"
    assert r["to_column"] == "id"
    assert r["relationship_name"] == "FK_orders_customers"
    assert r["relationship_type"] == "FOREIGN_KEY"
    assert r["confidence"] == 1.0
    assert r["snapshot_id"] == 1
    assert r["source_id"] == 1


# T2 — persist_relationships is idempotent (INSERT OR IGNORE)
def test_persist_relationships_is_idempotent(tmp_path):
    db_path = str(tmp_path / "rel_idempotent.db")
    _make_rel_db(db_path)

    rels = _parse_fks_from_snapshot_json(_snapshot_json_with_fks(), snapshot_id=1, source_id=1)

    conn = _db_conn(db_path)
    first_insert = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=rels)
    conn.close()

    conn = _db_conn(db_path)
    second_insert = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=rels)
    conn.close()

    assert first_insert == 2
    assert second_insert == 0  # all rows already exist


# T3 — snapshot with no foreign keys returns empty list without error
def test_parse_fks_no_foreign_keys_returns_empty():
    no_fk_json = json.dumps({
        "source_id": 1, "source_type": "mssql",
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "schemas": [{"schema_name": "dbo", "tables": [
            {"table_name": "users", "schema_name": "dbo", "table_fqn": "dbo.users",
             "table_type": "TABLE", "row_count_estimate": None,
             "columns": [], "primary_keys": [], "foreign_keys": []},
        ]}],
        "database_name": None, "server_name": None,
        "connector_version": None, "discovery_duration_ms": None, "warnings": [],
    })
    rels = _parse_fks_from_snapshot_json(no_fk_json, snapshot_id=1, source_id=1)
    assert rels == []


# T4 — malformed snapshot_json returns empty list, does not raise
def test_parse_fks_malformed_json_returns_empty():
    assert _parse_fks_from_snapshot_json("not valid json {{", snapshot_id=99, source_id=1) == []
    assert _parse_fks_from_snapshot_json("", snapshot_id=99, source_id=1) == []
    assert _parse_fks_from_snapshot_json('{"schemas": null}', snapshot_id=99, source_id=1) == []


# T5 — get_relationships_for_source lists rows; unknown source returns None
def test_get_relationships_for_source(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rel_source.db")
    _make_rel_db(db_path)

    conn = _db_conn(db_path)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (10, 'user-A')")
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (5, 10, ?)",
        (_snapshot_json_with_fks(),)
    )
    conn.commit()
    conn.close()

    def mock_get_conn():
        return _db_conn(db_path)

    monkeypatch.setattr("data.relationship_service.get_connection", mock_get_conn)
    monkeypatch.setattr("data.db.get_connection", mock_get_conn)

    result = extract_and_persist_relationships(snapshot_id=5, source_id=10)
    assert result["relationships_found"] == 2
    assert result["relationships_inserted"] == 2

    rows = get_relationships_for_source(source_id=10, user_id="user-A")
    assert rows is not None
    assert len(rows) == 2
    assert all(r["from_table_fqn"] == "dbo.orders" for r in rows)

    not_found = get_relationships_for_source(source_id=99, user_id="user-A")
    assert not_found is None


# T6 — get_relationships_for_table splits outbound and inbound correctly
def test_get_relationships_for_table(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rel_table.db")
    _make_rel_db(db_path)

    conn = _db_conn(db_path)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (20, 'user-B')")
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (7, 20, ?)",
        (_snapshot_json_with_fks(),)
    )
    conn.commit()
    conn.close()

    def mock_get_conn():
        return _db_conn(db_path)

    monkeypatch.setattr("data.relationship_service.get_connection", mock_get_conn)
    monkeypatch.setattr("data.db.get_connection", mock_get_conn)

    extract_and_persist_relationships(snapshot_id=7, source_id=20)

    # orders declares FKs outbound → customers and products
    orders = get_relationships_for_table(source_id=20, user_id="user-B", table_fqn="dbo.orders")
    assert orders is not None
    assert len(orders["outbound"]) == 2
    assert orders["inbound"] == []

    # customers is referenced by orders (inbound), declares none (outbound)
    customers = get_relationships_for_table(source_id=20, user_id="user-B", table_fqn="dbo.customers")
    assert customers is not None
    assert customers["outbound"] == []
    assert len(customers["inbound"]) == 1
    assert customers["inbound"][0]["from_table_fqn"] == "dbo.orders"


# T7 — get_relationship_summary returns correct aggregate counts
def test_get_relationship_summary(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rel_summary.db")
    _make_rel_db(db_path)

    conn = _db_conn(db_path)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (30, 'user-C')")
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_json) VALUES (9, 30, ?)",
        (_snapshot_json_with_fks(),)
    )
    conn.commit()
    conn.close()

    def mock_get_conn():
        return _db_conn(db_path)

    monkeypatch.setattr("data.relationship_service.get_connection", mock_get_conn)
    monkeypatch.setattr("data.db.get_connection", mock_get_conn)

    extract_and_persist_relationships(snapshot_id=9, source_id=30)

    summary = get_relationship_summary(source_id=30, user_id="user-C")
    assert summary is not None
    assert summary["snapshot_id"] == 9
    assert summary["total_relationships"] == 2
    assert summary["tables_with_outbound_fks"] == 1   # only dbo.orders has outbound FKs
    assert summary["tables_referenced_by_fk"] == 2    # dbo.customers and dbo.products
    assert len(summary["most_referenced"]) == 2
