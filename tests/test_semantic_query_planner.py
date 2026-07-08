"""
Enterprise Semantic Query Planner (Phase 8) — tests.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern established by
tests/test_phase9_query_planning.py — this phase's engine sits directly on
top of data.query_planning_service.plan_business_query, so the same seeding
helpers apply. Extends the patched-module list to cover the additional
reads core/semantic/ makes (search, relationships, dictionary, domain,
entity, governance, schema).

Run from the project root:
    python -m pytest tests/test_semantic_query_planner.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase8-semantic-planner-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase8-salt-long-enough-value-1234567890")

import data.models as models
from core.orchestrator import EnterpriseOrchestrator, IntentType, OrchestratorRequest
from core.orchestrator.registry import ServiceRegistry
from core.semantic.execution_plan import ConceptStatus
from core.semantic.planner import SemanticQueryPlanner

_NOW = "2026-07-08T00:00:00+00:00"

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
    db_path = str(tmp_path / "phase8.db")
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


_rel_seq = [500]


def _add_fk(db, from_fqn, from_col, to_fqn, to_col, *, status="AUTO", confidence=1.0):
    c = _c(db)
    _rel_seq[0] += 1
    fs, ft = from_fqn.split(".")
    ts, tt = to_fqn.split(".")
    c.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,'FOREIGN_KEY',?,'{}',?,?)",
        (_rel_seq[0], fs, ft, from_fqn, from_col, ts, tt, to_fqn, to_col,
         f"FK_{_rel_seq[0]}", confidence, _NOW, status),
    )
    c.commit()
    c.close()


def _plan(db_path, question, filters=None):
    return SemanticQueryPlanner().plan(1, "u1", question, filters=filters)


# ---------------------------------------------------------------------------
# Revenue / Customer questions
# ---------------------------------------------------------------------------

class TestConceptQuestions:
    def test_revenue_question_resolves(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)
        _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                    cardinality_tier="LOW", business_label="Order Status", approved=True)

        plan = _plan(db, "revenue by status")
        assert plan is not None
        assert "dbo.orders" in plan.relevant_tables
        revenue_concepts = [c for c in plan.concepts if c.term == "revenue"]
        assert revenue_concepts
        assert revenue_concepts[0].status == ConceptStatus.RESOLVED
        assert "dbo.orders" in revenue_concepts[0].matched_tables

    def test_customer_question_resolves(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.customers", table_class="Master")
        _add_column(db, "dbo.customers", "name", data_type="TEXT", is_dimension=True,
                    cardinality_tier="MEDIUM", business_label="Customer Name", approved=True)

        plan = _plan(db, "list customer names")
        assert plan is not None
        customer_concepts = [c for c in plan.concepts if c.term == "customer"]
        assert customer_concepts
        assert customer_concepts[0].status == ConceptStatus.RESOLVED


# ---------------------------------------------------------------------------
# PII questions / governance restriction
# ---------------------------------------------------------------------------

class TestGovernanceAndPII:
    def test_pii_question_sets_governance_restricted(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.customers")
        _add_column(db, "dbo.customers", "revenue", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)
        _add_column(db, "dbo.customers", "email", data_type="TEXT", is_dimension=True,
                    cardinality_tier="MEDIUM", business_label="Email Address",
                    pii=1, pii_confirmed=0, approved=True)

        plan = _plan(db, "revenue by email")
        assert plan is not None
        assert plan.governance_restricted is True
        assert any(w["type"] == "pii_involved" for w in plan.governance_checks)

    def test_unapproved_metadata_sets_needs_dictionary(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.sales", approved=False)
        _add_column(db, "dbo.sales", "revenue", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=False)
        _add_column(db, "dbo.sales", "region", data_type="TEXT", is_dimension=True,
                    cardinality_tier="LOW", business_label="Region", approved=False)

        plan = _plan(db, "revenue by region")
        assert plan is not None
        assert plan.semantic_context.business_rules["needs_dictionary"] is True


# ---------------------------------------------------------------------------
# Relationship / cross-table questions
# ---------------------------------------------------------------------------

class TestRelationshipQuestions:
    def test_cross_table_question_includes_joins_and_fk_inventory(self, tmp_path, monkeypatch):
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

        plan = _plan(db, "revenue by customer")
        assert plan is not None
        assert plan.relationships["join_required"] is True
        assert plan.relationships["joins"][0]["path_found"] is True
        assert "dbo.orders" in plan.relationships["foreign_keys"]
        assert any(
            row["to_table_fqn"] == "dbo.customers"
            for row in plan.relationships["foreign_keys"]["dbo.orders"]["outbound"]
        )


# ---------------------------------------------------------------------------
# Unknown / ambiguous concepts
# ---------------------------------------------------------------------------

class TestConceptEdgeCases:
    def test_unknown_concept_does_not_crash(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)

        plan = _plan(db, "zorblaxian")
        assert plan is not None
        zorb = [c for c in plan.concepts if c.term == "zorblaxian"]
        assert zorb
        assert zorb[0].status == ConceptStatus.UNKNOWN

    def test_ambiguous_concept_detected(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders_fact")
        _add_table(db, "dbo.orders_summary")
        _add_column(db, "dbo.orders_fact", "revenue", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)
        _add_column(db, "dbo.orders_summary", "revenue_total", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)

        plan = _plan(db, "revenue")
        assert plan is not None
        revenue_concepts = [c for c in plan.concepts if c.term == "revenue"]
        assert revenue_concepts
        assert revenue_concepts[0].status in (ConceptStatus.AMBIGUOUS, ConceptStatus.RESOLVED)
        assert len(revenue_concepts[0].matched_tables) >= 2

    def test_unknown_source_returns_none(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        assert SemanticQueryPlanner().plan(999, "u1", "revenue by region") is None
        assert SemanticQueryPlanner().plan(1, "someone-else", "revenue by region") is None


# ---------------------------------------------------------------------------
# Structural / no-SQL guarantees
# ---------------------------------------------------------------------------

class TestExecutionPlanStructure:
    def test_no_sql_anywhere_in_plan(self, tmp_path, monkeypatch):
        import json
        import re

        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)
        _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                    cardinality_tier="LOW", business_label="Status", approved=True)

        plan = _plan(db, "revenue by status")
        text = json.dumps(plan.to_dict())
        assert not re.search(r'\bSELECT\s+\w', text, re.IGNORECASE)
        assert not re.search(r'\bINSERT\s+INTO\b', text, re.IGNORECASE)
        assert not re.search(r'\bWHERE\s+\w+\s*=', text, re.IGNORECASE)

    def test_plan_serializes_and_has_required_sections(self, tmp_path, monkeypatch):
        import json

        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)

        plan = _plan(db, "revenue")
        d = plan.to_dict()
        for key in (
            "question", "concepts", "relevant_tables", "relevant_columns",
            "relationships", "required_filters", "governance_restricted",
            "governance_checks", "recommended_query_strategy", "semantic_context",
            "confidence", "warnings", "explanation",
        ):
            assert key in d
        json.dumps(d)  # must be JSON-serializable

    def test_recommended_query_strategy_has_intent_and_roles(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)
        _add_column(db, "dbo.orders", "status", data_type="TEXT", is_dimension=True,
                    cardinality_tier="LOW", business_label="Status", approved=True)

        plan = _plan(db, "revenue by status")
        strategy = plan.recommended_query_strategy
        assert "intent_type" in strategy
        assert "table_roles" in strategy
        assert "dbo.orders" in strategy["table_roles"]


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def test_registry_has_at_least_the_semantic_query_plan_service(self):
        # Exact count is asserted in tests/test_execution_planner.py, which
        # tracks the current total (18, after Phase 9 added
        # "execution_planner"). This test only pins the Phase 8 addition so
        # it doesn't need updating every time a later phase registers
        # another service.
        service_ids = [s.service_id for s in ServiceRegistry().get_all()]
        assert "semantic_query_plan" in service_ids

    def test_run_semantic_query_plan_selects_service(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)

        req = OrchestratorRequest(
            query="anything", source_id=1, user_id="u1",
            params={"question": "revenue"},
        )
        package = EnterpriseOrchestrator().run_semantic_query_plan(req)
        assert package.intent.intent_type == IntentType.SEMANTIC_QUERY_PLAN
        service_ids = [c.service_id for c in package.service_calls]
        assert "semantic_query_plan" in service_ids
        item = next(e for e in package.evidence if e.source_service == "semantic_query_plan")
        assert item.data["question"] == "revenue"

    def test_chat_phrase_routes_to_semantic_query_plan_intent(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _add_table(db, "dbo.orders")
        _add_column(db, "dbo.orders", "amount", data_type="DECIMAL", is_metric=True,
                    business_label="Revenue", approved=True)

        req = OrchestratorRequest(
            query="give me a query plan for revenue by region", source_id=1, user_id="u1",
        )
        package = EnterpriseOrchestrator().process(req)
        assert package.intent.intent_type == IntentType.SEMANTIC_QUERY_PLAN

    def test_existing_chat_phrases_unaffected(self):
        # Regression: unrelated existing intents must still resolve unchanged.
        req = OrchestratorRequest(query="show me the dictionary definitions", source_id=None, user_id="u1")
        package = EnterpriseOrchestrator().process(req)
        assert package.intent.intent_type == IntentType.DICTIONARY

        req2 = OrchestratorRequest(query="governance compliance pii sensitive stewardship", source_id=None, user_id="u1")
        package2 = EnterpriseOrchestrator().process(req2)
        assert package2.intent.intent_type == IntentType.GOVERNANCE
