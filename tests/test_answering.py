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

import pytest
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
# live_query SQL-generation-refusal — the evidence shape
# core.orchestrator.context_builder._live_query() returns when
# generate_sql() refuses (no "status" key at all, unlike a real QueryResult).
# Previously fell through to the status check and rendered
# "status: None"; must now be detected before that and explained clearly.
# ---------------------------------------------------------------------------

class TestLiveQuerySqlGenerationRefusal:
    def test_refusal_shows_clear_answer_via_reason(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False,
                "reason": "sql_generation_refused",
                "explanation": [
                    "Generation refused: Unresolved term(s) cannot be planned: clients, system.",
                ],
                "warnings": [
                    {"type": "missing_measure", "severity": "MEDIUM",
                     "message": "No measure candidate found for 'clients'."},
                ],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert answer.answer_type == AnswerType.LIVE_QUERY
        assert "could not be translated into a sql query" in answer.answer.lower()
        assert "unresolved term" in answer.answer.lower()
        assert answer.confidence < 50

    def test_refusal_shows_clear_answer_via_executed_false_only(self):
        # "executed": False alone (no "reason" key) must also be detected —
        # proves the "or" branch, not just the "reason" check.
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {"executed": False, "explanation": [], "warnings": []}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert answer.answer_type == AnswerType.LIVE_QUERY
        assert "could not be translated into a sql query" in answer.answer.lower()

    def test_refusal_does_not_display_status_none(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False,
                "reason": "sql_generation_refused",
                "explanation": ["Generation refused: No measures or dimensions resolved."],
                "warnings": [],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert "status: none" not in answer.answer.lower()
        assert "None" not in answer.answer

    def test_successful_live_query_answer_unchanged(self):
        # Regression: a genuine QueryResult-shaped success dict (has "status",
        # no "executed"/"reason" keys) must take the original path unchanged.
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {"execution_id": "abc123", "status": "success",
                                  "row_count": 10, "columns": [{"name": "id"}], "duration_ms": 42,
                                  "truncated": False}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert answer.answer_type == AnswerType.LIVE_QUERY
        assert "10 row" in answer.answer
        assert answer.confidence == 95
        assert "could not be translated" not in answer.answer.lower()
        assert "status: none" not in answer.answer.lower()

    def test_failed_execution_status_still_shows_status_value(self):
        # Regression: a real QueryResult that executed but failed/blocked
        # (has "status" != "success") must still take the original
        # "did not complete successfully" path, unaffected by the new check.
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {"execution_id": "abc123", "status": "blocked",
                                  "error": "live_query_enabled is not enabled for this source."}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert answer.answer_type == AnswerType.LIVE_QUERY
        assert "status: blocked" in answer.answer.lower()
        assert "not enabled" in answer.answer.lower()


# ---------------------------------------------------------------------------
# Milestone M-25 — Enterprise Answer Value Rendering.
#
# core.orchestrator.context_builder._live_query()'s success branch now
# attaches a "business_plan" dict (see _build_business_plan) alongside the
# QueryResult fields. These tests hand-build that combined evidence shape and
# assert the resulting EnterpriseAnswer renders real business language
# instead of a technical row/column-count sentence, across every result
# shape in the milestone brief. A live_query dict with NO "business_plan" key
# (the raw-SQL trusted-caller bypass, exercised above in
# TestLiveQuerySqlGenerationRefusal) is intentionally left untouched — the
# fallback branch reproduces the original count-only sentence.
# ---------------------------------------------------------------------------

def _business_plan(**overrides):
    base = {
        "aggregation": None, "aggregation_target": None, "distinct": False,
        "entity_label": "clients", "measure_label": None,
        "select": [], "where": [], "group_by": [], "order_by": [],
        "order_intent": None, "dimension_labels": {}, "date_context": None,
        "status_label": None, "source_tables": ["dbo.Client"],
    }
    base.update(overrides)
    return base


def _success_data(business_plan, **overrides):
    base = {
        "execution_id": "exec-1", "status": "success", "duration_ms": 12,
        "row_count": 1, "columns": [], "rows": [], "truncated": False,
        "business_plan": business_plan,
    }
    base.update(overrides)
    return base


def _build_live_query_answer(data):
    pkg = _package(IntentType.SQL_REQUEST, [_item("live_query", data)])
    return AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)


class TestLiveQueryBusinessValueRendering:
    def test_scalar_count(self):
        plan = _business_plan(
            aggregation="COUNT", aggregation_target="entity_count", entity_label="clients",
            select=[{"table_fqn": "dbo.Client", "column_name": None, "alias": "row_count",
                     "aggregation": "COUNT", "distinct": False}],
        )
        data = _success_data(plan, row_count=1, columns=[{"name": "row_count"}],
                              rows=[{"row_count": 2218}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "There are 2,218 clients in the database."
        assert answer.actual_value == 2218
        assert answer.business_entity == "clients"
        assert answer.aggregation == "COUNT"

    def test_scalar_count_distinct(self):
        plan = _business_plan(
            aggregation="COUNT", aggregation_target="distinct_entity_count", distinct=True,
            entity_label="clients",
            select=[{"table_fqn": "dbo.Client", "column_name": "client_id", "alias": "count_client_id",
                     "aggregation": "COUNT", "distinct": True}],
        )
        data = _success_data(plan, row_count=1, columns=[{"name": "count_client_id"}],
                              rows=[{"count_client_id": 150}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "There are 150 unique clients in the database."
        assert answer.actual_value == 150

    def test_scalar_count_with_status_filter(self):
        plan = _business_plan(
            aggregation="COUNT", entity_label="clients", status_label="Active",
            where=[{"table_fqn": "dbo.Client", "column_name": "status", "operator": "=", "value": "Active"}],
            select=[{"table_fqn": "dbo.Client", "column_name": None, "alias": "row_count",
                     "aggregation": "COUNT", "distinct": False}],
        )
        data = _success_data(plan, rows=[{"row_count": 42}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "There are 42 active clients in the database."
        assert len(answer.applied_filters) == 1

    def test_scalar_sum(self):
        plan = _business_plan(aggregation="SUM", measure_label="payroll",
                               select=[{"table_fqn": "dbo.Payroll", "column_name": "amount",
                                        "alias": "sum_amount", "aggregation": "SUM", "distinct": False}])
        data = _success_data(plan, rows=[{"sum_amount": 1240550}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "Total payroll is 1,240,550."
        assert answer.actual_value == 1240550
        assert answer.measure == "payroll"
        assert "$" not in answer.answer  # no governed currency metadata exists — never invent a symbol

    def test_scalar_avg(self):
        plan = _business_plan(aggregation="AVG", measure_label="order amount")
        data = _success_data(plan, rows=[{"avg_amount": 523.4}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "Average order amount is 523.40."

    def test_scalar_min_max(self):
        plan = _business_plan(aggregation="MIN", measure_label="order amount")
        data = _success_data(plan, rows=[{"min_amount": 12}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "Minimum order amount is 12."

    def test_grouped_result(self):
        plan = _business_plan(
            aggregation="COUNT", aggregation_target="entity_count", entity_label="clients",
            group_by=[{"table_fqn": "dbo.Client", "column_name": "region"}],
            dimension_labels={"region": "Region"},
            select=[
                {"table_fqn": "dbo.Client", "column_name": "region", "alias": "region",
                 "aggregation": None, "distinct": False},
                {"table_fqn": "dbo.Client", "column_name": None, "alias": "row_count",
                 "aggregation": "COUNT", "distinct": False},
            ],
        )
        data = _success_data(
            plan, row_count=2, columns=[{"name": "region"}, {"name": "row_count"}],
            rows=[{"region": "West", "row_count": 10}, {"region": "East", "row_count": 5}],
        )
        answer = _build_live_query_answer(data)
        assert answer.answer == "Most clients are West. The current breakdown is 10 west and 5 east."
        assert len(answer.result_preview) == 2
        # raw column/alias keys must not leak into the preview unlabeled
        assert "region" not in answer.result_preview[0]
        assert "row_count" not in answer.result_preview[0]
        assert "Region" in answer.result_preview[0]

    def test_ranked_result(self):
        plan = _business_plan(
            aggregation="SUM", measure_label="revenue", entity_label="clients",
            order_intent={"direction": "DESC", "limit": 10},
            dimension_labels={"name": "Client Name"},
            select=[
                {"table_fqn": "dbo.Client", "column_name": "name", "alias": "name",
                 "aggregation": None, "distinct": False},
                {"table_fqn": "dbo.Orders", "column_name": "amount", "alias": "sum_amount",
                 "aggregation": "SUM", "distinct": False},
            ],
        )
        rows = [{"name": f"Client {i}", "sum_amount": 1000 * i} for i in range(10, 0, -1)]
        data = _success_data(plan, row_count=10, truncated=True, rows=rows)
        answer = _build_live_query_answer(data)
        assert answer.answer.startswith("Client 10 leads with 10,000, followed by Client 9 and Client 8.")
        assert "truncated" in answer.answer.lower()
        assert answer.truncation_notice is not None
        assert len(answer.result_preview) == 10

    def test_tabular_result(self):
        plan = _business_plan(entity_label="orders",
                               select=[{"table_fqn": "dbo.Orders", "column_name": "id", "alias": "id",
                                        "aggregation": None, "distinct": False}])
        data = _success_data(plan, row_count=3, rows=[{"id": 1}, {"id": 2}, {"id": 3}])
        answer = _build_live_query_answer(data)
        assert "3" in answer.answer
        assert "orders" in answer.answer
        assert answer.result_preview

    def test_empty_result_with_filters(self):
        plan = _business_plan(
            entity_label="clients",
            where=[{"table_fqn": "dbo.Client", "column_name": "status", "operator": "=", "value": "Closed"}],
        )
        data = _success_data(plan, row_count=0, rows=[])
        answer = _build_live_query_answer(data)
        assert answer.answer == "No matching clients were found for the selected filters."
        assert answer.result_preview == []

    def test_empty_result_without_filters(self):
        plan = _business_plan(entity_label="clients")
        data = _success_data(plan, row_count=0, rows=[])
        answer = _build_live_query_answer(data)
        assert answer.answer == "No matching clients were found."

    def test_null_scalar_result(self):
        plan = _business_plan(aggregation="SUM", measure_label="order amount")
        data = _success_data(plan, row_count=1, rows=[{"sum_amount": None}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "The query completed, but no value was available for order amount."
        assert answer.actual_value is None
        assert answer.limitations

    def test_null_scalar_is_distinct_from_empty(self):
        # Empty (0 rows) and a NULL aggregate (1 row, NULL value) are
        # different outcomes and must not share an answer.
        null_plan = _business_plan(aggregation="SUM", measure_label="order amount")
        empty_plan = _business_plan(entity_label="orders")
        null_answer = _build_live_query_answer(_success_data(null_plan, row_count=1, rows=[{"sum_amount": None}]))
        empty_answer = _build_live_query_answer(_success_data(empty_plan, row_count=0, rows=[]))
        assert null_answer.answer != empty_answer.answer

    def test_date_context_and_applied_filters(self):
        plan = _business_plan(
            aggregation="SUM", measure_label="payroll",
            where=[{"table_fqn": "dbo.Payroll", "column_name": "pay_date", "operator": "BETWEEN",
                    "value": ["2026-07-01", "2026-07-31"]}],
            date_context={"label": "this month", "start": "2026-07-01", "end": "2026-07-31"},
        )
        data = _success_data(plan, rows=[{"sum_amount": 1240550}])
        answer = _build_live_query_answer(data)
        assert answer.date_context == {"label": "this month", "start": "2026-07-01", "end": "2026-07-31"}
        assert len(answer.applied_filters) == 1
        assert answer.applied_filters[0]["label"] == "Pay Date"

    def test_execution_failure_not_shown_as_zero_result(self):
        # Regression: a real failure must never render like an empty/zero
        # business answer — this is the pre-existing non-success branch,
        # untouched by the business_plan/result_formatter changes.
        data = {"execution_id": "e1", "status": "failed", "error": "Connection timed out."}
        answer = _build_live_query_answer(data)
        assert "no matching" not in answer.answer.lower()
        assert "did not complete successfully" in answer.answer.lower()

    def test_raw_identifiers_hidden_from_primary_answer_text(self):
        plan = _business_plan(
            aggregation="COUNT", entity_label="clients",
            select=[{"table_fqn": "dbo.Client", "column_name": None, "alias": "row_count",
                     "aggregation": "COUNT", "distinct": False}],
            source_tables=["dbo.Client"],
        )
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert "dbo.Client" not in answer.answer
        assert "row_count" not in answer.answer
        # raw identifiers are still available, just not in the primary text
        assert answer.source_tables == ["dbo.Client"]

    def test_clarification_resumed_final_answer_uses_same_templates(self):
        # A clarification-resumed question re-enters _live_query()'s success
        # branch exactly like any other query — no special-cased answer path
        # exists or should exist for it.
        plan = _business_plan(
            aggregation="COUNT", aggregation_target="entity_count", entity_label="active clients",
            select=[{"table_fqn": "dbo.ADF_Clients", "column_name": None, "alias": "row_count",
                     "aggregation": "COUNT", "distinct": False}],
        )
        data = _success_data(plan, rows=[{"row_count": 87}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "There are 87 active clients in the database."


# ---------------------------------------------------------------------------
# Milestone Phase 6.6 — Enterprise Clarification Intelligence.
#
# core.orchestrator.context_builder._live_query() returns a
# "clarification_required" evidence shape (see there) whenever
# query_planning_service's own ranking left a measure/dimension ambiguous
# (selected=None, an "ambiguous_*" warning, >=2 ranked candidates) instead
# of guessing. These tests exercise the resulting EnterpriseAnswer directly
# against that hand-built evidence shape, the same convention as
# TestLiveQuerySqlGenerationRefusal above.
# ---------------------------------------------------------------------------

_AMBIGUOUS_DATASETS = [
    ("clients", [
        {"table_fqn": "dbo.active_clients", "column_name": None, "business_label": "Active Clients", "score": 0.62},
        {"table_fqn": "dbo.legacy_clients", "column_name": None, "business_label": "Historical Clients", "score": 0.60},
        {"table_fqn": "dbo.staffing_clients", "column_name": None, "business_label": "Staffing Clients", "score": 0.59},
    ]),
    ("payroll", [
        {"table_fqn": "dbo.payroll_current", "column_name": None, "business_label": "Current Payroll", "score": 0.58},
        {"table_fqn": "dbo.payroll_archive", "column_name": None, "business_label": "Archived Payroll", "score": 0.55},
    ]),
    ("candidates", [
        {"table_fqn": "dbo.candidate_pool", "column_name": None, "business_label": "Candidate Pool", "score": 0.57},
        {"table_fqn": "dbo.candidate_pipeline", "column_name": None, "business_label": "Candidate Pipeline", "score": 0.56},
    ]),
    ("invoices", [
        {"table_fqn": "dbo.invoices_ar", "column_name": None, "business_label": "Accounts Receivable Invoices", "score": 0.60},
        {"table_fqn": "dbo.invoices_billing", "column_name": None, "business_label": "Billing Invoices", "score": 0.59},
    ]),
    ("projects", [
        {"table_fqn": "dbo.projects_active", "column_name": None, "business_label": "Active Projects", "score": 0.61},
        {"table_fqn": "dbo.projects_closed", "column_name": None, "business_label": "Closed Projects", "score": 0.60},
    ]),
]


class TestClarificationNeeded:
    @pytest.mark.parametrize("term,candidates", _AMBIGUOUS_DATASETS)
    def test_ambiguous_entity_count_returns_clarification(self, term, candidates):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False,
                "reason": "clarification_required",
                "question": f"how many {term}",
                "ambiguous_terms": [{"term": term, "kind": "measure", "candidates": candidates}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert answer.answer_type == AnswerType.CLARIFICATION_NEEDED
        assert answer.confidence == 0
        assert answer.clarification is not None
        assert len(answer.clarification["options"]) == len(candidates)
        for option, candidate in zip(answer.clarification["options"], candidates):
            assert option["label"] == candidate["business_label"]
            assert option["table_fqn"] == candidate["table_fqn"]
        assert term in answer.clarification["reason"]
        assert "no sql" in " ".join(answer.limitations).lower()
        # Never silently executes — the answer text asks, it doesn't guess.
        assert "which would you like to use" in answer.answer.lower()

    def test_no_business_label_falls_back_to_humanized_table_name(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False,
                "reason": "clarification_required",
                "question": "how many widgets",
                "ambiguous_terms": [{
                    "term": "widgets", "kind": "measure",
                    "candidates": [
                        {"table_fqn": "dbo.widget_orders", "column_name": None, "business_label": None, "score": 0.5},
                        {"table_fqn": "dbo.widget_returns", "column_name": None, "business_label": None, "score": 0.49},
                    ],
                }],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        labels = [o["label"] for o in answer.clarification["options"]]
        assert labels == ["Widget Orders", "Widget Returns"]

    def test_multiple_ambiguous_terms_in_one_question(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False,
                "reason": "clarification_required",
                "question": "clients by region",
                "ambiguous_terms": [
                    {"term": "clients", "kind": "measure", "candidates": _AMBIGUOUS_DATASETS[0][1]},
                    {"term": "region", "kind": "dimension", "candidates": [
                        {"table_fqn": "dbo.region_current", "column_name": "region", "business_label": "Region", "score": 0.5},
                        {"table_fqn": "dbo.region_legacy", "column_name": "region_code", "business_label": "Legacy Region", "score": 0.48},
                    ]},
                ],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)

        assert answer.answer_type == AnswerType.CLARIFICATION_NEEDED
        assert len(answer.clarification["options"]) == 5
        assert {o["term"] for o in answer.clarification["options"]} == {"clients", "region"}

    def test_high_confidence_no_clarification_unchanged(self):
        # Regression: a normal successful live query must not be affected by
        # the new branch at all.
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {"execution_id": "abc123", "status": "success",
                                  "row_count": 3, "columns": [{"name": "id"}], "duration_ms": 12,
                                  "truncated": False}),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.answer_type == AnswerType.LIVE_QUERY
        assert answer.clarification is None


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
