"""
Enterprise Execution Planner (Phase 9) — tests.

Most scenarios are pure unit tests against ExecutionPlanner.plan(question) —
no source/semantic plan needed to classify dictionary/metadata/governance/
report/relationship/knowledge-graph/unknown questions correctly. The one
scenario that needs a real Semantic Plan ("Top 10 customers by revenue")
reuses the exact SQLite-seeding helpers from tests/test_semantic_query_planner.py.

Run from the project root:
    python -m pytest tests/test_execution_planner.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase9-execution-planner-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase9-salt-long-enough-value-1234567890")

import data.models as models
from core.execution.execution_strategy import StrategyType
from core.execution.planner import ExecutionPlanner
from core.orchestrator import EnterpriseOrchestrator, IntentType, OrchestratorRequest
from core.orchestrator.registry import ServiceRegistry

_NOW = "2026-07-09T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.query_planning_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    "data.semantic_layer_service",
    "data.search_service",
    "data.relationship_service",
    "data.dictionary_service",
    "data.domain_service",
    "data.entity_service",
    "data.governance_service",
    "data.schema_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase9.db")
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
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
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
    conn.commit()
    conn.close()
    return db_path


def _c(db_path):
    return _db_conn(db_path)


def _add_table(db, table_fqn, *, table_class="Transactional", row_count=1000, approved=True):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash(table_fqn)) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,'COMPLETE',?,?,?,?)",
        (tid, table_fqn, name, schema, table_class, row_count, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,'TABLE',?,?,?,?,?)",
        (table_fqn, name, schema, name.capitalize(), int(approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


_col_seq = [100]


def _add_column(db, table_fqn, col_name, *,
                data_type="DECIMAL", is_pk=0, is_id=0, uniqueness=0.05,
                is_nullable=0, null_pct=0.0, cardinality_tier="MEDIUM",
                pii=0, pii_confirmed=0,
                is_metric=None, is_dimension=None, is_date=None,
                business_label=None, approved=True):
    c = _c(db)
    _col_seq[0] += 1
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " cardinality_tier, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_col_seq[0], table_fqn, col_name, data_type, is_pk, is_id,
         uniqueness, is_nullable, null_pct, cardinality_tier, pii, pii_confirmed, _NOW, _NOW),
    )
    if is_metric is not None or is_dimension is not None or business_label or approved:
        c.execute(
            "INSERT OR REPLACE INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            " is_metric, is_dimension, is_date, is_id, pii_risk, is_approved, "
            " generation_method, created_at, updated_at) "
            "VALUES (1,1,?,?,?,?,?,?,?,?,?,?,?,?)",
            (table_fqn, col_name, business_label or col_name,
             int(bool(is_metric)), int(bool(is_dimension)),
             int(bool(is_date)), int(bool(is_id)), int(bool(pii)),
             int(bool(approved)), "rule_based", _NOW, _NOW),
        )
    c.commit()
    c.close()


def _add_fk(db, from_fqn, from_col, to_fqn, to_col, *, status="AUTO", confidence=1.0):
    c = _c(db)
    fs, ft = from_fqn.split(".")
    ts, tt = to_fqn.split(".")
    c.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status) "
        "VALUES (501,1,1,?,?,?,?,?,?,?,?,'FK_501','FOREIGN_KEY',?,'{}',?,?)",
        (fs, ft, from_fqn, from_col, ts, tt, to_fqn, to_col, confidence, _NOW, status),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# Pure unit tests — no source/semantic plan required
# ---------------------------------------------------------------------------

class TestDecisionRules:
    def test_dictionary_question(self):
        strategy = ExecutionPlanner().plan("What is Customer?")
        assert strategy.strategy_type == StrategyType.DICTIONARY_LOOKUP
        assert strategy.required_services == ["dictionary"]
        assert strategy.requires_dictionary is True

    def test_metadata_question(self):
        strategy = ExecutionPlanner().plan("What tables contain Customer?")
        assert strategy.strategy_type == StrategyType.METADATA_LOOKUP
        assert "schema" in strategy.required_services

    def test_governance_pii_question(self):
        strategy = ExecutionPlanner().plan("Show customer PII.")
        assert strategy.strategy_type == StrategyType.GOVERNANCE_CHECK
        assert "governance" in strategy.required_services
        assert "pii_review" in strategy.governance_checks

    def test_profiling_question(self):
        strategy = ExecutionPlanner().plan("Profile Sales table")
        assert strategy.strategy_type == StrategyType.PROFILING
        assert strategy.requires_profiling is True

    def test_report_generation_request(self):
        strategy = ExecutionPlanner().plan("Generate Executive Report")
        assert strategy.strategy_type == StrategyType.REPORT_GENERATION
        assert strategy.requires_report is True

    def test_relationship_question(self):
        strategy = ExecutionPlanner().plan("Show the foreign key relationship between orders and customers")
        assert strategy.strategy_type == StrategyType.RELATIONSHIP_LOOKUP

    def test_knowledge_graph_question(self):
        strategy = ExecutionPlanner().plan("Show me the knowledge graph for this source")
        assert strategy.strategy_type == StrategyType.KNOWLEDGE_GRAPH_LOOKUP

    def test_explain_entity_question(self):
        strategy = ExecutionPlanner().plan("Explain Sales")
        assert strategy.strategy_type == StrategyType.EXPLAIN_ENTITY
        for svc in ("dictionary", "domain", "entity", "knowledge_graph"):
            assert svc in strategy.required_services

    def test_unknown_question(self):
        strategy = ExecutionPlanner().plan("zorblaxian frobnicator qux")
        assert strategy.strategy_type == StrategyType.UNKNOWN
        assert strategy.confidence < 50
        assert strategy.warnings

    def test_analytical_question_without_semantic_plan_falls_back_to_keyword_heuristic(self):
        strategy = ExecutionPlanner().plan("total sales by region")
        assert strategy.strategy_type == StrategyType.SQL_REQUIRED
        assert strategy.requires_sql is True
        assert strategy.requires_live_data is True
        assert strategy.execution_order == ["semantic_query_plan", "sql_planner", "sql_generator", "live_query"]

    def test_never_executes_sql_or_reports(self, monkeypatch):
        # Any attempt to actually run something must fail the test.
        from core.live.query_engine import LiveQueryEngine

        def _boom_execute(self, *a, **k):
            raise AssertionError("LiveQueryEngine.execute must never be called by the Execution Planner")
        monkeypatch.setattr(LiveQueryEngine, "execute", _boom_execute)

        import data.sql_generation_service as sgs
        def _boom_generate(*a, **k):
            raise AssertionError("generate_sql must never be called by the Execution Planner")
        monkeypatch.setattr(sgs, "generate_sql", _boom_generate)

        import core.workflows.workflow_runner as wr
        def _boom_report(*a, **k):
            raise AssertionError("run_dataset_report_plan must never be called by the Execution Planner")
        monkeypatch.setattr(wr, "run_dataset_report_plan", _boom_report)

        planner = ExecutionPlanner()
        for question in (
            "What is Customer?", "What tables contain Customer?", "Show customer PII.",
            "Profile Sales table", "Generate Executive Report", "total sales by region",
            "Explain Sales", "zorblaxian",
        ):
            planner.plan(question)  # must not raise


# ---------------------------------------------------------------------------
# Semantic-plan-driven analytical detection (requires a real seeded source)
# ---------------------------------------------------------------------------

class TestSemanticPlanDrivenSqlRequired:
    def test_top_n_question_uses_semantic_plan_signal(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_table(db, "dbo.customers", table_class="Master")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)
        _add_column(db, "dbo.orders", "customer_id", data_type="INTEGER", uniqueness=0.02)
        _add_column(db, "dbo.customers", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
        _add_column(db, "dbo.customers", "name", data_type="TEXT", is_dimension=True,
                    cardinality_tier="MEDIUM", business_label="Customer Name", approved=True)
        _add_fk(db, "dbo.orders", "customer_id", "dbo.customers", "id")

        strategy = ExecutionPlanner().plan("top 10 customers by revenue", source_id=1, user_id="u1")
        assert strategy.strategy_type == StrategyType.SQL_REQUIRED
        assert strategy.requires_sql is True
        assert strategy.requires_live_data is True
        assert strategy.execution_order == ["semantic_query_plan", "sql_planner", "sql_generator", "live_query"]

    def test_missing_source_does_not_crash(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        # source_id 999 doesn't exist — SemanticQueryPlanner.plan returns None,
        # ExecutionPlanner must still return a strategy, not raise.
        strategy = ExecutionPlanner().plan("total revenue by region", source_id=999, user_id="u1")
        assert strategy is not None

