"""
Enterprise Convergence Phase A1.2 — Backend SQL Execution Pipeline tests.

Proves the full chain wired together in
core/orchestrator/context_builder.py::_live_query, entirely out of EXISTING,
already-tested services (no new subsystem):

    business question
      -> data.query_planning_service.plan_business_query   (semantic resolution)
      -> data.sql_planning_service.build_sql_plan           (SQL planning)
      -> data.sql_generation_service.generate_sql           (SQL generation)
      -> core.live.query_validator.validate (inside execute) (SQL validation)
      -> core.live.query_engine.LiveQueryEngine.execute     (execution)
      -> core.answering.answer_planner.AnswerPlanner.build  (EnterpriseAnswer)

Uses the same per-test temp SQLite fixture pattern as
test_phase9_query_planning.py for the metadata side, and the same fake
DBAPI2 connector pattern as test_live_query_engine.py for the live-execution
side.

Run from the project root:
    python -m pytest tests/test_live_query_pipeline.py -v
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phaseA1-2-live-pipeline-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phaseA1-2-salt-long-enough-value-1234567890")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector

import data.datasource_service as datasource_service
import data.models as models
from core.answering.models import CitationType
from core.live.query_result import QueryStatus
from core.orchestrator import EnterpriseOrchestrator, OrchestratorRequest
from core.orchestrator.context_builder import _live_query

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


def env(tmp_path, monkeypatch, *, live_query_enabled=True):
    """Seed an isolated temp SQLite DB with one mssql source owning one table
    (dbo.orders) with a resolvable metric column (amount/"Revenue") and a
    resolvable dimension column (status/"Order Status") — mirrors the
    smallest working scenario from test_phase9_query_planning.py's
    test_simple_measure_and_dimension_same_table."""
    db_path = str(tmp_path / "pipeline.db")
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
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (101,1,1,'dbo.orders','amount','DECIMAL',0,0,0.9,0,0.0,'HIGH',0,0,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.orders','amount','Revenue',1,0,0,0,0,1,'rule_based',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (102,1,1,'dbo.orders','status','TEXT',0,0,0.02,0,0.0,'LOW',0,0,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.orders','status','Order Status',0,1,0,0,0,1,'rule_based',?,?)",
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


@pytest.fixture(autouse=True)
def _bypass_rate_limits_and_audit(monkeypatch):
    import data.query_execution_service as qes
    monkeypatch.setattr(qes, "_check_user_rate_limit", lambda user_id: False)
    monkeypatch.setattr(qes, "_check_daily_limit", lambda user_id: 0)
    monkeypatch.setattr(qes, "_check_source_rate", lambda source_id: 0)
    monkeypatch.setattr(qes, "_check_repeated_query", lambda user_id, sql_hash: 0)
    monkeypatch.setattr(qes, "log_query_execution", lambda *a, **k: None)
    monkeypatch.setattr(qes, "_write_audit", lambda *a, **k: None)


def _wire_fake_connector(monkeypatch, *, rows, description, on_execute=None):
    fake_conn = _FakeConnection(_FakeCursor(description, rows, on_execute=on_execute))
    monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
    return fake_conn


# ---------------------------------------------------------------------------
# 1 & 2. SQL Server question executes successfully; LiveQueryEngine receives
#        the generated SQL.
# ---------------------------------------------------------------------------

def test_sql_server_question_executes_and_engine_receives_generated_sql(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    captured = {}

    def _on_execute(sql, params):
        captured["sql"] = sql
        captured["params"] = params

    _wire_fake_connector(
        monkeypatch,
        description=[("sum_amount",), ("status",)],
        rows=[(1500.0, "Approved"), (900.0, "Pending")],
        on_execute=_on_execute,
    )

    req = OrchestratorRequest(query="query revenue by status", source_id=1, user_id=_USER, params={})
    data = _live_query(req)

    assert data is not None
    assert data["status"] == QueryStatus.SUCCESS.value
    assert data["row_count"] == 2

    generated_sql = data["generated_sql"]
    assert generated_sql  # SQL was actually generated, not a refusal
    assert "amount" in generated_sql.lower()
    assert "status" in generated_sql.lower()
    assert "sum(" in generated_sql.lower()

    # LiveQueryEngine.execute() received exactly the SQL that was generated —
    # not some other string.
    assert captured["sql"] == generated_sql


# ---------------------------------------------------------------------------
# 3. EnterpriseAnswer receives live-query evidence.
# ---------------------------------------------------------------------------

def test_enterprise_answer_receives_live_query_evidence(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(
        monkeypatch,
        description=[("sum_amount",), ("status",)],
        rows=[(1500.0, "Approved")],
    )

    req = OrchestratorRequest(query="query revenue by status", source_id=1, user_id=_USER, params={})
    package = EnterpriseOrchestrator().run_enterprise_answer(req)

    live_item = next((e for e in package.evidence if e.source_service == "live_query"), None)
    assert live_item is not None
    assert live_item.data["status"] == QueryStatus.SUCCESS.value

    answer_item = next(e for e in package.evidence if e.source_service == "enterprise_answer")
    answer = answer_item.data["enterprise_answer"]
    assert answer["answer_type"] == "live_query"
    live_citations = [c for c in answer["citations"] if c["source_type"] == CitationType.LIVE_QUERY.value]
    assert live_citations, "expected at least one live_query citation on the EnterpriseAnswer"


# ---------------------------------------------------------------------------
# 4. live_query_enabled=false blocks execution.
# ---------------------------------------------------------------------------

def test_live_query_enabled_false_blocks_execution(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, live_query_enabled=False)
    monkeypatch.setattr(
        datasource_service, "get_connection_config",
        lambda sid, uid: _mssql_record(live_query_enabled=False),
    )
    opened = {"called": False}

    def _track_open(self, config):
        opened["called"] = True
        raise AssertionError("open_connection must not be called when live_query_enabled is False")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _track_open)

    req = OrchestratorRequest(query="query revenue by status", source_id=1, user_id=_USER, params={})
    data = _live_query(req)

    assert data is not None
    assert data["status"] == QueryStatus.BLOCKED.value
    assert "not enabled" in data["error"].lower()
    assert opened["called"] is False


# ---------------------------------------------------------------------------
# 5. Dangerous SQL remains blocked (raw-SQL trusted-caller branch, unaffected
#    by the new business-question branch added in this phase).
# ---------------------------------------------------------------------------

def test_dangerous_sql_still_blocked_via_raw_sql_branch(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    opened = {"called": False}

    def _track_open(self, config):
        opened["called"] = True
        raise AssertionError("open_connection must not be called for dangerous SQL")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _track_open)

    req = OrchestratorRequest(
        query="run this SQL", source_id=1, user_id=_USER,
        params={"sql": "DROP TABLE dbo.orders"},
    )
    data = _live_query(req)

    assert data["status"] == QueryStatus.BLOCKED.value
    assert opened["called"] is False


# ---------------------------------------------------------------------------
# 6. Metadata-only questions never execute SQL.
# ---------------------------------------------------------------------------

def test_metadata_only_question_never_executes_sql(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_execute(self, *a, **k):
        raise AssertionError("LiveQueryEngine.execute must not be called for a metadata-only question")
    import core.live.query_engine as query_engine_module
    monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

    req = OrchestratorRequest(
        query="what tables exist in this source", source_id=1, user_id=_USER, params={},
    )
    package = EnterpriseOrchestrator().process(req)

    service_ids = [c.service_id for c in package.service_calls]
    assert "live_query" not in service_ids


# ---------------------------------------------------------------------------
# Extra — unresolvable business question refuses generation without ever
# attempting execution (defense-in-depth for the new branch itself).
# ---------------------------------------------------------------------------

def test_unresolvable_question_refuses_without_executing(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_execute(self, *a, **k):
        raise AssertionError("LiveQueryEngine.execute must not be called when SQL generation is refused")
    import core.live.query_engine as query_engine_module
    monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

    req = OrchestratorRequest(query="query profit by region", source_id=1, user_id=_USER, params={})
    data = _live_query(req)

    assert data is not None
    assert data.get("executed") is False
    assert data.get("reason") == "sql_generation_refused"


# ---------------------------------------------------------------------------
# 7-11. Enterprise Implementation — CCPP Safe Enrollment Question Support.
#
# "Which classes/courses have the highest enrollment?" is intercepted
# before plan_business_query ever runs — neither branch depends on the
# generic semantic pipeline or on the source having any tables profiled at
# all, since detection is purely a question-text pattern match
# (core.semantic.concept_resolver.derive_analytics_intent) and the "classes"
# SQL is a fixed, verified business model, not resolved from candidates.
# ---------------------------------------------------------------------------

def test_classes_enrollment_question_generates_correct_sql_and_executes(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    captured = {}

    def _on_execute(sql, params):
        captured["sql"] = sql
        captured["params"] = params

    _wire_fake_connector(
        monkeypatch,
        description=[("ClassID",), ("ClassName",), ("enrolled_student_count",)],
        rows=[(101, "Intro to Python", 42)],
        on_execute=_on_execute,
    )

    req = OrchestratorRequest(
        query="Which classes have the highest enrollment?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert data is not None
    assert data["status"] == QueryStatus.SUCCESS.value

    sql = data["generated_sql"]
    assert "ADF_ClassSignups" in sql
    assert "ADF_Class" in sql
    assert "COUNT(DISTINCT" in sql and "StudentID" in sql
    assert "CancelID" in sql and "IS NULL" in sql
    assert "GROUP BY" in sql and "ClassID" in sql and "ClassName" in sql
    assert "ORDER BY" in sql and "enrolled_student_count" in sql and "DESC" in sql
    assert "TOP (10)" in sql
    # Never touches the wrong tables.
    assert "ADF_Enrollment_Tracking" not in sql
    assert "ADF_Course" not in sql
    assert "ADF_Path" not in sql

    assert captured["sql"] == sql


def test_courses_enrollment_question_returns_clarification_without_executing(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_execute(self, *a, **k):
        raise AssertionError("LiveQueryEngine.execute must not be called for the courses phrasing")
    import core.live.query_engine as query_engine_module
    monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

    def _forbidden_plan(*a, **k):
        raise AssertionError("plan_business_query must not run for the courses phrasing — "
                              "it would silently build the flawed Path-based query")
    import data.query_planning_service as qps
    monkeypatch.setattr(qps, "plan_business_query", _forbidden_plan)

    req = OrchestratorRequest(
        query="Which courses have the highest enrollment?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert data is not None
    assert data.get("executed") is False
    assert data.get("reason") == "ambiguous_enrollment_entity"
    assert any("Did you mean: Which classes have the highest enrollment?" in e for e in data["explanation"])


def test_classes_enrollment_question_still_blocked_when_live_query_disabled(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, live_query_enabled=False)
    monkeypatch.setattr(
        datasource_service, "get_connection_config",
        lambda sid, uid: _mssql_record(live_query_enabled=False),
    )
    opened = {"called": False}

    def _track_open(self, config):
        opened["called"] = True
        raise AssertionError("open_connection must not be called when live_query_enabled is False")
    monkeypatch.setattr(SQLServerConnector, "open_connection", _track_open)

    req = OrchestratorRequest(
        query="Which classes have the highest enrollment?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert data is not None
    assert data["status"] == QueryStatus.BLOCKED.value
    assert "not enabled" in data["error"].lower()
    assert opened["called"] is False


def test_unrelated_ranking_question_unaffected_by_enrollment_guard(tmp_path, monkeypatch):
    # "revenue" ranking, not "enrollment" — must fall through to the normal
    # pipeline exactly as before, proving the new guard is narrowly scoped.
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(
        monkeypatch,
        description=[("sum_amount",), ("status",)],
        rows=[(1500.0, "Approved")],
    )

    req = OrchestratorRequest(
        query="Which status has the highest revenue?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert data is not None
    assert "generated_sql" in data
    assert "ADF_ClassSignups" not in data["generated_sql"]


# ---------------------------------------------------------------------------
# 12-14. Safety correction — the CCPP enrollment override (both the "classes"
# hardcoded SQL and the "courses" clarification) must only apply to the
# verified CCPP source (params["database"] == "CCPP"), never to another
# tenant's source purely from question text, and must fail closed (fall
# through to the generic pipeline) when datasource identity can't be
# confirmed.
# ---------------------------------------------------------------------------

def _spy_plan_business_query(monkeypatch):
    import data.query_planning_service as qps
    original_plan = qps.plan_business_query
    calls = {"count": 0}

    def _spy(*a, **k):
        calls["count"] += 1
        return original_plan(*a, **k)
    monkeypatch.setattr(qps, "plan_business_query", _spy)
    return calls


def test_non_ccpp_source_classes_question_falls_through_to_generic_pipeline(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        datasource_service, "get_connection_config",
        lambda sid, uid: _mssql_record(params={"host": "db.internal", "database": "OtherCustomerDB"}),
    )
    calls = _spy_plan_business_query(monkeypatch)

    req = OrchestratorRequest(
        query="Which classes have the highest enrollment?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert calls["count"] == 1  # fell through to the generic pipeline, not the CCPP override
    assert data is not None
    assert data.get("reason") != "ambiguous_enrollment_entity"
    generated_sql = data.get("generated_sql", "")
    assert "ADF_ClassSignups" not in generated_sql
    assert "ADF_Class" not in generated_sql


def test_non_ccpp_source_courses_question_falls_through_to_generic_pipeline(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        datasource_service, "get_connection_config",
        lambda sid, uid: _mssql_record(params={"host": "db.internal", "database": "OtherCustomerDB"}),
    )
    calls = _spy_plan_business_query(monkeypatch)

    req = OrchestratorRequest(
        query="Which courses have the highest enrollment?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert calls["count"] == 1  # fell through to the generic pipeline, not the CCPP clarification
    assert data is not None
    assert data.get("reason") != "ambiguous_enrollment_entity"
    assert not any(
        "Did you mean: Which classes have the highest enrollment?" in e
        for e in (data.get("explanation") or [])
    )


def test_datasource_lookup_failure_fails_closed_and_falls_through(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)

    def _broken_lookup(sid, uid):
        raise RuntimeError("simulated datasource lookup failure")
    monkeypatch.setattr(datasource_service, "get_connection_config", _broken_lookup)
    calls = _spy_plan_business_query(monkeypatch)

    req = OrchestratorRequest(
        query="Which classes have the highest enrollment?", source_id=1, user_id=_USER, params={},
    )
    data = _live_query(req)

    assert calls["count"] == 1  # failed closed: no CCPP override, generic pipeline still ran
    assert data is not None
    assert data.get("reason") != "ambiguous_enrollment_entity"
    generated_sql = data.get("generated_sql", "")
    assert "ADF_ClassSignups" not in generated_sql
    assert "ADF_Class" not in generated_sql


def test_class_enrollment_ranking_entity_detection():
    from core.orchestrator.context_builder import _class_enrollment_ranking_entity

    assert _class_enrollment_ranking_entity("Which classes have the highest enrollment?") == "class"
    assert _class_enrollment_ranking_entity("Which class has the most enrollment?") == "class"
    assert _class_enrollment_ranking_entity("Which courses have the highest enrollment?") == "course"
    assert _class_enrollment_ranking_entity("Which course has the most enrollment?") == "course"
    assert _class_enrollment_ranking_entity("Which clients have the highest revenue?") is None
    assert _class_enrollment_ranking_entity("how many students are there") is None
