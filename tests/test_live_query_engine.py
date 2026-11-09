"""
Enterprise Live Query Engine (Phase 7) — tests.

Covers query_validator (read-only enforcement), query_limits (clamping),
LiveQueryEngine (execution, pagination, payload cap, timeout, cancel). No
real SQL Server / PostgreSQL / Oracle / MySQL instance is reachable in this
environment, so execution scenarios use a fake DBAPI2 connection/cursor.

Run from the project root:
    python -m pytest tests/test_live_query_engine.py -v
"""
from __future__ import annotations

import os
import time

import pytest
from cryptography.fernet import Fernet

# Must be set before any import that transitively loads core.config.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-live-query-engine!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-live-query-engine")

# Connector side-effect imports — populate the ConnectorRegistry.
import core.connectors.relational.mssql       # noqa: F401
import core.connectors.relational.mysql       # noqa: F401
import core.connectors.relational.postgresql  # noqa: F401

from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.relational.postgresql import PostgreSQLConnector
from core.live.query_engine import LiveQueryEngine
from core.live.query_limits import (
    DEFAULT_MAX_PAYLOAD_BYTES, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE,
    MAX_TIMEOUT_S, resolve_limits,
)
from core.live.query_result import QueryStatus
from core.live.query_validator import validate as validate_sql
import data.datasource_service as datasource_service


# ---------------------------------------------------------------------------
# Rate limiting and the audit log read/write the real shared data/toolsmith.db
# keyed by user_id. Tests in this file reuse "user-1" in rapid succession,
# which would otherwise trip the genuine per-user rate limit and pollute the
# shared execution log. Bypass both by default; test_rate_limit_* below
# re-enables the checks explicitly to verify that wiring still works.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bypass_rate_limits_and_audit(monkeypatch):
    import data.query_execution_service as qes
    monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: False)
    monkeypatch.setattr(qes, "_check_daily_limit", lambda user_id: 0)
    monkeypatch.setattr(qes, "_check_source_rate", lambda source_id: 0)
    monkeypatch.setattr(qes, "_check_repeated_query", lambda user_id, sql_hash: 0)
    monkeypatch.setattr(qes, "log_query_execution", lambda *a, **k: None)
    monkeypatch.setattr(qes, "_write_audit", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# query_validator
# ---------------------------------------------------------------------------

class TestQueryValidator:
    def test_valid_select(self):
        assert validate_sql("SELECT * FROM orders", "mssql").is_valid

    def test_valid_cte(self):
        sql = "WITH recent AS (SELECT id FROM orders) SELECT * FROM recent"
        assert validate_sql(sql, "mssql").is_valid

    def test_valid_explain(self):
        assert validate_sql("EXPLAIN SELECT * FROM orders", "postgresql").is_valid

    def test_valid_describe(self):
        assert validate_sql("DESCRIBE orders", "mysql").is_valid

    def test_show_allowed_for_mysql(self):
        assert validate_sql("SHOW TABLES", "mysql").is_valid

    def test_show_allowed_for_postgresql(self):
        assert validate_sql("SHOW search_path", "postgresql").is_valid

    def test_show_rejected_for_mssql(self):
        result = validate_sql("SHOW TABLES", "mssql")
        assert not result.is_valid

    def test_rejects_update(self):
        assert not validate_sql("UPDATE orders SET status='x'", "mssql").is_valid

    def test_rejects_delete(self):
        assert not validate_sql("DELETE FROM orders", "mssql").is_valid

    def test_rejects_drop(self):
        assert not validate_sql("DROP TABLE orders", "mssql").is_valid

    def test_rejects_alter(self):
        assert not validate_sql("ALTER TABLE orders ADD col INT", "mssql").is_valid

    def test_rejects_create(self):
        assert not validate_sql("CREATE TABLE x (id INT)", "mssql").is_valid

    def test_rejects_truncate(self):
        assert not validate_sql("TRUNCATE TABLE orders", "mssql").is_valid

    def test_rejects_merge(self):
        assert not validate_sql("MERGE INTO orders USING x ON 1=1", "mssql").is_valid

    def test_rejects_exec(self):
        assert not validate_sql("EXEC sp_help", "mssql").is_valid

    def test_rejects_call(self):
        assert not validate_sql("CALL my_procedure()", "mysql").is_valid

    def test_rejects_stored_procedure_shape(self):
        assert not validate_sql("SELECT sp_execute_something()", "mssql").is_valid

    def test_rejects_multiple_statements(self):
        result = validate_sql("SELECT 1; SELECT 2", "mssql")
        assert not result.is_valid
        assert any("multiple statements" in r.lower() for r in result.blocking_reasons)

    def test_rejects_dash_comment_hiding_sql(self):
        result = validate_sql("SELECT * FROM orders -- ; DROP TABLE orders", "mssql")
        assert not result.is_valid
        assert any("comment" in r.lower() for r in result.blocking_reasons)

    def test_rejects_block_comment_hiding_sql(self):
        result = validate_sql("/* SELECT 1 */ DROP TABLE orders", "mssql")
        assert not result.is_valid

    def test_rejects_empty_sql(self):
        assert not validate_sql("", "mssql").is_valid
        assert not validate_sql("   ", "mssql").is_valid
        assert not validate_sql(None, "mssql").is_valid

    def test_write_keyword_inside_quoted_identifier_not_falsely_blocked(self):
        # "DROP" appearing only inside a bracketed identifier name is not a
        # real DDL statement — matches data.query_execution_service's rule.
        result = validate_sql('SELECT [DROP_COUNT] FROM orders', "mssql")
        assert result.is_valid


# ---------------------------------------------------------------------------
# query_limits
# ---------------------------------------------------------------------------

class TestQueryLimits:
    def test_defaults(self):
        limits = resolve_limits()
        assert limits.page == 1
        assert limits.page_size == DEFAULT_PAGE_SIZE
        assert limits.max_payload_bytes == DEFAULT_MAX_PAYLOAD_BYTES

    def test_row_limit_clamped_to_max(self):
        limits = resolve_limits(row_limit=999_999)
        from data.query_execution_service import MAX_ROW_LIMIT
        assert limits.row_limit == MAX_ROW_LIMIT

    def test_timeout_clamped_to_max(self):
        limits = resolve_limits(timeout_s=999)
        assert limits.timeout_s == MAX_TIMEOUT_S

    def test_page_size_clamped_to_max(self):
        limits = resolve_limits(page_size=999_999)
        assert limits.page_size == MAX_PAGE_SIZE

    def test_page_floor_is_one(self):
        limits = resolve_limits(page=0)
        assert limits.page == 1


# ---------------------------------------------------------------------------
# LiveQueryEngine — fake DBAPI2 connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, description, rows, delay=0.0, raise_exc=None):
        self.description = description
        self._rows = rows
        self._delay = delay
        self._raise_exc = raise_exc

    def execute(self, sql, params):
        if self._delay:
            time.sleep(self._delay)
        if self._raise_exc:
            raise self._raise_exc

    def fetchmany(self, n):
        return self._rows[:n]


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _mssql_record(**overrides):
    base = {
        "source_type": "mssql",
        "source_category": "relational_db",
        "display_name": "Prod SQL Server",
        "is_active": True,
        "source_status": "ACTIVE",
        "capabilities": ["connection_test", "schema_discovery", "sql_query"],
        "live_query_enabled": True,
        "params": {"host": "db.internal", "database": "EXAMPLECO"},
    }
    base.update(overrides)
    return base


class TestLiveQueryEngineExecute:
    def test_successful_execution(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",), ("name",)]
        rows = [(1, "a"), (2, "b"), (3, "c")]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT id, name FROM orders")
        assert result.status == QueryStatus.SUCCESS
        assert result.row_count == 3
        assert len(result.rows) == 3
        assert result.columns[0]["name"] == "id"
        assert fake_conn.closed is True
    def test_row_limit_truncation(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",)]
        rows = [(i,) for i in range(10)]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT id FROM orders", row_limit=5)
        assert result.row_count == 5
        assert result.truncated is True
        assert result.row_limit_applied == 5

    def test_pagination_across_result_set(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",)]
        rows = [(i,) for i in range(25)]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        page1 = LiveQueryEngine().execute(1, "user-1", "SELECT id FROM orders", page=1, page_size=10)
        assert len(page1.rows) == 10
        assert page1.has_more is True

        fake_conn2 = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn2)
        page3 = LiveQueryEngine().execute(1, "user-1", "SELECT id FROM orders", page=3, page_size=10)
        assert len(page3.rows) == 5
        assert page3.has_more is False

    def test_payload_size_truncation(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("blob",)]
        rows = [("x" * 1000,) for _ in range(50)]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(
            1, "user-1", "SELECT blob FROM big_table", max_payload_bytes=2000
        )
        assert result.status == QueryStatus.SUCCESS
        assert result.truncated is True
        assert any("payload" in w.lower() for w in result.warnings)

    def test_timeout(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",)]
        fake_conn = _FakeConnection(_FakeCursor(description, [(1,)], delay=1.5))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT id FROM orders", timeout_s=1)
        assert result.status == QueryStatus.TIMEOUT

    def test_connection_failure(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        def _boom(self, config):
            raise RuntimeError("network unreachable")
        monkeypatch.setattr(SQLServerConnector, "open_connection", _boom)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.FAILED

    def test_permission_denied_source_not_found(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: None)
        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.BLOCKED

    def test_inactive_source_blocked(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: _mssql_record(is_active=False),
        )
        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.BLOCKED

    def test_oracle_no_connector_never_raises(self, monkeypatch):
        import core.connectors.registry as registry
        assert registry.get("oracle") is None
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: _mssql_record(source_type="oracle"),
        )
        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.BLOCKED
        assert "oracle" in result.error.lower()

    def test_postgresql_stub_open_connection_maps_to_failed(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: _mssql_record(source_type="postgresql"),
        )
        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.FAILED

    def test_dangerous_sql_blocked_before_connection_opened(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        opened = {"called": False}

        def _track_open(self, config):
            opened["called"] = True
            raise AssertionError("open_connection must not be called for invalid SQL")
        monkeypatch.setattr(SQLServerConnector, "open_connection", _track_open)

        result = LiveQueryEngine().execute(1, "user-1", "DROP TABLE orders")
        assert result.status == QueryStatus.BLOCKED
        assert opened["called"] is False

    def test_missing_capability_blocked(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: _mssql_record(capabilities=["connection_test"]),
        )
        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.BLOCKED

    def test_blocked_when_live_query_not_enabled(self, monkeypatch):
        # sql_query capability is present (connector supports it) but the
        # per-connection opt-in flag is off — must block before opening a
        # connection, same as any other resolution failure.
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: _mssql_record(live_query_enabled=False),
        )
        opened = {"called": False}

        def _track_open(self, config):
            opened["called"] = True
            raise AssertionError("open_connection must not be called when live_query_enabled is False")
        monkeypatch.setattr(SQLServerConnector, "open_connection", _track_open)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.BLOCKED
        assert "not enabled" in result.error.lower()
        assert opened["called"] is False

    def test_rate_limit_exceeded_returns_rate_limited(self, monkeypatch):
        # Overrides the module's autouse bypass to verify the reused
        # data.query_execution_service rate-limit wiring actually works.
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        import data.query_execution_service as qes
        monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: True)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1")
        assert result.status == QueryStatus.RATE_LIMITED

    def test_default_execution_kind_still_enforces_user_rate_limit(self, monkeypatch):
        # Day 4, Capability 2 — regression guard: the new execution_kind
        # parameter must not weaken the 2-second collision window for any
        # existing caller (none of which pass it, so they all keep
        # execution_kind="user_query" implicitly).
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        import data.query_execution_service as qes
        monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: True)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1", execution_kind="user_query")
        assert result.status == QueryStatus.RATE_LIMITED

    def test_non_user_query_kind_exempt_from_user_rate_limit(self, monkeypatch):
        # Day 4, Capability 2 — a bounded, same-turn supplementary query
        # (data.insight_service's period comparison) must never be blocked
        # by the primary query's own just-written log row. Mirrors the
        # existing investigation-probe exemption pattern.
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",)]
        fake_conn = _FakeConnection(_FakeCursor(description, [(1,)]))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
        import data.query_execution_service as qes
        monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: True)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1", execution_kind="insight_comparison")
        assert result.status == QueryStatus.SUCCESS

    def test_non_user_query_kind_still_enforces_daily_limit(self, monkeypatch):
        # Day 4, Capability 2 — only the tight per-user collision window is
        # exempt; daily/source-rate safeguards must keep counting every
        # execution_kind, so real query volume is still throttled.
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        import data.query_execution_service as qes
        monkeypatch.setattr(qes, "_check_daily_limit", lambda user_id: qes.DAILY_LIMIT)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT 1", execution_kind="insight_comparison")
        assert result.status == QueryStatus.RATE_LIMITED

    def test_execution_kind_is_recorded_on_log_query_execution(self, monkeypatch):
        # Day 4, Capability 2 — the comparison query must be logged under
        # its own kind (audit/observability), not silently as "user_query".
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",)]
        fake_conn = _FakeConnection(_FakeCursor(description, [(1,)]))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
        import data.query_execution_service as qes
        recorded = {}
        original = qes.log_query_execution

        def _spy(*args, **kwargs):
            recorded["execution_kind"] = kwargs.get("execution_kind")
            return original(*args, **kwargs)
        monkeypatch.setattr(qes, "log_query_execution", _spy)

        LiveQueryEngine().execute(1, "user-1", "SELECT 1", execution_kind="insight_comparison")
        assert recorded["execution_kind"] == "insight_comparison"

    def test_no_credentials_or_raw_sql_in_result(self, monkeypatch):
        monkeypatch.setattr(
            datasource_service, "get_connection_config",
            lambda sid, uid: _mssql_record(params={"host": "db.internal", "password": "hunter2"}),
        )
        description = [("id",)]
        fake_conn = _FakeConnection(_FakeCursor(description, [(1,)]))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT id FROM secret_table")
        assert "hunter2" not in str(result.to_dict())


class TestPIIMasking:
    """
    Security-hardening regression: LiveQueryEngine.execute() used to hardcode
    the PII-alias set to an empty set() at query_engine.py:196, so confirmed-PII
    columns were returned unmasked regardless of governance state. The fix adds
    an explicit pii_aliases param, sourced by callers (core.orchestrator.agent)
    from the existing data.query_execution_service._governance_recheck() —
    the same mechanism the legacy execute_generated_query() path already used.
    """

    def _fake_ctx_batch_confirmed_pii(self, source_id, user_id, columns):
        return {
            (table_fqn, column_name): {
                "dictionary": {"pii_risk": True, "is_approved": True},
                "profiling": {"pii_name_heuristic": True, "pii_confirmed": True},
            }
            for table_fqn, column_name in columns
        }

    def test_confirmed_pii_column_is_returned_unmasked_for_standard_identity_data(self, monkeypatch):
        """2026-08-24 "remove standard-PII blocking" milestone superseded
        the masking behavior this test used to cover: ordinary business
        identity/contact data (names, emails, ...) is no longer masked for
        any requester — core.governance.pii_access_policy.decide_pii_access
        returns ALLOW, not MASK (MASK is no longer a reachable outcome at
        all). Only a restricted category (ssn/password/credential/token/...)
        still blocks, regardless of confirmed-PII/role state — see
        tests/test_pii_access_policy.py for that coverage."""
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        import data.query_execution_service as qes
        # Day 4, Capability 6 (Task 3) — _governance_recheck now calls the
        # batched get_column_business_contexts_batch (one call for every
        # selected column) instead of get_column_business_context per column.
        monkeypatch.setattr(qes, "get_column_business_contexts_batch", self._fake_ctx_batch_confirmed_pii)

        sql_plan = {"select": [{"table_fqn": "dbo.customers", "column_name": "last_name", "alias": "last_name"}]}
        blocked_cols, pii_aliases, gov_warnings = qes._governance_recheck(1, "user-1", sql_plan)
        # Never masked, never blocked — standard business identity data.
        assert pii_aliases == set()
        assert blocked_cols == []
        assert any(w["type"] == "pii_standard_access" for w in gov_warnings)

        description = [("last_name",)]
        rows = [("Smith",)]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(
            1, "user-1", "SELECT last_name FROM customers", pii_aliases=pii_aliases,
        )

        assert result.status == QueryStatus.SUCCESS
        assert result.columns[0]["pii"] is False, "never masked under the current standard-identity policy"
        assert result.rows[0]["last_name"] == "Smith", "standard business identity data returned unmasked"

    def test_non_pii_column_unaffected_by_masking(self, monkeypatch):
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",), ("name",)]
        rows = [(1, "Acme Corp")]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(
            1, "user-1", "SELECT id, name FROM customers", pii_aliases={"ssn"},
        )

        assert result.columns[0]["pii"] is False
        assert result.columns[1]["pii"] is False
        assert result.rows[0]["id"] == 1
        assert result.rows[0]["name"] == "Acme Corp"

    def test_no_aliases_passed_defaults_to_unmasked(self, monkeypatch):
        # Backward-compatible default: a caller that does not know about PII
        # (or passes nothing) gets the pre-fix behavior, not a hard failure.
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("ssn",)]
        rows = [("123-45-6789",)]
        fake_conn = _FakeConnection(_FakeCursor(description, rows))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        result = LiveQueryEngine().execute(1, "user-1", "SELECT ssn FROM customers")

        assert result.columns[0]["pii"] is False
        assert result.rows[0]["ssn"] == "123-45-6789"


class TestCancel:
    def test_cancel_unknown_execution_returns_false(self):
        assert LiveQueryEngine().cancel("does-not-exist") is False

    def test_cancel_closes_tracked_connection(self, monkeypatch):
        # Drive a slow query in a background thread, then cancel it mid-flight.
        import threading
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        description = [("id",)]
        fake_conn = _FakeConnection(_FakeCursor(description, [(1,)], delay=2.0))
        monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)

        engine = LiveQueryEngine()
        result_holder = {}

        def _run():
            result_holder["result"] = engine.execute(1, "user-1", "SELECT id FROM orders", timeout_s=10)

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.2)  # let execution register the connection

        from core.live import query_engine as qe_module
        with qe_module._RUNNING_LOCK:
            execution_ids = list(qe_module._RUNNING.keys())
        assert len(execution_ids) == 1
        cancelled = engine.cancel(execution_ids[0])
        assert cancelled is True

        t.join(timeout=5)
        assert fake_conn.closed is True
