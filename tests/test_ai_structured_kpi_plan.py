"""
Tests for core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan
— Executive Report Query Foundation milestone, Parts 2/3/4 (multi-measure,
non-COUNT time-series aggregation, calendar + non-calendar dimensions).

Mirrors tests/test_ai_physical_planner.py's own mocking style: only the LLM
boundary (core.semantic.candidate_adjudicator.adjudicate_candidates) is
mocked; table/column resolution, entity-key selection, deterministic
validation, and SQL generation all run for real against a SQLite fixture.

Run:
    python -m pytest tests/test_ai_structured_kpi_plan.py -v
"""
from __future__ import annotations

from unittest.mock import patch

from core.orchestrator.ai_physical_planning import attempt_structured_kpi_plan
from tests.ai_physical_planner_fixtures import _mock_adjudication
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_fk


def test_kpi_measure_count_out_of_scope_is_disqualified():
    result = attempt_structured_kpi_plan(
        1, "u1", entity_name="client", business_question="q",
        measures=[{"term": f"m{i}", "aggregation": "SUM"} for i in range(6)],
    )
    assert result["attempted"] is False
    assert result["reason"] == "kpi_measure_count_out_of_scope"


def test_invalid_calendar_grain_is_disqualified():
    result = attempt_structured_kpi_plan(
        1, "u1", entity_name="client", business_question="q",
        measures=[{"term": "x", "aggregation": "COUNT"}], calendar_grain="fortnight",
    )
    assert result["attempted"] is False
    assert result["reason"] == "invalid_calendar_grain"


def test_single_count_measure_no_dimensions_produces_valid_plan(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "ClientID", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.clients"),
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="client", business_question="How many clients are there?",
            measures=[{"term": "client", "aggregation": "COUNT"}],
        )

    assert result["valid"] is True, result["reason"]
    assert len(result["plans"]) == 1
    plan = result["plans"][0]
    assert plan["aggregation"] == "COUNT"
    assert "COUNT" in plan["generated"]["sql"]


def test_same_aggregation_multi_measure_combines_into_one_plan(tmp_path, monkeypatch):
    # Part 2 — "total revenue and total refunds by month": two SUM measures
    # on the SAME table share one SELECT/one plan (no fan-out risk, same
    # aggregation verb, so the coexistence proof passes and there is only
    # one combined query).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.orders", "Revenue", data_type="DECIMAL")
    _add_column(db, "dbo.orders", "Refund", data_type="DECIMAL")

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.orders"),
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="order", business_question="Total revenue and total refunds",
            measures=[
                {"term": "revenue", "aggregation": "SUM"},
                {"term": "refund", "aggregation": "SUM"},
            ],
        )

    assert result["valid"] is True, result["reason"]
    assert len(result["plans"]) == 1
    select = result["plans"][0]["sql_plan"]["select"]
    selected_columns = {r["column_name"] for r in select if r.get("aggregation") == "SUM"}
    assert {"Revenue", "Refund"} <= selected_columns
    assert result["excluded_measures"] == []


def test_different_aggregation_multi_measure_splits_into_two_plans(tmp_path, monkeypatch):
    # Part 2 — "student count and average course fee": COUNT and AVG cannot
    # share one SELECT (data.sql_planning_service._select_entries applies
    # ONE aggregation verb per entries list) so this must produce two
    # independently-validated plans, never a fabricated mixed query.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "ClientID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.clients", "CourseFee", data_type="DECIMAL")

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.clients"),
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="client", business_question="Client count and average course fee",
            measures=[
                {"term": "client", "aggregation": "COUNT"},
                {"term": "course fee", "aggregation": "AVG"},
            ],
        )

    assert result["valid"] is True, result["reason"]
    assert len(result["plans"]) == 2
    aggregations = {p["aggregation"] for p in result["plans"]}
    assert aggregations == {"COUNT", "AVG"}


def test_calendar_and_non_calendar_dimensions_combine(tmp_path, monkeypatch):
    # Part 4 — "average course fee by program and month": calendar grain
    # (month, on the entity's own table) and a non-calendar dimension
    # (program, cross-table) coexist in ONE plan — attempt_ai_physical_plan's
    # interactive path deliberately disqualifies this combination; this
    # structured entry point does not.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.clients")
    _add_column(db, "dbo.clients", "ClientID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.clients", "CourseFee", data_type="DECIMAL")
    _add_column(db, "dbo.clients", "EnrollDate", data_type="DATETIME", is_date=True)
    _add_column(db, "dbo.clients", "ProgramID", data_type="INTEGER")

    _add_table(db, "dbo.programs")
    _add_column(db, "dbo.programs", "ProgramID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.programs", "ProgramName", data_type="VARCHAR", is_dimension=True)

    _add_fk(db, "dbo.clients", "ProgramID", "dbo.programs", "ProgramID", status="AUTO")

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        side_effect=[_mock_adjudication("dbo.clients"), _mock_adjudication("dbo.programs")],
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="client", business_question="Average course fee by program and month",
            measures=[{"term": "course fee", "aggregation": "AVG"}],
            calendar_grain="month", dimension_terms=["program"],
        )

    assert result["valid"] is True, result["reason"]
    plan = result["plans"][0]
    assert len(plan["sql_plan"]["group_by"]) == 2
    assert "AVG" in plan["generated"]["sql"]
    assert plan["physical_plan"].order_by is not None
    assert plan["physical_plan"].order_by["direction"] == "ASC"


def test_non_count_aggregation_by_calendar_grain_alone(tmp_path, monkeypatch):
    # Part 3 — "total revenue by month": a non-COUNT aggregation paired with
    # a bare calendar grain, with a chronological ORDER BY.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.orders", "Revenue", data_type="DECIMAL")
    _add_column(db, "dbo.orders", "OrderDate", data_type="DATETIME", is_date=True)

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.orders"),
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="order", business_question="Total revenue by month",
            measures=[{"term": "revenue", "aggregation": "SUM"}], calendar_grain="month",
        )

    assert result["valid"] is True, result["reason"]
    plan = result["plans"][0]
    assert "SUM" in plan["generated"]["sql"]
    assert plan["physical_plan"].group_by
    assert plan["physical_plan"].order_by["direction"] == "ASC"


def test_measure_exposed_to_fanout_is_excluded_not_silently_inflated(tmp_path, monkeypatch):
    # Part 2 — a SUM measure whose own table sits on the "one" side of a
    # one-to-many join introduced by a dimension must never be silently
    # summed across duplicated rows: it is excluded, not force-included.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.programs")
    _add_column(db, "dbo.programs", "ProgramID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.programs", "Budget", data_type="DECIMAL")

    _add_table(db, "dbo.departments")
    _add_column(db, "dbo.departments", "DeptID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.departments", "DeptName", data_type="VARCHAR", is_dimension=True)
    _add_column(db, "dbo.departments", "ProgramID", data_type="INTEGER")

    _add_fk(db, "dbo.departments", "ProgramID", "dbo.programs", "ProgramID", status="AUTO")

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        side_effect=[_mock_adjudication("dbo.programs"), _mock_adjudication("dbo.departments")],
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="program", business_question="Total budget by department",
            measures=[{"term": "budget", "aggregation": "SUM"}], dimension_terms=["department"],
        )

    assert result["valid"] is False
    assert result["reason"] == "all_kpi_measures_fanout_unsafe"
    assert result["excluded_measures"] == [{"term": "budget", "reason": "aggregate_fanout_safety"}]
    assert result["plans"] == []


def test_partial_fanout_exclusion_still_returns_safe_plan(tmp_path, monkeypatch):
    # Same fan-out-risk join as above, but paired with a COUNT measure that
    # DOES survive (an entity key is available to de-duplicate with) —
    # confirms a partial KPI result (one excluded, one plan) rather than an
    # all-or-nothing refusal.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.programs")
    _add_column(db, "dbo.programs", "ProgramID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.programs", "Budget", data_type="DECIMAL")

    _add_table(db, "dbo.departments")
    _add_column(db, "dbo.departments", "DeptID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.departments", "DeptName", data_type="VARCHAR", is_dimension=True)
    _add_column(db, "dbo.departments", "ProgramID", data_type="INTEGER")

    _add_fk(db, "dbo.departments", "ProgramID", "dbo.programs", "ProgramID", status="AUTO")

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        side_effect=[_mock_adjudication("dbo.programs"), _mock_adjudication("dbo.departments")],
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="program", business_question="Program count and total budget by department",
            measures=[
                {"term": "program", "aggregation": "COUNT"},
                {"term": "budget", "aggregation": "SUM"},
            ],
            dimension_terms=["department"],
        )

    assert result["valid"] is True, result["reason"]
    assert len(result["plans"]) == 1
    assert result["plans"][0]["aggregation"] == "COUNT"
    assert result["excluded_measures"] == [{"term": "budget", "reason": "aggregate_fanout_safety"}]
