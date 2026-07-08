"""
Enterprise Live Database Foundation — tests.

Covers LiveConnectionResolver, ConnectionHealthService, LiveMetadataProvider,
and their wiring into the Enterprise Orchestrator. No real SQL Server /
PostgreSQL / Oracle / MySQL instance is reachable in this environment, so
every scenario is exercised via monkeypatched connector calls rather than
a live network connection.

Run from the project root:
    python -m pytest tests/test_live_database_foundation.py -v
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-live-db-foundation!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-live-db-foundation")

# Connector side-effect imports — populate the ConnectorRegistry.
import core.connectors.relational.mssql       # noqa: F401
import core.connectors.relational.mysql       # noqa: F401
import core.connectors.relational.postgresql  # noqa: F401

from core.connectors.base import ConnectivityTestResult, DataSourceConfig
from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.relational.postgresql import PostgreSQLConnector
from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.live.connection_resolver import LiveConnectionResolver
from core.live.health_service import ConnectionHealthService
from core.live.metadata_provider import LiveMetadataProvider
from core.live.models import ConnectionContext, ConnectionState, ResolutionStatus
from core.orchestrator import EnterpriseOrchestrator, IntentType, OrchestratorRequest
from core.orchestrator.registry import ServiceRegistry
import data.datasource_service as datasource_service


def _context(source_type="mssql", connector_cls=SQLServerConnector, capabilities=None):
    return ConnectionContext(
        source_id=1,
        source_type=source_type,
        source_category="relational_db",
        display_name="Test Source",
        connector_cls=connector_cls,
        config=DataSourceConfig(source_type=source_type, params={"host": "db.internal", "database": "CCPP"}),
        capabilities=frozenset(capabilities or {"connection_test", "schema_discovery"}),
    )


# ---------------------------------------------------------------------------
# ConnectionHealthService
# ---------------------------------------------------------------------------

class TestConnectionHealthService:
    def test_online(self, monkeypatch):
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(success=True, message="Connection successful.", latency_ms=12),
        )
        result = ConnectionHealthService().check(_context())
        assert result.state == ConnectionState.ONLINE
        assert result.latency_ms == 12

    def test_auth_failed(self, monkeypatch):
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(
                success=False, message="Connection failed.", detail="Login failed for user 'svc'."
            ),
        )
        result = ConnectionHealthService().check(_context())
        assert result.state == ConnectionState.AUTH_FAILED
        assert "Login failed" not in result.message  # detail never surfaced

    def test_timeout(self, monkeypatch):
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(
                success=False, message="Connection failed.", detail="Connection Timeout Expired."
            ),
        )
        assert ConnectionHealthService().check(_context()).state == ConnectionState.TIMEOUT

    def test_unreachable(self, monkeypatch):
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(
                success=False, message="Connection failed.",
                detail="A network-related or instance-specific error occurred.",
            ),
        )
        assert ConnectionHealthService().check(_context()).state == ConnectionState.UNREACHABLE

    def test_permission_denied(self, monkeypatch):
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(
                success=False, message="Connection failed.", detail="Access is denied to database."
            ),
        )
        assert ConnectionHealthService().check(_context()).state == ConnectionState.PERMISSION_DENIED

    def test_generic_offline(self, monkeypatch):
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(success=False, message="Connection failed.", detail="???"),
        )
        assert ConnectionHealthService().check(_context()).state == ConnectionState.OFFLINE

    def test_stub_connector_reports_not_implemented(self):
        result = ConnectionHealthService().check(
            _context(source_type="postgresql", connector_cls=PostgreSQLConnector)
        )
        assert result.state == ConnectionState.NOT_IMPLEMENTED

    def test_health_check_never_raises_on_connector_exception(self, monkeypatch):
        def _boom(self, config):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(SQLServerConnector, "test_connectivity", _boom)
        result = ConnectionHealthService().check(_context())
        assert result.state == ConnectionState.OFFLINE


# ---------------------------------------------------------------------------
# LiveConnectionResolver
# ---------------------------------------------------------------------------

class TestLiveConnectionResolver:
    def _record(self, **overrides):
        base = {
            "source_type": "mssql",
            "source_category": "relational_db",
            "display_name": "Prod SQL Server",
            "is_active": True,
            "source_status": "ACTIVE",
            "capabilities": ["connection_test", "schema_discovery"],
            "live_query_enabled": False,
            "params": {"host": "db.internal", "database": "CCPP"},
        }
        base.update(overrides)
        return base

    def test_resolves_successfully(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: self._record())
        result = LiveConnectionResolver().resolve(1, "user-1")
        assert result.status == ResolutionStatus.RESOLVED
        assert result.context.source_type == "mssql"
        assert result.context.connector_cls is SQLServerConnector

    def test_missing_source_id_or_user_id(self):
        assert LiveConnectionResolver().resolve(None, "user-1").status == ResolutionStatus.NOT_FOUND
        assert LiveConnectionResolver().resolve(1, None).status == ResolutionStatus.NOT_FOUND

    def test_not_found_or_wrong_owner(self, monkeypatch):
        # get_connection_config's WHERE id=? AND user_id=? already folds
        # "doesn't exist" and "belongs to someone else" into the same None
        # result, matching the rest of the codebase's no-enumeration pattern.
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: None)
        result = LiveConnectionResolver().resolve(999, "user-1")
        assert result.status == ResolutionStatus.NOT_FOUND

    def test_inactive_source(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(is_active=False),
        )
        assert LiveConnectionResolver().resolve(1, "user-1").status == ResolutionStatus.INACTIVE

    def test_error_status_source(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(source_status="ERROR"),
        )
        assert LiveConnectionResolver().resolve(1, "user-1").status == ResolutionStatus.INACTIVE

    def test_unknown_source_type(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(source_type="bigquery"),
        )
        assert LiveConnectionResolver().resolve(1, "user-1").status == ResolutionStatus.ERROR

    def test_oracle_has_no_connector_and_degrades_without_raising(self, monkeypatch):
        # Oracle is listed in the brief as a "supported" database but has no
        # connector implementation at all (not even a stub) — resolution
        # must fail cleanly (ERROR, with a safe message) rather than raise.
        import core.connectors.registry as registry
        assert registry.get("oracle") is None

        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(source_type="oracle", capabilities=[]),
        )
        result = LiveConnectionResolver().resolve(1, "user-1")
        assert result.status == ResolutionStatus.ERROR
        assert result.context is None
        assert "oracle" in result.message.lower()

    def test_unsupported_capability(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(source_type="postgresql", capabilities=["connection_test"]),
        )
        result = LiveConnectionResolver().resolve(1, "user-1", required_capability="schema_discovery")
        assert result.status == ResolutionStatus.UNSUPPORTED_CAPABILITY

    def test_resolved_config_never_serializes_raw_object_with_password_by_default(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(params={"host": "db.internal", "password": "hunter2"}),
        )
        result = LiveConnectionResolver().resolve(1, "user-1")
        # The context itself legitimately carries the password in-memory for
        # connector use — the safety property under test is the *message*,
        # which is the only field of ResolutionResult ever surfaced upward.
        assert "hunter2" not in result.message

    def test_live_query_blocked_when_flag_disabled(self, monkeypatch):
        # sql_query is a supported capability but the per-connection opt-in
        # flag defaults to False — must block with UNAUTHORIZED, not silently
        # allow just because the connector type supports SQL execution.
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(
                capabilities=["connection_test", "schema_discovery", "sql_query"],
                live_query_enabled=False,
            ),
        )
        result = LiveConnectionResolver().resolve(1, "user-1", required_capability="sql_query")
        assert result.status == ResolutionStatus.UNAUTHORIZED
        assert result.context is None
        assert "not enabled" in result.message.lower()

    def test_live_query_allowed_when_flag_enabled(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(
                capabilities=["connection_test", "schema_discovery", "sql_query"],
                live_query_enabled=True,
            ),
        )
        result = LiveConnectionResolver().resolve(1, "user-1", required_capability="sql_query")
        assert result.status == ResolutionStatus.RESOLVED

    def test_live_query_flag_does_not_affect_other_capabilities(self, monkeypatch):
        # Regression guard: the flag must gate sql_query only. Metadata
        # discovery (and, by the same logic, profiling/report generation,
        # which don't call resolve() with required_capability="sql_query"
        # at all) must be unaffected by live_query_enabled being False.
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: self._record(live_query_enabled=False),
        )
        result = LiveConnectionResolver().resolve(1, "user-1", required_capability="schema_discovery")
        assert result.status == ResolutionStatus.RESOLVED

        result_no_capability = LiveConnectionResolver().resolve(1, "user-1")
        assert result_no_capability.status == ResolutionStatus.RESOLVED


# ---------------------------------------------------------------------------
# LiveMetadataProvider
# ---------------------------------------------------------------------------

def _sample_snapshot() -> SchemaSnapshot:
    schema = SchemaInfo(schema_name="dbo")
    table = TableInfo(
        table_name="orders", schema_name="dbo", table_fqn="dbo.orders", table_type="TABLE",
        row_count_estimate=1000,
    )
    table.columns.append(ColumnInfo(
        column_name="id", ordinal_position=1, data_type="INTEGER", raw_type="int",
        is_nullable=False, is_primary_key=True, is_identity=True,
    ))
    table.primary_keys.append(PrimaryKeyInfo(column_name="id", key_ordinal=1))
    table.foreign_keys.append(ForeignKeyInfo(
        fk_name="fk_orders_customer", from_column="customer_id",
        to_schema="dbo", to_table="customers", to_column="id",
    ))
    view = TableInfo(table_name="vw_orders", schema_name="dbo", table_fqn="dbo.vw_orders", table_type="VIEW")
    schema.tables.extend([table, view])
    return SchemaSnapshot(
        source_id=1, source_type="mssql", discovered_at="2026-07-07T00:00:00+00:00",
        schemas=[schema], database_name="CCPP",
    )


class TestLiveMetadataProvider:
    def test_populated_schema(self, monkeypatch):
        monkeypatch.setattr(SQLServerConnector, "discover_schema", lambda self, config: _sample_snapshot())
        result = LiveMetadataProvider().get_metadata(_context())
        assert result["databases"] == ["CCPP"]
        assert result["schemas"] == ["dbo"]
        assert len(result["tables"]) == 1
        assert len(result["views"]) == 1
        assert len(result["columns"]) == 1
        assert len(result["primary_keys"]) == 1
        assert len(result["foreign_keys"]) == 1
        assert result["indexes"] == []

    def test_empty_schema(self, monkeypatch):
        empty = SchemaSnapshot(source_id=1, source_type="mssql", discovered_at="2026-07-07T00:00:00+00:00")
        monkeypatch.setattr(SQLServerConnector, "discover_schema", lambda self, config: empty)
        result = LiveMetadataProvider().get_metadata(_context())
        assert result["tables"] == []
        assert result["columns"] == []
        assert result["warnings"] == []

    def test_large_schema_not_truncated(self, monkeypatch):
        schema = SchemaInfo(schema_name="dbo")
        for i in range(500):
            t = TableInfo(table_name=f"t{i}", schema_name="dbo", table_fqn=f"dbo.t{i}", table_type="TABLE")
            t.columns.append(ColumnInfo(
                column_name="id", ordinal_position=1, data_type="INTEGER", raw_type="int",
                is_nullable=False, is_primary_key=True, is_identity=True,
            ))
            schema.tables.append(t)
        big_snapshot = SchemaSnapshot(
            source_id=1, source_type="mssql", discovered_at="2026-07-07T00:00:00+00:00", schemas=[schema],
        )
        monkeypatch.setattr(SQLServerConnector, "discover_schema", lambda self, config: big_snapshot)
        result = LiveMetadataProvider().get_metadata(_context())
        assert len(result["tables"]) == 500
        assert len(result["columns"]) == 500

    def test_stub_connector_passes_through_warning_without_fabricating_data(self):
        result = LiveMetadataProvider().get_metadata(
            _context(source_type="postgresql", connector_cls=PostgreSQLConnector)
        )
        assert result["tables"] == []
        assert any("not yet implemented" in w.lower() for w in result["warnings"])

    def test_discover_schema_exception_never_raises(self, monkeypatch):
        def _boom(self, config):
            raise RuntimeError("boom")
        monkeypatch.setattr(SQLServerConnector, "discover_schema", _boom)
        result = LiveMetadataProvider().get_metadata(_context())
        assert result["tables"] == []
        assert result["warnings"]


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------

class TestCredentialSafety:
    def test_metadata_dict_never_contains_password(self, monkeypatch):
        monkeypatch.setattr(SQLServerConnector, "discover_schema", lambda self, config: _sample_snapshot())
        ctx = _context()
        ctx.config.params["password"] = "hunter2"
        result = LiveMetadataProvider().get_metadata(ctx)
        assert "hunter2" not in str(result)


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def test_registry_has_at_least_the_live_metadata_service(self):
        # Exact count is asserted in tests/test_live_query_engine.py, which
        # tracks the current total (16, after Phase 7 added "live_query").
        # This test only pins the Phase 6 addition so it doesn't need
        # updating every time a later phase registers another service.
        service_ids = [s.service_id for s in ServiceRegistry().get_all()]
        assert "live_metadata" in service_ids

    def test_live_metadata_service_selected_for_metadata_lookup(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: {
                "source_type": "mssql", "source_category": "relational_db",
                "display_name": "Prod SQL Server", "is_active": True, "source_status": "ACTIVE",
                "capabilities": ["connection_test", "schema_discovery"],
                "params": {"host": "db.internal", "database": "CCPP"},
            },
        )
        monkeypatch.setattr(SQLServerConnector, "discover_schema", lambda self, config: _sample_snapshot())
        monkeypatch.setattr(
            SQLServerConnector, "test_connectivity",
            lambda self, config: ConnectivityTestResult(success=True, message="Connection successful."),
        )

        req = OrchestratorRequest(query="what tables exist", source_id=1, user_id="user-1")
        package = EnterpriseOrchestrator().process(req)

        assert package.intent.intent_type == IntentType.METADATA_LOOKUP
        service_ids = [c.service_id for c in package.service_calls]
        assert "live_metadata" in service_ids

        live_item = next(e for e in package.evidence if e.source_service == "live_metadata")
        assert live_item.data["connection_state"] == "online"
        assert len(live_item.data["tables"]) == 1

    def test_no_source_id_behaves_as_before(self):
        req = OrchestratorRequest(query="what tables exist", source_id=None, user_id="user-1")
        package = EnterpriseOrchestrator().process(req)
        live_items = [e for e in package.evidence if e.source_service == "live_metadata"]
        assert len(live_items) == 1
        assert live_items[0].data is None

    def test_oracle_source_through_full_pipeline_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: {
                "source_type": "oracle", "source_category": "relational_db",
                "display_name": "Legacy Oracle", "is_active": True, "source_status": "ACTIVE",
                "capabilities": [],
                "params": {"host": "oracle.internal"},
            },
        )
        req = OrchestratorRequest(query="what tables exist", source_id=2, user_id="user-1")
        package = EnterpriseOrchestrator().process(req)

        live_item = next(e for e in package.evidence if e.source_service == "live_metadata")
        assert live_item.data["connection_state"] == "error"
        assert "oracle" in live_item.data["message"].lower()
        # Failure is surfaced as evidence, not as a service-call error/exception.
        live_call = next(c for c in package.service_calls if c.service_id == "live_metadata")
        assert live_call.succeeded is True

    def test_postgresql_source_through_full_pipeline_reports_not_implemented(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: {
                "source_type": "postgresql", "source_category": "relational_db",
                "display_name": "Analytics PG", "is_active": True, "source_status": "ACTIVE",
                "capabilities": ["connection_test", "schema_discovery"],
                "params": {"host": "pg.internal", "database": "analytics"},
            },
        )
        req = OrchestratorRequest(query="what tables exist", source_id=3, user_id="user-1")
        package = EnterpriseOrchestrator().process(req)

        live_item = next(e for e in package.evidence if e.source_service == "live_metadata")
        assert live_item.data["connection_state"] == "not_implemented"
        assert live_item.data["tables"] == []
