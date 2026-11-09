"""
Tests for data.investigation_service — the Enterprise AI Analyst Agent's
targeted investigation tool (Milestone M-29).

Direct, unit-level tests of inspect_targeted_values() itself (ownership,
PII/approval gating, read-only enforcement, timeout, row cap, invalid
investigation types). Agent-level integration (when the agent invokes it,
the bounded 3-call cap, trace content) is covered in
tests/test_agent_orchestrator.py, which already owns the full plan ->
execute -> validate -> answer environment this tool plugs into.

Run from the project root:
    python -m pytest tests/test_investigation_service.py -v
"""
from __future__ import annotations

import inspect
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-investigation-service-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-investigation-service-salt-long-enough-12")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector

import data.datasource_service as datasource_service
import data.models as models
import data.investigation_service as investigation_service
from data.investigation_service import MAX_INVESTIGATION_ROWS, InvestigationResult, inspect_targeted_values

_NOW = "2026-07-26T00:00:00+00:00"
_USER = "user-1"

_PATCHED_MODULES = (
    "data.business_knowledge_service",
    "data.query_execution_service",
    "data.audit",
    "data.profiling_snapshot_resolver",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _mssql_record(**overrides):
    base = {
        "source_type": "mssql", "source_category": "relational_db",
        "display_name": "Prod SQL Server", "is_active": True, "source_status": "ACTIVE",
        "capabilities": ["connection_test", "schema_discovery", "sql_query"],
        "live_query_enabled": True, "params": {"host": "db.internal", "database": "EXAMPLECO"},
    }
    base.update(overrides)
    return base


class _FakeCursor:
    def __init__(self, description, rows, on_execute=None):
        self.description = description
        self._rows = rows
        self._on_execute = on_execute

    def execute(self, sql, params):
        if self._on_execute:
            self._on_execute(sql, params)

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


def _wire_fake_connector(monkeypatch, *, rows, description, on_execute=None):
    fake_conn = _FakeConnection(_FakeCursor(description, rows, on_execute=on_execute))
    monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
    return fake_conn


@pytest.fixture(autouse=True)
def _bypass_rate_limits_and_audit(monkeypatch):
    import data.query_execution_service as qes
    monkeypatch.setattr(qes, "_write_audit", lambda *a, **k: None)
    monkeypatch.setattr(qes, "log_query_execution", lambda *a, **k: None)


def env(tmp_path, monkeypatch, *, live_query_enabled=True):
    """One mssql source owning dbo.orders with a non-PII TEXT 'status'
    column, a DATETIME 'created_at' column, and an unconfirmed-PII 'email'
    column."""
    db_path = str(tmp_path / "investigation.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, live_query_enabled, created_at, updated_at) "
        "VALUES (1,?,'Prod SQL Server','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?,?)",
        (_USER, int(live_query_enabled), _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',1,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (1,1,1,'dbo.orders','orders','dbo','Transactional','COMPLETE',1000,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.orders','orders','dbo','TABLE','Orders',1,'rule_based',?,?)",
        (_NOW, _NOW),
    )
    for col_id, col_name, data_type, pii_heur, pii_conf in (
        (201, "status", "TEXT", 0, 0),
        (202, "created_at", "DATETIME", 0, 0),
        (203, "email", "TEXT", 1, 0),
    ):
        conn.execute(
            "INSERT INTO profiling_column_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
            " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (?,1,1,'dbo.orders',?,?,0,0,0.02,0,0.0,'LOW',?,?,?,?)",
            (col_id, col_name, data_type, pii_heur, pii_conf, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,'dbo.orders',?,?,0,1,0,0,?,1,'rule_based',?,?)",
            (col_name, col_name.capitalize(), pii_heur, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return db_path


def _authorize(monkeypatch, **overrides):
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record(**overrides))


# ---------------------------------------------------------------------------
# 1. Distinct lookup succeeds.
# ---------------------------------------------------------------------------

def test_distinct_lookup_succeeds(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    _wire_fake_connector(monkeypatch, description=[("status",)], rows=[("Approved",), ("Pending",)])

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "status",
        investigation_type="distinct_values", reason="confirm status values",
    )

    assert isinstance(result, InvestigationResult)
    assert result.valid is True
    assert result.row_count == 2
    assert result.sample_values == ["Approved", "Pending"]
    assert result.table == "dbo.orders"
    assert result.column == "status"


# ---------------------------------------------------------------------------
# 2. Min/max succeeds.
# ---------------------------------------------------------------------------

def test_min_max_succeeds(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    _wire_fake_connector(monkeypatch, description=[("min_value",), ("max_value",)], rows=[("2026-01-01", "2026-07-01")])

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "created_at",
        investigation_type="min_max", reason="confirm date coverage",
    )

    assert result.valid is True
    assert result.sample_values == ["2026-01-01", "2026-07-01"]
    assert "min=" in result.summary and "max=" in result.summary


# ---------------------------------------------------------------------------
# 3. Bounded row limit enforced.
# ---------------------------------------------------------------------------

def test_bounded_row_limit_enforced(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    # The fake connector's fetchmany() honors whatever row_cap is passed in,
    # so wire far more rows than the cap to prove the cap — not the fake —
    # is what limits the result.
    many_rows = [(f"value_{i}",) for i in range(500)]
    _wire_fake_connector(monkeypatch, description=[("status",)], rows=many_rows)

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "status",
        investigation_type="distinct_values", reason="confirm status values",
    )

    assert result.valid is True
    assert result.row_count == MAX_INVESTIGATION_ROWS
    assert len(result.sample_values) == MAX_INVESTIGATION_ROWS


# ---------------------------------------------------------------------------
# 4. PII column blocked (approved-columns-only security rule).
# ---------------------------------------------------------------------------

def test_unconfirmed_pii_column_allowed(tmp_path, monkeypatch):
    """2026-08-24 "remove standard-PII blocking" milestone — an unconfirmed
    ordinary business identity/contact column ("email", a Tier-1 name-
    pattern hit that was never manually/auto-confirmed in this fixture) no
    longer blocks investigation. Only a restricted category still does —
    see test_restricted_category_column_blocked below. Replaces the old
    test_unconfirmed_pii_column_blocked."""
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    _wire_fake_connector(monkeypatch, description=[("email",)], rows=[("jane@example.com",)])

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "email",
        investigation_type="distinct_values", reason="confirm email values",
    )

    assert result.valid is True


def test_restricted_category_column_blocked(tmp_path, monkeypatch):
    """Credential/secret/government-ID/payment columns stay blocked from
    investigation unconditionally, matching every other governed layer's
    policy (core.governance.pii_access_policy)."""
    db_path = env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (204,1,1,'dbo.orders','password_hash','TEXT',0,0,0.99,0,0.0,'HIGH',1,1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()

    def _forbidden_open(self, config):
        raise AssertionError("open_connection must not be called for a restricted-category column")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "password_hash",
        investigation_type="distinct_values", reason="confirm password_hash values",
    )

    assert result.valid is False
    assert "restricted" in result.summary.lower()


# ---------------------------------------------------------------------------
# 5. Source ownership enforced.
# ---------------------------------------------------------------------------

def test_source_ownership_enforced(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)

    result = inspect_targeted_values(
        1, "someone-else", "dbo.orders", "status",
        investigation_type="distinct_values", reason="confirm status values",
    )

    assert result.valid is False
    assert "not owned" in result.summary.lower() or "not found" in result.summary.lower()


# ---------------------------------------------------------------------------
# 6. Read-only enforcement (existing SQL validator is actually consulted).
# ---------------------------------------------------------------------------

def test_read_only_validator_is_consulted(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    opened = {"called": False}

    def _track_open(self, config):
        opened["called"] = True
        raise AssertionError("should not reach open_connection when validator refuses")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _track_open)

    from core.live.query_validator import ValidationResult
    # investigation_service imports validate_sql lazily inside the function
    # (matching this package's own lazy-import convention) — patch it at its
    # real source module so the next call picks up the fake.
    import core.live.query_validator as query_validator_module
    monkeypatch.setattr(
        query_validator_module, "validate",
        lambda sql, dialect: ValidationResult(is_valid=False, blocking_reasons=["forced refusal for test"]),
    )

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "status",
        investigation_type="distinct_values", reason="confirm status values",
    )

    assert result.valid is False
    assert "forced refusal for test" in result.summary
    assert opened["called"] is False


# ---------------------------------------------------------------------------
# 7. Timeout enforcement.
# ---------------------------------------------------------------------------

def test_timeout_enforcement(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)
    _wire_fake_connector(monkeypatch, description=[("status",)], rows=[("Approved",)])

    import data.query_execution_service as qes
    monkeypatch.setattr(qes, "_execute_with_timeout", lambda *a, **k: (None, None, None, True))

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "status",
        investigation_type="distinct_values", reason="confirm status values",
    )

    assert result.valid is False
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# 8. Invalid investigation refused.
# ---------------------------------------------------------------------------

def test_invalid_investigation_type_refused(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "status",
        investigation_type="full_table_dump", reason="not a real investigation type",
    )

    assert result.valid is False
    assert result.reason == "unsupported_investigation_type"


def test_missing_column_for_value_investigation_refused(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", None,
        investigation_type="distinct_values", reason="no column given",
    )

    assert result.valid is False
    assert result.reason == "missing_column"


def test_column_not_found_refused(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "not_a_real_column",
        investigation_type="distinct_values", reason="bogus column",
    )

    assert result.valid is False


def test_column_exists_check_is_metadata_only(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)

    def _forbidden_open(self, config):
        raise AssertionError("column_exists must never open a live connection")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _forbidden_open)

    result = inspect_targeted_values(
        1, _USER, "dbo.orders", "status",
        investigation_type="column_exists", reason="verify column exists",
    )

    assert result.valid is True
    assert result.row_count == 0


# ---------------------------------------------------------------------------
# 13. No unrestricted SQL — structural checks.
# ---------------------------------------------------------------------------

def test_no_unrestricted_sql_in_module_source():
    # Scans _build_sql's own source (the only place SQL text is assembled)
    # for actual write/DDL keywords — not the whole module, since the
    # module's docstrings legitimately name "SELECT *" in prose describing
    # what is forbidden.
    source = inspect.getsource(investigation_service._build_sql)
    for forbidden in ("DROP ", "DELETE ", "INSERT ", "UPDATE ", "TRUNCATE ", "EXEC ", "SELECT *"):
        assert forbidden not in source


def test_built_sql_never_selects_star():
    from data.sql_dialects import get_adapter
    adapter = get_adapter("mssql")
    for kind in ("distinct_values", "min_max", "null_rate", "row_count", "view_definition"):
        sql = investigation_service._build_sql(kind, "dbo.orders", "status", adapter)
        assert "select *" not in sql.lower()  # COUNT(*) is fine; a bare SELECT * is not
        assert ";" not in sql
        assert sql.strip().upper().startswith("SELECT")


# ---------------------------------------------------------------------------
# 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — row_count/view_definition
# investigation types. Table-level (no column_name required/touched),
# dialect-aware view_definition SQL, restricted to the three connected
# relational dialects this codebase actually supports live queries against.
# ---------------------------------------------------------------------------

def test_row_count_and_view_definition_do_not_require_a_column(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _authorize(monkeypatch)

    for kind in ("row_count", "view_definition"):
        result = inspect_targeted_values(
            1, _USER, "dbo.orders", None, investigation_type=kind, reason="table-level check",
        )
        assert result.reason != "missing_column"


def test_row_count_sql_shape():
    from data.sql_dialects import get_adapter
    adapter = get_adapter("mssql")
    sql = investigation_service._build_sql("row_count", "dbo.orders", None, adapter)
    assert "COUNT(*)" in sql
    assert "FROM" in sql


def test_view_definition_sql_per_dialect():
    from data.sql_dialects import get_adapter
    for dialect, expect in (
        ("mssql", "OBJECT_DEFINITION"),
        ("postgresql", "pg_get_viewdef"),
        ("mysql", "INFORMATION_SCHEMA.VIEWS"),
    ):
        adapter = get_adapter(dialect)
        sql = investigation_service._build_sql("view_definition", "dbo.vw_x", None, adapter)
        assert expect in sql


def test_view_definition_sql_supports_sqlite():
    from data.sql_dialects import get_adapter
    adapter = get_adapter("sqlite")
    sql = investigation_service._build_sql("view_definition", "dbo.vw_x", None, adapter)
    assert "sqlite_master" in sql
    assert "vw_x" in sql
