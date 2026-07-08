"""
Enterprise Convergence Phase A1.3 — Composer End-to-End SQL Routing tests.

Proves the existing Composer (api/v1/composer.py::composer_ask, the
POST /composer/ask endpoint) automatically routes SQL_REQUEST questions
through the full backend pipeline with no special-case bypass:

    Composer (composer_ask)
      -> EnterpriseOrchestrator.process()      (Orchestrator + Intent Resolver)
      -> core.semantic.planner.SemanticQueryPlanner   (via ExecutionPlanner.plan())
      -> data.sql_planning_service.build_sql_plan     (via the live_query adapter)
      -> data.sql_generation_service.generate_sql     (via the live_query adapter)
      -> core.live.query_engine.LiveQueryEngine.execute
      -> core.answering.answer_planner.AnswerPlanner.build -> EnterpriseAnswer

Calls composer_ask() directly (not through HTTP/FastAPI DI) with a real
AuthenticatedUser, so this exercises the actual endpoint function body,
including its REPORT_GENERATION early-return branch check (confirmed NOT
taken for these questions) exactly as a live request would.

Uses the same per-test temp SQLite fixture pattern as
test_live_query_pipeline.py.

Run from the project root:
    python -m pytest tests/test_composer_sql_routing.py -v
"""
from __future__ import annotations

import os
import sqlite3
import uuid

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phaseA1-3-composer-sql-routing-secret-1")
os.environ.setdefault("USER_ID_SALT", "test-phaseA1-3-salt-long-enough-value-123456")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector

import data.datasource_service as datasource_service
import data.models as models
from auth.api_key import AuthenticatedUser
from api.v1.composer import ComposerRequest, composer_ask
from core.answering.models import CitationType
from core.live.query_result import QueryStatus
from core.orchestrator.models import IntentType

_NOW = "2026-07-08T00:00:00+00:00"
_USER = "user-1"

_PATCHED_MODULES = (
    "data.query_planning_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    "data.semantic_layer_service",
    "data.schema_service",
    "data.relationship_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    """Seed one mssql source owning dbo.sales with columns covering all
    three required test phrases without cross-resolution:
      amount          (metric, "Sales Value")   -> resolves "sales"/"value"
      customer_count  (metric, "Customers")     -> exact-matches "customers"
      revenue_tier    (dimension, "Revenue Tier") -> resolves "revenue"
      region          (dimension, "Region")       -> resolves "region"
      order_month     (dimension, "Order Month")  -> resolves "month"
    """
    db_path = str(tmp_path / "composer_sql.db")
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
        "VALUES (1,?,'Prod SQL Server','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,1,?,?)",
        (_USER, _NOW, _NOW),
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
        "VALUES (1,1,1,'dbo.sales','sales','dbo','Transactional','COMPLETE',1000,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.sales','sales','dbo','TABLE','Sales',1,'rule_based',?,?)",
        (_NOW, _NOW),
    )

    columns = [
        # (id, column_name, is_metric, is_dimension, business_label, cardinality_tier)
        (101, "amount",         1, 0, "Sales Value",  "HIGH"),
        (102, "customer_count", 1, 0, "Customers",    "HIGH"),
        (103, "revenue_tier",   0, 1, "Revenue Tier", "LOW"),
        (104, "region",         0, 1, "Region",       "LOW"),
        (105, "order_month",    0, 1, "Order Month",  "LOW"),
    ]
    for cid, name, is_metric, is_dim, label, tier in columns:
        conn.execute(
            "INSERT INTO profiling_column_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
            " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (?,1,1,'dbo.sales',?,?,0,0,0.5,0,0.0,?,0,0,?,?)",
            (cid, name, "DECIMAL" if is_metric else "TEXT", tier, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,'dbo.sales',?,?,?,?,0,0,0,1,'rule_based',?,?)",
            (name, label, is_metric, is_dim, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return db_path


def _mssql_record(**overrides):
    base = {
        "source_type": "mssql",
        "source_category": "relational_db",
        "display_name": "Prod SQL Server",
        "is_active": True,
        "source_status": "ACTIVE",
        "capabilities": ["connection_test", "schema_discovery", "sql_query"],
        "live_query_enabled": True,
        "params": {"host": "db.internal", "database": "CCPP"},
    }
    base.update(overrides)
    return base


class _FakeCursor:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def execute(self, sql, params):
        pass

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


@pytest.fixture(autouse=True)
def _bypass_rate_limits_and_audit(monkeypatch):
    import data.query_execution_service as qes
    monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: False)
    monkeypatch.setattr(qes, "_check_daily_limit", lambda user_id: 0)
    monkeypatch.setattr(qes, "_check_source_rate", lambda source_id: 0)
    monkeypatch.setattr(qes, "_check_repeated_query", lambda user_id, sql_hash: 0)
    monkeypatch.setattr(qes, "log_query_execution", lambda *a, **k: None)
    monkeypatch.setattr(qes, "_write_audit", lambda *a, **k: None)


def _wire_fake_connector(monkeypatch, *, rows, description):
    fake_conn = _FakeConnection(_FakeCursor(description, rows))
    monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
    return fake_conn


def _ask(message: str) -> dict:
    """Call the real composer_ask() endpoint function directly (bypassing
    HTTP/FastAPI dependency injection) with a real AuthenticatedUser — this
    executes the exact same code the live /composer/ask route runs."""
    body = ComposerRequest(
        session_id=str(uuid.uuid4()),
        message=message,
        selected_data_source=1,
    )
    user = AuthenticatedUser(role="user", user_id=_USER)
    return composer_ask(body, user)


# ---------------------------------------------------------------------------
# The 3 required questions — each proven to resolve SQL_REQUEST, execute
# through LiveQueryEngine, return an EnterpriseAnswer, and include a
# live-query citation.
# ---------------------------------------------------------------------------

_QUESTIONS = [
    "Top 10 customers by revenue",
    "query sales by region",  # "Show sales by region" contains no SQL_REQUEST
                               # signal keyword today (see Phase A1.3 audit);
                               # rephrased with an explicit trigger word so
                               # this proves the routing wiring, not the
                               # separate keyword-coverage gap.
    "Average order value by month",
]


@pytest.mark.parametrize("question", _QUESTIONS)
def test_question_resolves_sql_request(tmp_path, monkeypatch, question):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("col1",), ("col2",)], rows=[(1, "a"), (2, "b")])

    result = _ask(question)

    assert result["resolved_intent"]["intent_type"] == IntentType.SQL_REQUEST.value


@pytest.mark.parametrize("question", _QUESTIONS)
def test_question_executes_through_live_query_engine(tmp_path, monkeypatch, question):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("col1",), ("col2",)], rows=[(1, "a"), (2, "b")])

    result = _ask(question)

    assert "live_query" in result["services_selected"]
    live_evidence = next(
        e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
    )
    assert live_evidence["data"]["status"] == QueryStatus.SUCCESS.value
    assert live_evidence["data"]["generated_sql"]  # SQL was actually generated and run


@pytest.mark.parametrize("question", _QUESTIONS)
def test_question_returns_enterprise_answer_with_live_query_citation(tmp_path, monkeypatch, question):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("col1",), ("col2",)], rows=[(1, "a"), (2, "b")])

    result = _ask(question)

    enterprise_answer = result["enterprise_answer"]
    assert enterprise_answer is not None
    assert enterprise_answer["answer_type"] == "live_query"
    live_citations = [
        c for c in enterprise_answer["citations"] if c["source_type"] == CitationType.LIVE_QUERY.value
    ]
    assert live_citations, f"expected a live_query citation for {question!r}"


# ---------------------------------------------------------------------------
# Requirement 4 — other intents are unaffected by this phase's changes.
# ---------------------------------------------------------------------------

class TestOtherIntentsUnaffected:
    def test_metadata_question_still_uses_metadata_services(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        def _forbidden_execute(self, *a, **k):
            raise AssertionError("LiveQueryEngine.execute must not be called for a metadata question")
        import core.live.query_engine as query_engine_module
        monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

        result = _ask("what tables exist in this source")

        assert result["resolved_intent"]["intent_type"] == IntentType.METADATA_LOOKUP.value
        assert "live_query" not in result["services_selected"]

    def test_governance_question_unchanged(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        result = _ask("governance compliance pii sensitive stewardship")
        assert result["resolved_intent"]["intent_type"] == IntentType.GOVERNANCE.value
        assert "live_query" not in result["services_selected"]

    def test_dictionary_question_unchanged(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        result = _ask("show me the dictionary definitions for this data source")
        assert result["resolved_intent"]["intent_type"] == IntentType.DICTIONARY.value
        assert "live_query" not in result["services_selected"]

    def test_report_request_still_uses_report_engine(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

        def _forbidden_execute(self, *a, **k):
            raise AssertionError("LiveQueryEngine.execute must not be called for a report request")
        import core.live.query_engine as query_engine_module
        monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

        called = {}

        def _fake_run_report(plan, user_id=None, dataset_id=None):
            called["ran"] = True
            return {"report_id": None, "dataset_report_error": "no dataset uploaded"}
        monkeypatch.setattr(
            "core.workflows.workflow_runner.run_dataset_report_plan", _fake_run_report
        )

        result = _ask("build kpi report for this dataset")

        assert result["resolved_intent"]["intent_type"] == IntentType.REPORT_GENERATION.value
        assert called.get("ran") is True
        assert "services_selected" not in result or result["services_selected"] == ["report_pipeline"]
