"""
Enterprise Answer Generation Layer (Phase 10) — tests.

Most scenarios hand-build an EvidencePackage in-memory (mirroring
tests/test_composer_api.py::TestDeterministicAnswer's own convention) since
core/answering/ is a pure, deterministic transform over already-collected
evidence — no database needed to test it.

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
from core.orchestrator.models import (
    EvidenceItem, EvidencePackage, IntentType, ResolvedIntent, ServiceCapability,
)


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
# Live-query SQL-generation refusal evidence has no "status" key, unlike a
# successful QueryResult.
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
        # Rule G — no hardcoded 95%: with no business_plan on this raw dict
        # (no plan-level confidence signal), the final confidence falls back
        # to the package's own real intent confidence (0.9 -> 90), not a
        # flat constant.
        assert answer.confidence == 90
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
# Enterprise Answer Value Rendering.
#
# These tests hand-build the historical combined business-plan/result shape and
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
            select=[{"table_fqn": "dbo.EDU_Clients", "column_name": None, "alias": "row_count",
                     "aggregation": "COUNT", "distinct": False}],
        )
        data = _success_data(plan, rows=[{"row_count": 87}])
        answer = _build_live_query_answer(data)
        assert answer.answer == "There are 87 active clients in the database."


# ---------------------------------------------------------------------------
# Enterprise Clarification Intelligence.
#
# The "clarification_required" evidence shape is used whenever
# ranking left a measure/dimension ambiguous
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

    # -----------------------------------------------------------------
    # Accuracy Program A4, Fix #2 — business-friendly label precedence.
    # dictionary business_name > entity-assignment name (excluding
    # Unknown/Generic) > existing business_label > humanized table name >
    # raw table_fqn. Display-only: must not touch score/order/selection.
    # -----------------------------------------------------------------

    def test_dictionary_business_name_used_first(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{
                    "term": "candidates", "kind": "measure",
                    "candidates": [{
                        "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
                        "business_label": "BH Candidate Records",
                        "table_business_name": "Candidates",
                        "entity_name": "Candidate",
                        "score": 0.6,
                    }],
                }],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["label"] == "Candidates"

    def test_entity_assignment_used_when_business_name_absent(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{
                    "term": "candidates", "kind": "measure",
                    "candidates": [{
                        "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
                        "business_label": "BH Candidate Records",
                        "table_business_name": None,
                        "entity_name": "Candidate",
                        "score": 0.6,
                    }],
                }],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["label"] == "Candidate"

    def test_unknown_entity_name_ignored(self):
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{
                    "term": "candidates", "kind": "measure",
                    "candidates": [{
                        "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
                        "business_label": "BH Candidate Records",
                        "table_business_name": None,
                        "entity_name": "Unknown",
                        "score": 0.6,
                    }, {
                        "table_fqn": "dbo.EDU_StaffingCandidates2", "column_name": None,
                        "business_label": "BH Candidate Records 2",
                        "table_business_name": None,
                        "entity_name": "Generic",
                        "score": 0.5,
                    }],
                }],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        labels = [o["label"] for o in answer.clarification["options"]]
        assert labels == ["BH Candidate Records", "BH Candidate Records 2"]

    def test_business_label_remains_fallback_after_entity(self):
        # No dictionary business_name, no usable entity name -> falls to the
        # existing column-level business_label, not the humanized table name.
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{
                    "term": "candidates", "kind": "measure",
                    "candidates": [{
                        "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
                        "business_label": "BH Candidate Records",
                        "table_business_name": None,
                        "entity_name": None,
                        "score": 0.6,
                    }],
                }],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["label"] == "BH Candidate Records"

    def test_raw_table_fqn_is_final_fallback(self):
        # No dictionary/entity/business_label metadata AND humanizing the
        # table name itself yields nothing usable (trailing-dot fqn) -> the
        # raw table_fqn is shown rather than an empty label.
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show widgets",
                "ambiguous_terms": [{
                    "term": "widgets", "kind": "measure",
                    "candidates": [{
                        "table_fqn": "dbo.", "column_name": None,
                        "business_label": None, "table_business_name": None,
                        "entity_name": None, "score": 0.5,
                    }],
                }],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["label"] == "dbo."

    def test_fix1_and_fix2_behavior_remains_intact_with_fix3_fields_present(self):
        # Accuracy Program A4, Fix #3 — the option contract intentionally
        # grows five additive fields; table_fqn/score/label/description keep
        # their exact pre-Fix-#3 values and semantics (no raw table_fqn or
        # confidence number in description; label still Fix #2's precedence).
        candidate = {
            "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
            "business_label": "BH Candidate Records",
            "table_business_name": "Candidates", "entity_name": "Candidate",
            "score": 0.8734,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{"term": "candidates", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        option = answer.clarification["options"][0]
        assert set(option.keys()) == {
            "id", "term", "table_fqn", "column_name", "label", "description", "score",
            "object_type", "business_name", "domain", "entity", "why_relevant",
        }
        assert option["table_fqn"] == "dbo.EDU_StaffingCandidates"
        assert option["score"] == 0.8734
        assert option["label"] == "Candidates"  # Fix #2: dictionary business_name wins
        assert "dbo.EDU_StaffingCandidates" not in option["description"]
        assert "0.87" not in option["description"] and "confidence" not in option["description"].lower()

    # -----------------------------------------------------------------
    # Accuracy Program A4, Fix #3 — additive safe clarification metadata
    # (object_type/business_name/domain/entity/why_relevant). Display and
    # evidence only: must not affect score, order, selection, or resume.
    # -----------------------------------------------------------------

    def test_object_type_included_when_available(self):
        candidate = {
            "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
            "business_label": None, "table_object_type": "VIEW", "score": 0.5,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{"term": "candidates", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["object_type"] == "VIEW"

    def test_business_name_field_included_safely(self):
        candidate = {
            "table_fqn": "dbo.EDU_StaffingCandidates", "column_name": None,
            "business_label": None, "table_business_name": "  Candidates  ", "score": 0.5,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show candidates",
                "ambiguous_terms": [{"term": "candidates", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["business_name"] == "Candidates"

    def test_unknown_generic_domain_and_entity_excluded(self):
        candidate = {
            "table_fqn": "dbo.T004", "column_name": None, "business_label": None,
            "domain_name": "Unknown", "entity_name": "Generic", "score": 0.5,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show widgets",
                "ambiguous_terms": [{"term": "widgets", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        option = answer.clarification["options"][0]
        assert option["domain"] is None
        assert option["entity"] is None

    def test_why_relevant_deterministic_and_capped_at_three(self):
        candidate = {
            "table_fqn": "dbo.placements", "column_name": "placement_id",
            "business_label": None, "table_business_name": "Placements",
            "entity_name": "Placement", "domain_name": "Operations",
            "relationship_related_tables": ["dbo.candidates", "dbo.clients"],
            "score": 0.7,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show placements",
                "ambiguous_terms": [{"term": "placements", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer1 = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        answer2 = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        why1 = answer1.clarification["options"][0]["why_relevant"]
        why2 = answer2.clarification["options"][0]["why_relevant"]
        assert why1 == why2  # deterministic: identical input -> identical output
        assert len(why1) <= 3
        assert why1[0] == "Business name matches the requested term."

    def test_why_relevant_empty_when_no_factual_reason_available(self):
        candidate = {
            "table_fqn": "dbo.T004", "column_name": None, "business_label": None, "score": 0.5,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show widgets",
                "ambiguous_terms": [{"term": "widgets", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        assert answer.clarification["options"][0]["why_relevant"] == []

    def test_why_relevant_never_contains_score_or_sql_or_sensitive_values(self):
        candidate = {
            "table_fqn": "dbo.placements", "column_name": "placement_id",
            "business_label": None, "table_business_name": "Placements",
            "entity_name": "Placement", "domain_name": "Operations",
            "relationship_related_tables": ["dbo.candidates", "dbo.clients"],
            "score": 0.913482,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show placements",
                "ambiguous_terms": [{"term": "placements", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        why = answer.clarification["options"][0]["why_relevant"]
        joined = " ".join(why).lower()
        assert "0.91" not in joined and "confidence" not in joined
        assert "select" not in joined and "from" not in joined
        assert "dbo.placements" not in joined  # raw table_fqn stays out of why_relevant text

    def test_old_candidates_missing_fix3_metadata_still_serialize_safely(self):
        # Pre-Fix-#3-shaped candidate (Fix #1/#2 only) — no crash, all five
        # new fields present with safe defaults.
        candidate = {
            "table_fqn": "dbo.widget_orders", "column_name": None,
            "business_label": "Widget Orders", "score": 0.5,
        }
        pkg = _package(IntentType.SQL_REQUEST, [
            _item("live_query", {
                "executed": False, "reason": "clarification_required",
                "question": "show widgets",
                "ambiguous_terms": [{"term": "widgets", "kind": "measure", "candidates": [candidate]}],
            }),
        ])
        answer = AnswerPlanner().build(_strategy(strategy_type=StrategyType.SQL_REQUIRED), pkg)
        option = answer.clarification["options"][0]
        assert option["object_type"] is None
        assert option["business_name"] is None
        assert option["domain"] is None
        assert option["entity"] is None
        assert option["why_relevant"] == []
        assert option["label"] == "Widget Orders"

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
# Phase 3, Step 4 — remembered-terminology explainability
# ---------------------------------------------------------------------------

class TestRememberedTerminologyExplainability:
    def _remembered(self, answer):
        return [c for c in answer.citations if c.source_type == CitationType.REMEMBERED_TERMINOLOGY]

    def test_remembered_synonym_creates_one_evidence_citation(self):
        plan = _business_plan(remembered_terminology=[{
            "evidence_type": "remembered_terminology",
            "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert len(self._remembered(answer)) == 1

    def test_citation_preserves_original_and_canonical_terms(self):
        plan = _business_plan(remembered_terminology=[{
            "evidence_type": "remembered_terminology",
            "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        citation = self._remembered(answer)[0]
        assert citation.detail == {
            "evidence_type": "remembered_terminology",
            "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
        }

    def test_no_evidence_when_key_absent(self):
        # No remembered_terminology key at all — the ordinary case for
        # every source with nothing taught, or a term with no synonym.
        plan = _business_plan()
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert self._remembered(answer) == []

    def test_no_evidence_when_list_is_explicitly_empty(self):
        plan = _business_plan(remembered_terminology=[])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert self._remembered(answer) == []

    def test_multiple_records_each_produce_their_own_citation(self):
        plan = _business_plan(remembered_terminology=[
            {"evidence_type": "remembered_terminology", "original_term": "clients",
             "canonical_term": "customer", "source": "user_memory"},
            {"evidence_type": "remembered_terminology", "original_term": "revenue",
             "canonical_term": "sales_amount", "source": "user_memory"},
        ])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert len(self._remembered(answer)) == 2

    def test_conversational_wording(self):
        plan = _business_plan(remembered_terminology=[{
            "evidence_type": "remembered_terminology",
            "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        citation = self._remembered(answer)[0]
        assert citation.label == 'Resolved using your remembered terminology: "clients" → "customer"'

    def test_no_governance_jargon_in_label(self):
        plan = _business_plan(remembered_terminology=[{
            "evidence_type": "remembered_terminology",
            "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        citation = self._remembered(answer)[0]
        for banned in ("governance", "auto_approved", "human_approved", "confidence", "lifecycle"):
            assert banned not in citation.label.lower()

    def test_existing_scalar_count_answer_unchanged_when_nothing_remembered(self):
        # Pins TestLiveQueryBusinessValueRendering.test_scalar_count's own
        # assertions exactly — proves this feature is purely additive.
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
        assert self._remembered(answer) == []


# ---------------------------------------------------------------------------
# Enterprise Phase 4 — generated-vocabulary explainability
# ---------------------------------------------------------------------------

class TestGeneratedVocabularyExplainability:
    def _generated(self, answer):
        return [c for c in answer.citations if c.source_type == CitationType.GENERATED_VOCABULARY]

    def test_generated_vocabulary_creates_one_evidence_citation(self):
        plan = _business_plan(generated_vocabulary_evidence=[{
            "original_term": "clients", "generated_term": "organizations",
            "source": "generated_vocabulary",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert len(self._generated(answer)) == 1

    def test_citation_preserves_original_and_generated_terms(self):
        plan = _business_plan(generated_vocabulary_evidence=[{
            "original_term": "clients", "generated_term": "organizations",
            "source": "generated_vocabulary",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        citation = self._generated(answer)[0]
        assert citation.detail == {
            "original_term": "clients", "generated_term": "organizations",
            "source": "generated_vocabulary",
        }

    def test_no_evidence_when_key_absent(self):
        plan = _business_plan()
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert self._generated(answer) == []

    def test_no_evidence_when_list_is_explicitly_empty(self):
        plan = _business_plan(generated_vocabulary_evidence=[])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        assert self._generated(answer) == []

    def test_conversational_wording_distinguishes_from_remembered_terminology(self):
        plan = _business_plan(generated_vocabulary_evidence=[{
            "original_term": "clients", "generated_term": "organizations",
            "source": "generated_vocabulary",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        citation = self._generated(answer)[0]
        assert citation.label == 'Resolved using database vocabulary: "clients" → "organizations"'

    def test_no_governance_jargon_in_label(self):
        plan = _business_plan(generated_vocabulary_evidence=[{
            "original_term": "clients", "generated_term": "organizations",
            "source": "generated_vocabulary",
        }])
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        citation = self._generated(answer)[0]
        for banned in ("governance", "auto_approved", "human_approved", "confidence", "tier"):
            assert banned not in citation.label.lower()

    def test_both_remembered_and_generated_can_coexist_additively(self):
        plan = _business_plan(
            remembered_terminology=[{
                "evidence_type": "remembered_terminology",
                "original_term": "clients", "canonical_term": "customer", "source": "user_memory",
            }],
            generated_vocabulary_evidence=[{
                "original_term": "revenue", "generated_term": "sales amount",
                "source": "generated_vocabulary",
            }],
        )
        data = _success_data(plan, rows=[{"row_count": 5}])
        answer = _build_live_query_answer(data)
        remembered = [c for c in answer.citations if c.source_type == CitationType.REMEMBERED_TERMINOLOGY]
        generated = self._generated(answer)
        assert len(remembered) == 1
        assert len(generated) == 1

    def test_existing_scalar_count_answer_unchanged_when_nothing_generated(self):
        # Pins TestLiveQueryBusinessValueRendering.test_scalar_count's own
        # assertions exactly — proves this feature is purely additive.
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
        assert self._generated(answer) == []


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
