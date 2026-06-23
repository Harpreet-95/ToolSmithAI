"""
Smoke tests for Phase 1 Data Source Foundation.

Run from the project root:
    venv/Scripts/pytest tests/test_phase1_datasources.py -v
"""

import json
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

import core.connectors.registry as registry
from core.connectors.base import DataSourceConfig
from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.relational.mysql import MySQLConnector
from core.connectors.relational.postgresql import PostgreSQLConnector
from core.secrets.manager import get_secret_manager
from data.datasource_service import _to_public_record


# ── 1. Registry: list_supported ──────────────────────────────────────────────

def test_registry_lists_mssql_postgresql_mysql():
    supported = registry.list_supported()
    assert "mssql" in supported
    assert "postgresql" in supported
    assert "mysql" in supported


# ── 2. Registry: get by source_type ──────────────────────────────────────────

def test_registry_get_mssql_returns_sqlserver_connector():
    assert registry.get("mssql") is SQLServerConnector


# ── 3. Registry: unknown source_type returns None ────────────────────────────

def test_registry_unknown_returns_none():
    assert registry.get("oracle") is None
    assert registry.get("bigquery") is None
    assert registry.get("") is None


# ── 4. Registry: list_by_category ────────────────────────────────────────────

def test_list_by_category_relational_db_returns_all_three():
    relational = registry.list_by_category("relational_db")
    assert set(relational) == {"mssql", "postgresql", "mysql"}


# ── 5. SQLServerConnector: instantiation ─────────────────────────────────────

def test_mssql_connector_instantiates():
    assert SQLServerConnector() is not None


# ── 6. SQLServerConnector: capabilities ──────────────────────────────────────

def test_mssql_capabilities_include_connection_test():
    assert "connection_test" in SQLServerConnector.supported_capabilities


# ── 7. SQLServerConnector: config summary is credential-free ─────────────────

def test_mssql_config_summary_never_includes_password():
    summary = SQLServerConnector().get_config_summary({
        "host": "server.corp.com",
        "port": 1433,
        "database": "CCPP_DB",
        "username": "svc_toolsmith",
        "password": "super_secret_password",
    })
    assert "super_secret_password" not in summary
    assert "svc_toolsmith" not in summary
    assert "server.corp.com" in summary
    assert "CCPP_DB" in summary


# ── 8. PostgreSQLConnector stub ───────────────────────────────────────────────

def test_postgresql_stub_returns_success_false():
    result = PostgreSQLConnector().test_connectivity(
        DataSourceConfig(source_type="postgresql", params={})
    )
    assert result.success is False
    assert "not yet implemented" in result.message.lower()


# ── 9. MySQLConnector stub ────────────────────────────────────────────────────

def test_mysql_stub_returns_success_false():
    result = MySQLConnector().test_connectivity(
        DataSourceConfig(source_type="mysql", params={})
    )
    assert result.success is False
    assert "not yet implemented" in result.message.lower()


# ── 10. SecretManager: encrypt/decrypt round trip ────────────────────────────

def test_secret_manager_round_trip():
    sm = get_secret_manager()
    plaintext = "super-secret-password-123!@#"
    ciphertext = sm.encrypt_secret(plaintext)
    assert ciphertext != plaintext
    assert sm.decrypt_secret(ciphertext) == plaintext


# ── 11. _to_public_record: removes encrypted_config_json ─────────────────────

def test_to_public_record_removes_encrypted_config_json():
    sm = get_secret_manager()
    config = {"host": "localhost", "port": 1433, "database": "testdb"}
    fake_row = {
        "id": 1,
        "user_id": "test-user",
        "display_name": "Test Source",
        "source_type": "mssql",
        "source_category": "relational_db",
        "encrypted_config_json": sm.encrypt_secret(json.dumps(config)),
        "config_schema_version": 1,
        "capabilities_json": '["connection_test", "sql_query"]',
        "metadata_json": "{}",
        "source_status": "ACTIVE",
        "is_active": 1,
        "last_tested_at": None,
        "last_test_status": None,
        "last_test_message": None,
        "created_at": "2026-06-18T00:00:00+00:00",
        "updated_at": "2026-06-18T00:00:00+00:00",
    }
    result = _to_public_record(fake_row)
    assert "encrypted_config_json" not in result
    assert result["id"] == 1
    assert "config_summary" in result
    assert isinstance(result["capabilities"], list)
    assert isinstance(result["metadata"], dict)


# ── 12. config_schema_version is int ─────────────────────────────────────────

def test_config_schema_version_is_int():
    assert isinstance(SQLServerConnector.config_schema_version, int)
    assert isinstance(PostgreSQLConnector.config_schema_version, int)
    assert isinstance(MySQLConnector.config_schema_version, int)
