"""
Tests for Milestone M-31 — Typed Conversation Context and Clarification
Resume: core.orchestrator.agent.ConversationContext, its isolation/expiry
gate (_resolve_conversation_context), bounded follow-up recognition/merge
(_classify_follow_up/_build_follow_up_plan), clarification resume/cancel
wired into answer_business_question(), and api/v1/composer.py's routing of
all of the above through the shared agent.

Uses the same per-test temp SQLite + fake DBAPI2 connector fixture pattern
as tests/test_agent_orchestrator.py and tests/test_clarification_intelligence.py
— no new test-environment convention is introduced.

Run from the project root:
    python -m pytest tests/test_conversation_context.py -v
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-conversation-context-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-conversation-context-salt-long-enough12")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector

import core.orchestrator.agent as agent_module
import core.orchestrator.context_builder as context_builder_module
import data.datasource_service as datasource_service
import data.models as models
from auth.api_key import AuthenticatedUser
from api.v1.agent_response_adapters import build_conversation_state
from api.v1.composer import ComposerRequest, _build_conversation_context, composer_ask
from core.orchestrator.agent import (
    AgentStatus,
    ConversationContext,
    _resolve_conversation_context,
    answer_business_question,
)

_NOW = "2026-07-26T00:00:00+00:00"
_USER = "user-1"
_OTHER_USER = "user-2"

_PATCHED_MODULES = (
    "data.query_planning_service", "data.knowledge_graph_service",
    "data.business_knowledge_service", "data.semantic_layer_service",
    "data.schema_service", "data.relationship_service", "data.metadata_preparation_service",
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
        "live_query_enabled": True, "params": {"host": "db.internal", "database": "CCPP"},
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


def _wire_fake_connector(monkeypatch, *, rows, description):
    fake_conn = _FakeConnection(_FakeCursor(description, rows))
    monkeypatch.setattr(SQLServerConnector, "open_connection", lambda self, config: fake_conn)
    return fake_conn


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
# Environment 1 — dbo.orders(amount, status, region, order_date): metric +
# two dimensions + a real date column, so follow-up merges (time range,
# filter-add, limit change, dimension breakdown) all have something genuine
# to resolve against. Every generated SQL shape below was verified directly
# against answer_business_question for this exact fixture.
# ---------------------------------------------------------------------------

def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "conversation_context.db")
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
    columns = [
        (101, "amount", "DECIMAL", 1, 0, "Revenue", "HIGH"),
        (102, "status", "TEXT", 0, 1, "Order Status", "LOW"),
        (103, "region", "TEXT", 0, 1, "Region", "LOW"),
    ]
    for cid, name, dtype, is_metric, is_dim, label, tier in columns:
        conn.execute(
            "INSERT INTO profiling_column_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
            " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (?,1,1,'dbo.orders',?,?,0,0,0.5,0,0.0,?,0,0,?,?)",
            (cid, name, dtype, tier, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,'dbo.orders',?,?,?,?,0,0,0,1,'rule_based',?,?)",
            (name, label, is_metric, is_dim, _NOW, _NOW),
        )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, semantic_type, created_at, updated_at) "
        "VALUES (104,1,1,'dbo.orders','order_date','TEXT',0,0,0.9,0,0.0,'HIGH',0,0,'DATE',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1,1,'dbo.orders','order_date','Order Date',0,1,1,0,0,1,'rule_based',?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _wire_success(monkeypatch, *, description=None, rows=None):
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    return _wire_fake_connector(
        monkeypatch,
        description=description or [("sum_amount",), ("status",)],
        rows=rows or [(1500.0, "Approved")],
    )


def _first_turn_context(tmp_path, monkeypatch, *, conversation_id="conv-1") -> ConversationContext:
    """Answers "query revenue by status" and returns the round-tripped
    ConversationContext a real second turn would receive — reuses
    build_conversation_state, never a hand-built shortcut, so every
    follow-up test below exercises the real production contract."""
    env(tmp_path, monkeypatch)
    _wire_success(monkeypatch)
    state = answer_business_question(1, _USER, "query revenue by status")
    assert state.status == AgentStatus.ANSWERED
    data = build_conversation_state(
        state, conversation_id=conversation_id, source_id=1, user_id=_USER, turn_number=1,
    )
    assert data is not None
    return ConversationContext.from_dict(data)


# ---------------------------------------------------------------------------
# Environment 2 — two tied "clients" tables (reused pattern from
# tests/test_clarification_intelligence.py::env) for clarification resume/
# cancel tests.
# ---------------------------------------------------------------------------

def tied_clients_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "conversation_context_clarification.db")
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
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    for i, (table_fqn, table_name, business_name) in enumerate((
        ("dbo.active_clients", "active_clients", "Active Clients"),
        ("dbo.legacy_clients", "legacy_clients", "Historical Clients"),
    ), start=1):
        conn.execute(
            "INSERT INTO profiling_table_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
            " table_class, profiling_status, exact_row_count, created_at, updated_at) "
            "VALUES (?,1,1,?,?,'dbo','Transactional','COMPLETE',500,?,?)",
            (i, table_fqn, table_name, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            " business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,'dbo','TABLE',?,0,'rule_based',?,?)",
            (table_fqn, table_name, business_name, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO profiling_column_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
            " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (?,1,1,?,'id','INTEGER',1,1,1.0,0,0.0,'HIGH',0,0,?,?)",
            (100 + i, table_fqn, _NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,?,'id','Client ID',0,0,0,1,0,0,'rule_based',?,?)",
            (table_fqn, _NOW, _NOW),
        )
    conn.commit()
    conn.close()
    return db_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1/2. Clarification selection resumes the agent, reusing the existing
#      _apply_clarification_overrides implementation (spied, never a second
#      implementation).
# ---------------------------------------------------------------------------

def test_clarification_selection_resumes_agent_via_existing_override(tmp_path, monkeypatch):
    tied_clients_env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("count_id",)], rows=[(42,)])

    calls = {"count": 0}
    original = context_builder_module._apply_clarification_overrides

    def _spy(*a, **k):
        calls["count"] += 1
        return original(*a, **k)
    monkeypatch.setattr(context_builder_module, "_apply_clarification_overrides", _spy)

    cc = ConversationContext(
        conversation_id="conv-1", source_id=1, user_id=_USER, created_at=_now_iso(),
        clarification_selection=({"term": "clients", "table_fqn": "dbo.active_clients"},),
    )
    state = answer_business_question(1, _USER, "how many clients", conversation_context=cc)

    assert state.status == AgentStatus.ANSWERED
    assert state.clarification_resumed is True
    assert calls["count"] == 1
    assert "active_clients" in state.generated_sql
    assert "legacy_clients" not in state.generated_sql
    resume_steps = [s for s in state.trace if s.tool == "apply_clarification_selection"]
    assert len(resume_steps) == 1
    # Accuracy Program A4, Fix #5 — governed execution (M-27's structural
    # result validation) is not skipped on the clarification-resume path;
    # same assertion pattern as test_agent_orchestrator.py's
    # test_result_validator_is_called for a plain (non-clarification) request.
    assert state.execution_result is not None
    assert state.result_validation is not None


# ---------------------------------------------------------------------------
# 3. Clarification cancellation does not execute SQL.
# ---------------------------------------------------------------------------

def test_cancel_clarification_does_not_execute_sql(tmp_path, monkeypatch):
    tied_clients_env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden_execute(*a, **k):
        raise AssertionError("LiveQueryEngine.execute must not be called on a cancelled clarification")
    import core.live.query_engine as query_engine_module
    monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

    cc = ConversationContext(
        conversation_id="conv-1", source_id=1, user_id=_USER, created_at=_now_iso(),
        cancel_clarification=True,
    )
    state = answer_business_question(1, _USER, "how many clients", conversation_context=cc)

    assert state.status == AgentStatus.SAFELY_REFUSED
    assert state.stop_code == "clarification_cancelled"
    assert state.query_plan is None  # never even reached planning
    assert state.answer_evidence_data["executed"] is False
    cancel_steps = [s for s in state.trace if s.tool == "cancel_clarification"]
    assert len(cancel_steps) == 1


# ---------------------------------------------------------------------------
# 4. Clarification resume occurs at most once — the next turn's
#    round-tripped context never carries a resumed selection forward.
# ---------------------------------------------------------------------------

def test_clarification_resume_bounded_to_one_cycle(tmp_path, monkeypatch):
    tied_clients_env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("count_id",)], rows=[(42,)])

    cc = ConversationContext(
        conversation_id="conv-1", source_id=1, user_id=_USER, created_at=_now_iso(),
        clarification_selection=({"term": "clients", "table_fqn": "dbo.active_clients"},),
    )
    state = answer_business_question(1, _USER, "how many clients", conversation_context=cc)
    assert state.status == AgentStatus.ANSWERED

    next_context = build_conversation_state(
        state, conversation_id="conv-1", source_id=1, user_id=_USER, turn_number=1,
    )
    assert next_context["clarification_selection"] is None
    assert next_context["cancel_clarification"] is False


# ---------------------------------------------------------------------------
# 5/6/7. Follow-up merges — time range replaces, filter/dimensions/metric
#        preserved; filter-add preserves the prior plan and adds one filter;
#        limit change replaces the prior limit.
# ---------------------------------------------------------------------------

def test_follow_up_time_range_reuses_prior_metric_and_dimensions(tmp_path, monkeypatch):
    cc = _first_turn_context(tmp_path, monkeypatch)
    _wire_success(monkeypatch, description=[("sum_amount",), ("status",)], rows=[(900.0, "Approved")])

    state = answer_business_question(1, _USER, "What about last quarter?", conversation_context=cc)

    assert state.status == AgentStatus.ANSWERED
    assert state.follow_up_type == "time_range_change"
    assert state.conversation_context_used is True
    assert "SUM([dbo].[orders].[amount])" in state.generated_sql
    assert "GROUP BY [dbo].[orders].[status]" in state.generated_sql
    assert "order_date] BETWEEN" in state.generated_sql


def test_follow_up_filter_reuses_prior_plan(tmp_path, monkeypatch):
    cc = _first_turn_context(tmp_path, monkeypatch)
    _wire_success(monkeypatch, description=[("sum_amount",), ("status",)], rows=[(900.0, "Approved")])

    state = answer_business_question(1, _USER, "Only Approved.", conversation_context=cc)

    assert state.status == AgentStatus.ANSWERED
    assert state.follow_up_type == "filter_add"
    assert state.conversation_context_used is True
    assert "SUM([dbo].[orders].[amount])" in state.generated_sql
    assert "GROUP BY [dbo].[orders].[status]" in state.generated_sql
    assert "[dbo].[orders].[status] = ?" in state.generated_sql
    assert state.sql_generation["parameters"]["values"] == ["Approved"]


def test_follow_up_limit_replaces_prior_limit(tmp_path, monkeypatch):
    cc = _first_turn_context(tmp_path, monkeypatch)
    _wire_success(monkeypatch, description=[("sum_amount",), ("status",)], rows=[(900.0, "Approved")])

    state = answer_business_question(1, _USER, "Top 5 instead.", conversation_context=cc)

    assert state.status == AgentStatus.ANSWERED
    assert state.follow_up_type == "limit_change"
    assert state.conversation_context_used is True
    assert "TOP (5)" in state.generated_sql
    assert "TOP (1000)" not in state.generated_sql


# ---------------------------------------------------------------------------
# 8. A complete new question does not reuse old context.
# ---------------------------------------------------------------------------

def test_complete_new_question_does_not_reuse_context(tmp_path, monkeypatch):
    cc = _first_turn_context(tmp_path, monkeypatch)
    _wire_success(monkeypatch, description=[("sum_amount",), ("region",)], rows=[(900.0, "West")])

    state = answer_business_question(1, _USER, "query revenue by region", conversation_context=cc)

    assert state.conversation_context_used is False
    assert state.follow_up_type is None
    assert "region" in state.generated_sql.lower()


# ---------------------------------------------------------------------------
# 9/10. Context does not cross users or sources (agent-level isolation gate).
# ---------------------------------------------------------------------------

def test_context_does_not_cross_users():
    candidate = ConversationContext(
        conversation_id="conv-1", source_id=1, user_id=_OTHER_USER, created_at=_now_iso(),
    )
    assert _resolve_conversation_context(candidate, source_id=1, user_id=_USER) is None


def test_context_does_not_cross_sources():
    candidate = ConversationContext(
        conversation_id="conv-1", source_id=999, user_id=_USER, created_at=_now_iso(),
    )
    assert _resolve_conversation_context(candidate, source_id=1, user_id=_USER) is None


def test_agent_falls_back_safely_when_context_crosses_users(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _wire_success(monkeypatch)
    foreign_cc = ConversationContext(
        conversation_id="conv-1", source_id=1, user_id=_OTHER_USER, created_at=_now_iso(),
        previous_effective_question="query revenue by status",
        previous_query_plan={"dimensions": [{"selected": {"table_fqn": "dbo.orders", "column_name": "status"}}]},
    )
    state = answer_business_question(1, _USER, "What about last quarter?", conversation_context=foreign_cc)
    assert state.conversation_context_used is False


# ---------------------------------------------------------------------------
# 11. Context does not cross conversation IDs (composer-level check — the
#     one dimension with no independent trusted reference at the agent
#     layer, see api/v1/composer.py::_build_conversation_context).
# ---------------------------------------------------------------------------

def test_build_conversation_context_discards_mismatched_conversation_id():
    prior_state = {
        "conversation_id": "conv-OLD", "source_id": 1, "user_id": _USER,
        "turn_number": 1, "created_at": _now_iso(),
        "previous_question": "query revenue by status",
        "previous_effective_question": "query revenue by status",
        "previous_query_plan": {"dimensions": []}, "previous_business_plan": {},
        "selected_tables": ["dbo.orders"], "metric": "Revenue", "dimensions": ["Order Status"],
        "filters": [], "time_range": None,
        "clarification_selection": None, "cancel_clarification": False,
    }
    body = ComposerRequest(
        session_id="conv-NEW", message="What about last quarter?",
        selected_data_source=1, conversation_state=prior_state,
    )
    context = _build_conversation_context(body, _USER)

    assert context is not None  # a minimal context is still built for this turn...
    assert context.previous_query_plan is None  # ...but the mismatched prior turn is discarded
    assert context.previous_effective_question is None


def test_build_conversation_context_accepts_matching_conversation_id():
    prior_state = {
        "conversation_id": "conv-1", "source_id": 1, "user_id": _USER,
        "turn_number": 1, "created_at": _now_iso(),
        "previous_question": "query revenue by status",
        "previous_effective_question": "query revenue by status",
        "previous_query_plan": {"dimensions": []}, "previous_business_plan": {},
        "selected_tables": ["dbo.orders"], "metric": "Revenue", "dimensions": ["Order Status"],
        "filters": [], "time_range": None,
        "clarification_selection": None, "cancel_clarification": False,
    }
    body = ComposerRequest(
        session_id="conv-1", message="What about last quarter?",
        selected_data_source=1, conversation_state=prior_state,
    )
    context = _build_conversation_context(body, _USER)

    assert context is not None
    assert context.previous_effective_question == "query revenue by status"
    assert context.turn_number == 2


# ---------------------------------------------------------------------------
# 12. Missing prior plan causes safe fallback (a follow-up phrase with no
#     usable previous_query_plan/previous_effective_question never crashes
#     and never guesses — it is answered as a fresh question).
# ---------------------------------------------------------------------------

def test_missing_prior_plan_falls_back_safely(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    _wire_success(monkeypatch)
    cc = ConversationContext(conversation_id="conv-1", source_id=1, user_id=_USER, created_at=_now_iso())

    state = answer_business_question(1, _USER, "What about last quarter?", conversation_context=cc)

    assert state.follow_up_type == "time_range_change"
    assert state.conversation_context_used is False
    assert state.status in (AgentStatus.SAFELY_REFUSED, AgentStatus.CLARIFICATION_REQUIRED)
    fallback_steps = [s for s in state.trace if s.tool == "apply_conversation_context"]
    assert len(fallback_steps) == 1
    assert fallback_steps[0].reason_code == "no_prior_plan"


# ---------------------------------------------------------------------------
# 13. Expired context is ignored.
# ---------------------------------------------------------------------------

def test_expired_context_is_ignored():
    stale_created_at = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
    candidate = ConversationContext(
        conversation_id="conv-1", source_id=1, user_id=_USER, created_at=stale_created_at,
    )
    assert _resolve_conversation_context(candidate, source_id=1, user_id=_USER) is None


def test_context_missing_created_at_is_treated_as_expired():
    candidate = ConversationContext(conversation_id="conv-1", source_id=1, user_id=_USER, created_at=None)
    assert _resolve_conversation_context(candidate, source_id=1, user_id=_USER) is None


# ---------------------------------------------------------------------------
# 14. Trace records context use without data leakage.
# ---------------------------------------------------------------------------

def test_trace_records_context_use_without_data_leakage(tmp_path, monkeypatch):
    cc = _first_turn_context(tmp_path, monkeypatch)
    _wire_success(monkeypatch, description=[("sum_amount",), ("status",)], rows=[(900.0, "Approved")])

    state = answer_business_question(1, _USER, "Only Approved.", conversation_context=cc)

    context_steps = [s for s in state.trace if s.tool == "apply_conversation_context"]
    assert len(context_steps) == 1
    blob = f"{context_steps[0].input_summary} {context_steps[0].output_summary}"
    # The follow-up's extracted value and the prior turn's full plan must
    # never appear in the trace — only IDs, the follow-up type, and counts.
    assert "Approved" not in blob
    assert "amount" not in blob
    assert "conversation_id=conv-1" in blob
    assert "follow_up_type=filter_add" in blob


# ---------------------------------------------------------------------------
# 15. Composer clarification requests no longer use the legacy Live Query
#     path (EnterpriseOrchestrator.process / context_builder._live_query).
# ---------------------------------------------------------------------------

def test_composer_clarification_resume_does_not_use_legacy_live_query_path(tmp_path, monkeypatch):
    tied_clients_env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())
    _wire_fake_connector(monkeypatch, description=[("count_id",)], rows=[(42,)])

    def _forbidden(*a, **k):
        raise AssertionError("legacy _live_query must not be called for a clarification resume")
    monkeypatch.setattr(context_builder_module, "_live_query", _forbidden)

    body = ComposerRequest(
        session_id=str(uuid.uuid4()), message="how many clients", selected_data_source=1,
        clarification_selection=[{"term": "clients", "table_fqn": "dbo.active_clients"}],
    )
    result = composer_ask(body, AuthenticatedUser(role="user", user_id=_USER))

    assert result["agent_status"] == AgentStatus.ANSWERED.value


def test_composer_clarification_cancel_does_not_use_legacy_live_query_path(tmp_path, monkeypatch):
    tied_clients_env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden(*a, **k):
        raise AssertionError("legacy _live_query must not be called for a clarification cancel")
    monkeypatch.setattr(context_builder_module, "_live_query", _forbidden)

    body = ComposerRequest(
        session_id=str(uuid.uuid4()), message="how many clients", selected_data_source=1,
        cancel_clarification=True,
    )
    result = composer_ask(body, AuthenticatedUser(role="user", user_id=_USER))

    assert result["agent_status"] == AgentStatus.SAFELY_REFUSED.value


# ---------------------------------------------------------------------------
# 16. Non-SQL composer flows remain unchanged by any of this milestone's
#     additions (conversation_state present, but intent isn't SQL_REQUEST).
# ---------------------------------------------------------------------------

def test_non_sql_intent_unaffected_by_conversation_state(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    monkeypatch.setattr(datasource_service, "get_connection_config", lambda sid, uid: _mssql_record())

    def _forbidden(*a, **k):
        raise AssertionError("answer_business_question must not be called for a non-SQL intent")
    monkeypatch.setattr(agent_module, "answer_business_question", _forbidden)

    body = ComposerRequest(
        session_id=str(uuid.uuid4()), message="show me the dictionary definitions for this data source",
        selected_data_source=1,
        conversation_state={
            "conversation_id": str(uuid.uuid4()), "source_id": 1, "user_id": _USER,
            "turn_number": 1, "created_at": _now_iso(),
        },
    )
    result = composer_ask(body, AuthenticatedUser(role="user", user_id=_USER))

    assert "agent_status" not in result


# ---------------------------------------------------------------------------
# Additional coverage — bounded follow-up classifier itself: a strict
# allow-list, not a general classifier.
# ---------------------------------------------------------------------------

def test_classify_follow_up_recognizes_the_six_documented_forms():
    from core.orchestrator.agent import _classify_follow_up

    assert _classify_follow_up("What about last month?") == ("time_range_change", "last month")
    assert _classify_follow_up("What about last quarter?") == ("time_range_change", "last quarter")
    assert _classify_follow_up("Only California.") == ("filter_add", "California")
    assert _classify_follow_up("Top 5 instead.") == ("limit_change", "Top 5")
    assert _classify_follow_up("Break it down by department.") == ("dimension_breakdown", "department")
    assert _classify_follow_up("Compare both periods.") == ("compare_periods", "Compare both periods.")


def test_classify_follow_up_returns_none_for_unrelated_text():
    from core.orchestrator.agent import _classify_follow_up

    assert _classify_follow_up("How many orders were placed last year across all regions?") is None
    assert _classify_follow_up("query revenue by status") is None


def test_compare_periods_is_safely_refused_not_guessed(tmp_path, monkeypatch):
    cc = _first_turn_context(tmp_path, monkeypatch)

    def _forbidden_execute(*a, **k):
        raise AssertionError("LiveQueryEngine.execute must not be called for an unsupported comparison follow-up")
    import core.live.query_engine as query_engine_module
    monkeypatch.setattr(query_engine_module.LiveQueryEngine, "execute", _forbidden_execute)

    state = answer_business_question(1, _USER, "Compare both periods.", conversation_context=cc)

    assert state.status == AgentStatus.SAFELY_REFUSED
    assert state.stop_code == "follow_up_unsupported"
    assert state.follow_up_type == "compare_periods"
    assert state.conversation_context_used is False
