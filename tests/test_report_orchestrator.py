"""
Tests for core.reporting.report_orchestrator — Executive Report Query
Foundation milestone, Part 9.

Unit-level: core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan
and data.query_execution_service.execute_governed_query are mocked (both
are already covered by their own dedicated test suites —
tests/test_ai_structured_kpi_plan.py and the live-execution test suites);
this file verifies the ORCHESTRATION contract: plan -> execute -> collect ->
compare -> ExecutiveReportData, and that a KPI that fails planning/execution
is skipped, never silently substituted or routed through legacy.

Run:
    python -m pytest tests/test_report_orchestrator.py -v
"""
from __future__ import annotations

from unittest.mock import patch

from core.live.query_result import QueryResult, QueryStatus
from core.reporting.kpi_spec import ComparisonRequest, KPIMeasure, KPIQuerySpec
from core.reporting.report_models import ChartDataset, KPICard
from core.reporting.report_orchestrator import execute_kpi_specs


def _plan_result(select_rows, sql="SELECT 1", aggregation="COUNT"):
    return {
        "attempted": True, "valid": True, "reason": None, "adjudication_call_count": 1,
        "selected_table_fqns": ["dbo.clients"],
        "plans": [{
            "measure_terms": ["client"], "aggregation": aggregation,
            "physical_plan": None, "query_plan": {},
            "sql_plan": {"select": select_rows}, "generated": {"sql": sql},
        }],
        "excluded_measures": [],
    }


def _disqualified_result(reason="no_candidate_evidence"):
    return {
        "attempted": True, "valid": False, "reason": reason, "adjudication_call_count": 0,
        "selected_table_fqns": [], "plans": [], "excluded_measures": [],
    }


def _query_result(rows, status=QueryStatus.SUCCESS, error=None):
    return QueryResult(
        execution_id="e1", status=status, source_id=1, executed_at="2026-08-17T00:00:00Z",
        duration_ms=5, rows=rows, row_count=len(rows),
    )


def test_valid_kpi_becomes_kpi_card():
    spec = KPIQuerySpec(
        id="student_count", title="Student Count", business_question="How many students are there?",
        entity_name="student", measures=(KPIMeasure(term="student", aggregation="COUNT"),),
    )
    select_rows = [{"table_fqn": "dbo.students", "column_name": "StudentID", "alias": "count_studentid", "aggregation": "COUNT"}]
    with (
        patch(
            "core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan",
            return_value=_plan_result(select_rows),
        ),
        patch(
            "data.query_execution_service.execute_governed_query",
            return_value=(_query_result([{"count_studentid": 250}]), []),
        ),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert len(report.kpi_cards) == 1
    assert report.kpi_cards[0].value == 250
    assert len(report.trace) == 1
    assert report.trace[0].execution_status == "executed"
    assert report.warnings == []


def test_unsafe_kpi_is_skipped_not_substituted():
    spec = KPIQuerySpec(
        id="bad_kpi", title="Mystery Metric", business_question="Something unanswerable",
        entity_name="unicorn", measures=(KPIMeasure(term="unicorn", aggregation="COUNT"),),
    )
    with patch(
        "core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan",
        return_value=_disqualified_result("no_candidate_evidence"),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert report.kpi_cards == []
    assert report.charts == []
    assert report.tables == []
    assert len(report.trace) == 1
    assert report.trace[0].execution_status == "skipped_unsafe"
    assert report.trace[0].skip_reason == "no_candidate_evidence"
    assert any("skipped" in w for w in report.warnings)


def test_failed_execution_is_recorded_not_silently_dropped():
    spec = KPIQuerySpec(
        id="k1", title="Revenue", business_question="Total revenue",
        entity_name="order", measures=(KPIMeasure(term="revenue", aggregation="SUM"),),
    )
    select_rows = [{"table_fqn": "dbo.orders", "column_name": "Revenue", "alias": "sum_revenue", "aggregation": "SUM"}]
    with (
        patch(
            "core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan",
            return_value=_plan_result(select_rows, aggregation="SUM"),
        ),
        patch(
            "data.query_execution_service.execute_governed_query",
            return_value=(_query_result([], status=QueryStatus.FAILED, error="timeout"), []),
        ),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert report.kpi_cards == []
    assert report.trace[0].execution_status == "failed"
    assert any("execution failed" in w for w in report.warnings)


def test_comparison_kpi_produces_kpi_card_with_deltas():
    spec = KPIQuerySpec(
        id="revenue_mom", title="Revenue", business_question="Revenue this month vs last month",
        entity_name="order", measures=(KPIMeasure(term="revenue", aggregation="SUM"),),
        comparison=ComparisonRequest(comparison_type="month_over_month"),
    )
    select_rows = [{"table_fqn": "dbo.orders", "column_name": "Revenue", "alias": "sum_revenue", "aggregation": "SUM"}]

    call_values = iter([1200, 1000])  # current, then previous

    def fake_execute(*args, **kwargs):
        return _query_result([{"sum_revenue": next(call_values)}]), []

    with (
        patch(
            "core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan",
            return_value=_plan_result(select_rows, aggregation="SUM"),
        ),
        patch("data.query_execution_service.execute_governed_query", side_effect=fake_execute),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert len(report.kpi_cards) == 1
    card = report.kpi_cards[0]
    assert card.value == 1200
    assert card.previous_value == 1000
    assert card.absolute_change == 200
    assert card.percent_change == 20.0
    assert card.direction == "up"
    assert card.comparison_label == "vs last month"


def test_multi_plan_spec_produces_multiple_visuals_with_distinct_ids():
    # A KPIQuerySpec whose measures split into two plans (heterogeneous
    # aggregations) must produce two distinct, traceable visuals — never
    # one merged/guessed value.
    spec = KPIQuerySpec(
        id="mix", title="Client Count and Avg Fee", business_question="q",
        entity_name="client",
        measures=(KPIMeasure(term="client", aggregation="COUNT"), KPIMeasure(term="course fee", aggregation="AVG")),
    )
    plan_result = {
        "attempted": True, "valid": True, "reason": None, "adjudication_call_count": 2,
        "selected_table_fqns": ["dbo.clients"],
        "plans": [
            {
                "measure_terms": ["client"], "aggregation": "COUNT", "physical_plan": None, "query_plan": {},
                "sql_plan": {"select": [{"table_fqn": "dbo.clients", "column_name": "ClientID", "alias": "count_clientid", "aggregation": "COUNT"}]},
                "generated": {"sql": "SELECT COUNT(*) ..."},
            },
            {
                "measure_terms": ["course fee"], "aggregation": "AVG", "physical_plan": None, "query_plan": {},
                "sql_plan": {"select": [{"table_fqn": "dbo.clients", "column_name": "CourseFee", "alias": "avg_coursefee", "aggregation": "AVG"}]},
                "generated": {"sql": "SELECT AVG(...) ..."},
            },
        ],
        "excluded_measures": [],
    }
    results = iter([
        (_query_result([{"count_clientid": 40}]), []),
        (_query_result([{"avg_coursefee": 425.5}]), []),
    ])
    with (
        patch("core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan", return_value=plan_result),
        patch("data.query_execution_service.execute_governed_query", side_effect=lambda *a, **k: next(results)),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert len(report.kpi_cards) == 2
    ids = {c.id for c in report.kpi_cards}
    assert ids == {"mix_count_0", "mix_avg_1"}
    assert len(report.trace) == 2


def test_calendar_grain_trend_ignores_forced_comparison():
    # FINAL EXECUTIVE-REPORT BLOCKER FIX milestone, Part 4 — defense-in-depth:
    # a KPIQuerySpec carrying BOTH calendar_grain (a chart-series trend) and
    # a comparison (a single current-vs-previous scalar) is structurally
    # incompatible — core.reporting.report_planner is the primary fix (it
    # never lets an AI proposal keep both), but a hand-authored spec (this
    # module's own docstring says callers may author one directly) must be
    # equally safe: the comparison is ignored and the KPI still executes as
    # a normal grouped trend, producing a chart rather than silently losing
    # both the chart AND the comparison to the "incompatible result grain"
    # skip.
    spec = KPIQuerySpec(
        id="trend_mom", title="Monthly Revenue Trend", business_question="Revenue trend by month",
        entity_name="order", measures=(KPIMeasure(term="revenue", aggregation="SUM"),),
        calendar_grain="month", comparison=ComparisonRequest(comparison_type="month_over_month"),
    )
    select_rows = [
        {"table_fqn": "dbo.orders", "column_name": "OrderMonth", "alias": "order_month", "aggregation": None, "time_grain": "month"},
        {"table_fqn": "dbo.orders", "column_name": "Revenue", "alias": "sum_revenue", "aggregation": "SUM"},
    ]
    with (
        patch(
            "core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan",
            return_value=_plan_result(select_rows, aggregation="SUM"),
        ),
        patch(
            "data.query_execution_service.execute_governed_query",
            return_value=(_query_result([{"order_month": 1, "sum_revenue": 100}]), []),
        ),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert report.kpi_cards == []
    assert len(report.charts) == 1
    assert any("comparison ignored" in w for w in report.warnings)


def test_timings_are_recorded():
    spec = KPIQuerySpec(
        id="k1", title="Client Count", business_question="q", entity_name="client",
        measures=(KPIMeasure(term="client", aggregation="COUNT"),),
    )
    select_rows = [{"table_fqn": "dbo.clients", "column_name": "ClientID", "alias": "count_clientid", "aggregation": "COUNT"}]
    with (
        patch(
            "core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan",
            return_value=_plan_result(select_rows),
        ),
        patch(
            "data.query_execution_service.execute_governed_query",
            return_value=(_query_result([{"count_clientid": 5}]), []),
        ),
    ):
        report = execute_kpi_specs(1, "u1", "Executive Overview", [spec])

    assert "planning_ms" in report.timings_ms
    assert "execution_ms" in report.timings_ms
    assert "total_ms" in report.timings_ms
