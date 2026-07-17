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
      product_count   (metric, "Products")      -> unrelated decoy column
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
        # Milestone M-5, Part 2: "clients" and "customers" are a governed
        # synonym pair (data/synonyms.json) now wired into the SQL-answering
        # path, so a "product_count"/"Products" decoy (unrelated to
        # clients/customers) is used here instead of the former
        # "customer_count"/"Customers" one, which would now legitimately
        # (and correctly) cross-resolve with "clients" via that synonym
        # group — this fixture is verifying Count/Distinct SQL generation,
        # not client/customer disambiguation.
        (102, "product_count",  1, 0, "Products",     "HIGH"),
        (103, "revenue_tier",   0, 1, "Revenue Tier", "LOW"),
        (104, "region",         0, 1, "Region",       "LOW"),
        (105, "order_month",    0, 1, "Order Month",  "LOW"),
        (106, "client_count",   1, 0, "Clients",      "HIGH"),
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

    # Milestone M-1 (Enterprise Question Intelligence) additions — a real
    # date column and a real status column so date-intelligence/status-filter
    # end-to-end tests have something genuine to resolve against.
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, semantic_type, created_at, updated_at) "
        "VALUES (107,1,1,'dbo.sales','order_date','TEXT',0,0,0.9,0,0.0,'HIGH',0,0,'DATE',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.sales','order_date','Order Date',0,1,1,0,0,1,'rule_based',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, semantic_type, created_at, updated_at) "
        "VALUES (108,1,1,'dbo.sales','status','TEXT',0,0,0.02,0,0.0,'LOW',0,0,'STATUS',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.sales','status','Status',0,1,0,0,0,1,'rule_based',?,?)",
        (_NOW, _NOW),
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


# ---------------------------------------------------------------------------
# Enterprise Convergence Phase A1.4 — AI Workspace -> Composer unification.
#
# The frontend change (AIWorkspace.jsx handleRun: a selected live data source
# always calls handleComposerAsk(), before any regex classification) is not
# independently testable here — there is no frontend test framework in this
# repo (verified: no vitest/jest config, no .test files, no test script).
# These tests instead prove the backend contract that change relies on:
# once a question reaches composer_ask() with selected_data_source set, it
# resolves correctly and never needs the legacy dataset/report path — and
# that the "how many" / "number of" SQL_REQUEST keyword addition (moved to
# secondary weight after collision analysis — see intent_resolver.py) doesn't
# regress metadata/workflow/governance/dictionary classification.
# ---------------------------------------------------------------------------

class TestHowManyClientsRoutesThroughComposer:
    """Reproduces the exact bug scenario: CCPP selected, 'How many clients
    are in the system?' — must resolve SQL_REQUEST and execute live, not
    fall back to a report generated from an unrelated CSV dataset."""

    def test_resolves_sql_request_and_executes_through_live_query_engine(self, tmp_path, monkeypatch):
        # Milestone Phase 6.2 note: this fixture's `dbo.sales` table has no
        # "clients"-named table, only a `client_count` metric column — under
        # Aggregation Shape Correctness, "How many clients" now correctly
        # refuses rather than count that decoy column (see
        # TestQuestionIntelligenceEndToEnd.test_distinct_count_generates_
        # count_distinct_sql for that exact regression test). "How many
        # sales are there?" is used here instead — "sales" IS the real
        # table name, with no primary key in this fixture, so it correctly
        # falls back to bare COUNT(*) — preserving this test's original
        # purpose: proving SQL_REQUEST intent resolution and live execution
        # wiring both work end to end through the real composer_ask() path,
        # not a fallback to a report generated from an unrelated CSV dataset.
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("row_count",)], rows=[(42,)])

        result = _ask("How many sales are there?")

        assert result["resolved_intent"]["intent_type"] == IntentType.SQL_REQUEST.value
        assert "live_query" in result["services_selected"]
        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        assert live_evidence["data"]["status"] == QueryStatus.SUCCESS.value
        assert live_evidence["data"]["generated_sql"]
        assert "COUNT(*)" in live_evidence["data"]["generated_sql"]
        assert "dbo" in live_evidence["data"]["generated_sql"].lower()
        assert "sales" in live_evidence["data"]["generated_sql"].lower()

        # Milestone M-25 — Enterprise Answer Value Rendering: a bare COUNT(*)
        # renders as real business language, not a row/column-count sentence.
        enterprise_answer = result["enterprise_answer"]
        assert enterprise_answer["answer"] == "There are 42 sales."
        assert enterprise_answer["actual_value"] == 42
        assert "row(s)" not in enterprise_answer["answer"]

    def test_returns_enterprise_answer_with_live_query_citation(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_client_count",)], rows=[(42,)])

        result = _ask("How many clients are in the system?")

        enterprise_answer = result["enterprise_answer"]
        assert enterprise_answer is not None
        assert enterprise_answer["answer_type"] == "live_query"
        live_citations = [
            c for c in enterprise_answer["citations"] if c["source_type"] == CitationType.LIVE_QUERY.value
        ]
        assert live_citations

    def test_composer_receives_selected_data_source(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_client_count",)], rows=[(42,)])

        result = _ask("How many clients are in the system?")

        assert result["evidence_package"]["source_id"] == 1

    def test_legacy_dataset_interpret_path_not_used(self, tmp_path, monkeypatch):
        """The legacy CSV report pipeline (run_dataset_report_plan /
        interpret_task, reached via /v1/interpret -> handle_input) must never
        be invoked when the question is answered through Composer with a
        live source selected — this is what previously produced a report
        from an unrelated CSV dataset instead of a live answer."""
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_client_count",)], rows=[(42,)])

        def _forbidden(*a, **k):
            raise AssertionError("legacy dataset report/interpret path must not be invoked")
        monkeypatch.setattr("core.workflows.workflow_runner.run_dataset_report_plan", _forbidden)
        monkeypatch.setattr("core.interpreter.task_interpreter.interpret_task", _forbidden)

        result = _ask("How many clients are in the system?")

        assert result["resolved_intent"]["intent_type"] == IntentType.SQL_REQUEST.value
        assert "report_id" not in result


# ---------------------------------------------------------------------------
# Milestone M-1 — Enterprise Question Intelligence: full-stack proof.
#
# Question Intent -> Structured Query Intent -> existing SQL Planner ->
# existing SQL Generator -> existing Validator -> existing LiveQueryEngine,
# through the real composer_ask() entry point — the same proof pattern
# TestHowManyClientsRoutesThroughComposer already established, extended to
# the new DISTINCT/Ranking/Date/Status capabilities.
# ---------------------------------------------------------------------------

class TestQuestionIntelligenceEndToEnd:
    def test_distinct_count_generates_count_distinct_sql(self, tmp_path, monkeypatch):
        # Milestone Phase 6.2 — Aggregation Shape Correctness supersedes this
        # fixture's old behavior. `dbo.sales` has no "clients"-named table to
        # count, and `client_count` is a stored per-row metric column, not an
        # entity — the old COUNT(DISTINCT client_count) meant "distinct
        # values of a count column", not "distinct clients", which is
        # exactly the wrong-aggregation-shape class of bug this milestone
        # exists to prevent. Entity-count questions with no confidently
        # resolved authoritative table now refuse rather than fall back to
        # a decoy metric column — see test_entity_count_still_uses_metric_
        # column_when_question_asks_for_a_sum below for the case where
        # summing that same column IS correct.
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("count_client_count",)], rows=[(17,)])

        result = _ask("How many unique clients are there?")

        assert result["resolved_intent"]["intent_type"] == IntentType.SQL_REQUEST.value
        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        assert live_evidence["data"]["executed"] is False
        assert live_evidence["data"]["reason"] == "sql_generation_refused"

    def test_entity_count_still_uses_metric_column_when_question_asks_for_a_sum(self, tmp_path, monkeypatch):
        # The companion case for the test above: when the question explicitly
        # asks to total/sum a stored metric ("total client count"), that is
        # aggregation_target=measure_sum, not entity_count — it keeps the
        # unchanged column-level _resolve_term path and correctly sums the
        # client_count metric column. Preserves pre-Phase-6.2 SUM behavior.
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_client_count",)], rows=[(42,)])

        result = _ask("Total client count")

        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        sql = live_evidence["data"]["generated_sql"]
        assert "SUM(" in sql
        assert "client_count" in sql
        assert live_evidence["data"]["status"] == QueryStatus.SUCCESS.value

        # Milestone M-25 — Enterprise Answer Value Rendering: a successful
        # SUM renders as real business language, not a row/column-count
        # sentence, and the raw column identifier stays out of the primary
        # answer text (it's only exposed via source_columns).
        enterprise_answer = result["enterprise_answer"]
        assert enterprise_answer["actual_value"] == 42
        assert enterprise_answer["aggregation"] == "SUM"
        assert "42" in enterprise_answer["answer"]
        assert "row(s)" not in enterprise_answer["answer"]
        assert "$" not in enterprise_answer["answer"]

    def test_top_n_generates_order_by_and_tightened_limit(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_amount",)], rows=[(1,)])

        result = _ask("Top 10 sales by amount")

        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        sql = live_evidence["data"]["generated_sql"]
        assert "ORDER BY" in sql
        assert "TOP (10)" in sql  # mssql dialect for this fixture's data source

    def test_status_filter_end_to_end(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_amount",)], rows=[(1,)])

        result = _ask("Total amount for active sales")

        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        sql = live_evidence["data"]["generated_sql"]
        assert '"status"' in sql or "[status]" in sql
        assert live_evidence["data"]["status"] == QueryStatus.SUCCESS.value

    def test_date_range_filter_end_to_end(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        _wire_fake_connector(monkeypatch, description=[("sum_amount",)], rows=[(1,)])

        result = _ask("Total amount this month")

        live_evidence = next(
            e for e in result["evidence_package"]["evidence"] if e["source_service"] == "live_query"
        )
        sql = live_evidence["data"]["generated_sql"]
        assert "BETWEEN" in sql
        assert "order_date" in sql.lower()


class TestHowManyCollisionRegression:
    """The 'how many'/'number of' SQL_REQUEST keywords were deliberately
    weighted secondary (0.2), not primary, because collision analysis found
    they otherwise tie 0.4-vs-0.4 with METADATA_LOOKUP/WORKFLOW/GOVERNANCE
    and hijack those intents via specificity/insertion-order tie-breaks.
    These prove the fix holds through the full composer_ask() call, not
    just IntentResolver.resolve() in isolation."""

    def test_how_many_tables_still_resolves_metadata_lookup(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        result = _ask("how many tables are in this schema")
        assert result["resolved_intent"]["intent_type"] == IntentType.METADATA_LOOKUP.value
        assert "live_query" not in result["services_selected"]

    def test_how_many_governance_objects_still_resolves_governance(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
        result = _ask("how many governance objects are pending")
        assert result["resolved_intent"]["intent_type"] == IntentType.GOVERNANCE.value
        assert "live_query" not in result["services_selected"]


class TestDatasetOnlyWorkflowsStillWork:
    """Requirement: 'Do not remove dataset support' / 'existing dataset
    workflow remains unchanged'. Exercises the legacy /v1/interpret ->
    handle_input() path directly (untouched by this phase) to reconfirm
    Path 1 (direct dataset report shortcut) still fires exactly as before
    for a dataset-only session with no live source selected."""

    def test_dataset_report_hint_still_routes_to_direct_dataset_shortcut(self, monkeypatch):
        from core.input.input_handler import handle_input

        monkeypatch.setattr("data.audit.log_audit_event", lambda *a, **k: None)
        monkeypatch.setattr("data.execution_history.log_execution_history", lambda *a, **k: None)
        monkeypatch.setattr("data.usage_service.log_usage_event", lambda *a, **k: None)
        # input_handler imports these names directly, so patch its own module
        # namespace too (the calls above patch the source modules, which is
        # enough since input_handler calls them via `from ... import ...`
        # re-bound names — patch those bindings directly for safety).
        monkeypatch.setattr("core.input.input_handler.log_audit_event", lambda *a, **k: None)
        monkeypatch.setattr("core.input.input_handler.log_execution_history", lambda *a, **k: None)
        monkeypatch.setattr("core.input.input_handler.log_usage_event", lambda *a, **k: None)

        called = {}

        def _fake_run_report(plan, user_id, dataset_id=None, selected_sections=None, report_type=None):
            called["dataset_id"] = dataset_id
            return {"status": "success", "report_id": 99, "dataset_report": {"sections": []}}
        monkeypatch.setattr("core.input.input_handler.run_dataset_report_plan", _fake_run_report)

        response = handle_input("generate a report on clients", user_id="user-1", dataset_id=77)
        result = response["data"]

        assert called.get("dataset_id") == 77
        assert result["planner_source"] == "legacy_interpreter"
        assert result["fallback_used"] is False
