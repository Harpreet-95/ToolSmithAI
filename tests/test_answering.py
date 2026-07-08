"""
Enterprise Answer Generation Layer (Phase 10) — tests.

Most scenarios hand-build an EvidencePackage in-memory (mirroring
tests/test_composer_api.py::TestDeterministicAnswer's own convention) since
core/answering/ is a pure, deterministic transform over already-collected
evidence — no database needed to test it. A few tests exercise the real
orchestrator/composer integration end to end.

Run from the project root:
    python -m pytest tests/test_answering.py -v
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase10-answering-secret-long-enough-value1")
os.environ.setdefault("USER_ID_SALT", "test-phase10-salt-long-enough-value-1234567890")

from core.answering.answer_planner import AnswerPlanner
from core.answering.models import AnswerType, CitationType
from core.execution.execution_strategy import ExecutionStrategy, StrategyType
from core.orchestrator import EnterpriseOrchestrator, OrchestratorRequest
from core.orchestrator.models import (
    EvidenceItem, EvidencePackage, IntentType, ResolvedIntent, ServiceCapability,
)
from core.orchestrator.registry import ServiceRegistry


def _item(source_service, data, governance_state=None):
    return EvidenceItem(
        evidence_id=f"ev-{source_service}",
        source_service=source_service,
        source_function="test_fn",
        capability=ServiceCapability.SEARCH_READ,
        data=data,
        timestamp=datetime.utcnow(),
        confidence=1.0 if data else 0.0,
        governance_state=governance_state,
    )


def _package(intent_type, items, source_id=1):
    intent = ResolvedIntent(
        intent_type=intent_type, confidence=0.9,
        required_capabilities=[ServiceCapability.SEARCH_READ],
    )
    return EvidencePackage(
        request_id="test-req", query="test query", intent=intent,
        evidence=items, service_calls=[], built_at=datetime.utcnow(),
        source_id=source_id, errors=[], total_evidence_items=len(items),
        services_attempted=len(items), services_succeeded=len(items),
    )


def _strategy(**overrides):
    base = dict(strategy_type=StrategyType.DICTIONARY_LOOKUP)
    base.update(overrides)
    return ExecutionStrategy(**base)


# ---------------------------------------------------------------------------
# Supported answer categories
# ---------------------------------------------------------------------------

class TestSupportedAnswers:
    def test_dictionary_response(self):
        pkg = _package(IntentType.DICTIONARY, [
            _item("dictionary", [
                {"table_fqn": "dbo.customer", "business_name": "Customer", "is_approved": True},
            ]),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.DICTIONARY_LOOKUP), pkg)
        assert answer.answer_type == AnswerType.DICTIONARY
        assert "dbo.customer" in answer.answer
        assert "Customer" in answer.answer
        assert answer.confidence > 0

    def test_metadata_response(self):
        pkg = _package(IntentType.METADATA_LOOKUP, [
            _item("schema", {"table_count": 12, "view_count": 2, "column_count": 88,
                              "discovered_at": datetime.now(timezone.utc).isoformat()}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.METADATA_LOOKUP), pkg)
        assert answer.answer_type == AnswerType.METADATA
        assert "12" in answer.answer

    def test_entity_response(self):
        pkg = _package(IntentType.ENTITY, [
            _item("entity", {"tables_total": 10, "entities_assigned": 7,
                              "entity_counts": {"Customer": 4, "Order": 3}}),
        ])
        answer = AnswerPlanner().build(_strategy(), pkg)
        assert answer.answer_type == AnswerType.ENTITY
        assert "7" in answer.answer and "10" in answer.answer

    def test_governance_response(self):
        pkg = _package(IntentType.GOVERNANCE, [
            _item("governance", {"governance_score": 72, "total_governed": 50,
                                  "objects_ready": 40, "objects_pending": 8, "objects_escalated": 2}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.GOVERNANCE_CHECK), pkg)
        assert answer.answer_type == AnswerType.GOVERNANCE
        assert "72" in answer.answer

    def test_profiling_response(self):
        pkg = _package(IntentType.PROFILING, [
            _item("profiling", {"tables": [
                {"table_fqn": "dbo.customer", "exact_row_count": 1245987,
                 "pii_column_count": 1, "confirmed_pii_count": 1},
            ]}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.PROFILING), pkg)
        assert answer.answer_type == AnswerType.PROFILING
        assert "1,245,987" in answer.answer
        assert "PII" in answer.answer

    def test_relationship_response(self):
        pkg = _package(IntentType.RELATIONSHIP, [
            _item("relationship", {"total_relationships": 5, "tables_with_outbound_fks": 3,
                                    "most_referenced": [{"table_fqn": "dbo.customer", "inbound_count": 4}]}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.RELATIONSHIP_LOOKUP), pkg)
        assert answer.answer_type == AnswerType.RELATIONSHIP
        assert "5" in answer.answer

    def test_knowledge_graph_response(self):
        pkg = _package(IntentType.KNOWLEDGE_GRAPH, [
            _item("knowledge_graph", {"total_nodes": 20, "total_edges": 15, "metrics": {}}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.KNOWLEDGE_GRAPH_LOOKUP), pkg)
        assert answer.answer_type == AnswerType.KNOWLEDGE_GRAPH

    def test_live_query_summary(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {"execution_id": "abc123", "status": "success",
                                  "row_count": 10, "columns": [{"name": "id"}], "duration_ms": 42,
                                  "truncated": False}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.answer_type == AnswerType.LIVE_QUERY
        assert "10 row" in answer.answer

    def test_report_summary(self):
        pkg = _package(IntentType.REPORTS, [
            _item("reports", [{"id": 1, "title": "Q1 Summary"}, {"id": 2, "title": "Q2 Summary"}]),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.REPORT_GENERATION), pkg)
        assert answer.answer_type == AnswerType.REPORT
        assert "2" in answer.answer

    def test_restricted_response_short_circuits(self):
        pkg = _package(IntentType.PROFILING, [
            _item("profiling", {"tables": []}, governance_state="restricted_pii"),
        ])
        answer = AnswerPlanner().build(_strategy(), pkg)
        assert answer.answer_type == AnswerType.RESTRICTED

    def test_no_evidence_returns_unknown(self):
        pkg = _package(IntentType.UNKNOWN, [])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.UNKNOWN), pkg)
        assert answer.confidence == 0


# ---------------------------------------------------------------------------
# Recommendation rules
# ---------------------------------------------------------------------------

class TestRecommendations:
    def test_missing_profiling_recommends_deep_profile(self):
        pkg = _package(IntentType.DICTIONARY, [_item("dictionary", [{"table_fqn": "dbo.x", "business_name": "X", "is_approved": True}])])
        answer = AnswerPlanner().build(_strategy(requires_profiling=True), pkg)
        assert any(r.type == "deep_profile" for r in answer.recommendations)

    def test_missing_dictionary_recommends_generation(self):
        pkg = _package(IntentType.METADATA_LOOKUP, [_item("schema", {"table_count": 1})])
        answer = AnswerPlanner().build(_strategy(requires_dictionary=True), pkg)
        assert any(r.type == "dictionary_generation" for r in answer.recommendations)

    def test_governance_warning_recommends_review(self):
        pkg = _package(IntentType.GOVERNANCE, [_item("governance", {"governance_score": 40})])
        answer = AnswerPlanner().build(_strategy(governance_checks=["pii_review"]), pkg)
        rec = next(r for r in answer.recommendations if r.type == "review")
        assert rec.priority == "HIGH"

    def test_stale_metadata_recommends_rescan(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        pkg = _package(IntentType.METADATA_LOOKUP, [_item("schema", {"table_count": 5, "discovered_at": old})])
        answer = AnswerPlanner().build(_strategy(requires_metadata=True), pkg)
        assert any(r.type == "rescan" for r in answer.recommendations)

    def test_fresh_metadata_does_not_recommend_rescan(self):
        fresh = datetime.now(timezone.utc).isoformat()
        pkg = _package(IntentType.METADATA_LOOKUP, [_item("schema", {"table_count": 5, "discovered_at": fresh})])
        answer = AnswerPlanner().build(_strategy(requires_metadata=True), pkg)
        assert not any(r.type == "rescan" for r in answer.recommendations)


# ---------------------------------------------------------------------------
# Follow-up generation
# ---------------------------------------------------------------------------

class TestFollowUps:
    def test_dictionary_follow_ups_match_brief_examples(self):
        pkg = _package(IntentType.DICTIONARY, [_item("dictionary", [{"table_fqn": "dbo.x", "business_name": "X"}])])
        answer = AnswerPlanner().build(_strategy(), pkg)
        assert "What relationships exist?" in answer.follow_up_questions
        assert "Show profiling results." in answer.follow_up_questions

    def test_governance_follow_ups_match_brief_examples(self):
        pkg = _package(IntentType.GOVERNANCE, [_item("governance", {"governance_score": 80})])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.GOVERNANCE_CHECK), pkg)
        assert "Generate governance report." in answer.follow_up_questions
        assert "Explain related entities." in answer.follow_up_questions


# ---------------------------------------------------------------------------
# Citation generation
# ---------------------------------------------------------------------------

class TestCitations:
    def test_every_evidence_item_produces_a_citation(self):
        pkg = _package(IntentType.DICTIONARY, [
            _item("dictionary", [{"table_fqn": "dbo.x", "business_name": "X"}]),
            _item("relationship", {"total_relationships": 2}),
            _item("profiling", {"tables": [{"table_fqn": "dbo.x"}]}),
        ])
        answer = AnswerPlanner().build(_strategy(), pkg)
        assert len(answer.citations) >= 3
        types = {c.source_type for c in answer.citations}
        assert CitationType.DICTIONARY_ENTRY in types
        assert CitationType.RELATIONSHIP in types
        assert CitationType.PROFILING in types

    def test_unknown_service_gets_generic_citation(self):
        pkg = _package(IntentType.UNKNOWN, [_item("some_future_service", {"x": 1})])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.UNKNOWN), pkg)
        assert len(answer.citations) == 1
        assert answer.citations[0].source_type == CitationType.METADATA_SOURCE


# ---------------------------------------------------------------------------
# No-AI guarantee
# ---------------------------------------------------------------------------

class TestNoAI:
    def test_no_ai_imports_anywhere_in_answering_package(self):
        import ast
        import pathlib

        pkg_dir = pathlib.Path(__file__).parent.parent / "core" / "answering"
        banned = {"openai", "anthropic"}
        for path in pkg_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {n.name.split(".")[0] for n in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {(node.module or "").split(".")[0]}
                else:
                    continue
                assert not (names & banned), f"{path} imports {names & banned}"


# ---------------------------------------------------------------------------
# Orchestrator integration + backward compatibility
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def test_registry_has_nineteen_services(self):
        assert len(ServiceRegistry().get_all()) == 19

    def test_run_enterprise_answer_is_additive_over_process(self):
        req = OrchestratorRequest(query="show me the dictionary definitions", source_id=None, user_id="u1")
        orchestrator = EnterpriseOrchestrator()
        baseline = orchestrator.process(req)
        enriched = orchestrator.run_enterprise_answer(req)

        assert enriched.total_evidence_items == baseline.total_evidence_items + 1
        baseline_services = [e.source_service for e in baseline.evidence]
        enriched_services = [e.source_service for e in enriched.evidence]
        assert enriched_services[:-1] == baseline_services
        assert enriched_services[-1] == "enterprise_answer"

        extra = enriched.evidence[-1].data
        assert "enterprise_answer" in extra
        assert "execution_strategy" in extra


# ---------------------------------------------------------------------------
# Composer API backward compatibility — calls the real endpoint function
# ---------------------------------------------------------------------------

class TestComposerBackwardCompatibility:
    _ORIGINAL_KEYS = {
        "status", "request_id", "session_id", "business_answer", "resolved_intent",
        "services_selected", "evidence_summary", "evidence_package",
        "governance_state", "confidence", "execution_time", "warnings", "errors",
    }

    def test_response_keeps_all_original_keys_and_adds_two_new_ones(self):
        from auth.api_key import AuthenticatedUser
        from api.v1.composer import ComposerRequest, composer_ask

        body = ComposerRequest(session_id="sess-1", message="show me the dictionary")
        user = AuthenticatedUser(role="user", user_id="u1")

        response = composer_ask(body, user)

        assert self._ORIGINAL_KEYS.issubset(response.keys())
        assert "enterprise_answer" in response
        assert "execution_strategy" in response
        assert response["business_answer"] is not None  # existing field unaffected

    def test_enterprise_answer_present_and_shaped_correctly(self):
        from auth.api_key import AuthenticatedUser
        from api.v1.composer import ComposerRequest, composer_ask

        body = ComposerRequest(session_id="sess-2", message="show me the dictionary")
        user = AuthenticatedUser(role="user", user_id="u1")

        response = composer_ask(body, user)

        assert response["enterprise_answer"] is not None
        for key in ("answer", "summary", "answer_type", "confidence", "citations",
                    "recommendations", "follow_up_questions", "next_actions"):
            assert key in response["enterprise_answer"]
        assert response["execution_strategy"] is not None
        assert "strategy_type" in response["execution_strategy"]
