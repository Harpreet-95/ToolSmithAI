"""
Tests for the Enterprise AI Analyst Agent's Result Validator (Milestone M-27).

core.answering.result_validator.validate_execution_result is a pure,
structural transform over the same combined execution_result dict
(QueryResult.to_dict() + an attached "business_plan") that
core.answering.result_formatter.classify_result_shape()/build_business_answer()
already consume — this file hand-builds that shape exactly like
tests/test_answering.py's own _business_plan()/_success_data() fixtures, so
no database or live pipeline is needed to test it.

Run from the project root:
    python -m pytest tests/test_result_validator.py -v
"""
from __future__ import annotations

from core.answering.result_validator import ResultValidation, validate_execution_result


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


def _execution_result(business_plan, **overrides):
    base = {
        "execution_id": "exec-1", "status": "success", "duration_ms": 12,
        "row_count": 1, "columns": [], "rows": [], "truncated": False,
        "business_plan": business_plan,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Executed successfully
# ---------------------------------------------------------------------------

def test_failed_status_blocks_immediately():
    data = _execution_result(_business_plan(), status="failed", error="connection lost", row_count=0)
    v = validate_execution_result(data)
    assert v.valid is False
    assert v.checks["executed_successfully"] is False
    assert any("connection lost" in r for r in v.blocking_reasons)


def test_empty_execution_result_blocks():
    v = validate_execution_result({})
    assert v.valid is False
    assert v.checks["executed_successfully"] is False


def test_successful_scalar_count_is_valid():
    plan = _business_plan(aggregation="COUNT", select=[
        {"table_fqn": "dbo.Client", "column_name": None, "alias": "row_count", "aggregation": "COUNT"},
    ])
    data = _execution_result(
        plan, row_count=1, columns=[{"name": "row_count", "pii": False}],
        rows=[{"row_count": 42}],
    )
    v = validate_execution_result(data)
    assert v.valid is True
    assert v.result_shape == "scalar_count"
    assert v.blocking_reasons == []


# ---------------------------------------------------------------------------
# Expected columns present
# ---------------------------------------------------------------------------

def test_missing_expected_column_blocks():
    plan = _business_plan(select=[
        {"table_fqn": "dbo.Client", "column_name": "name", "alias": "name", "aggregation": None},
    ])
    data = _execution_result(plan, columns=[{"name": "other_column", "pii": False}], rows=[{"other_column": "x"}])
    v = validate_execution_result(data)
    assert v.valid is False
    assert v.checks["expected_columns_present"] is False
    assert any("name" in r for r in v.blocking_reasons)


def test_matching_expected_columns_passes():
    plan = _business_plan(select=[
        {"table_fqn": "dbo.Client", "column_name": "name", "alias": "name", "aggregation": None},
    ])
    data = _execution_result(plan, columns=[{"name": "name", "pii": False}], rows=[{"name": "Acme"}])
    v = validate_execution_result(data)
    assert v.checks["expected_columns_present"] is True


# ---------------------------------------------------------------------------
# Row shape usable
# ---------------------------------------------------------------------------

def test_rows_without_column_metadata_blocks():
    data = _execution_result(_business_plan(), row_count=3, columns=[], rows=[{"x": 1}, {"x": 2}, {"x": 3}])
    v = validate_execution_result(data)
    assert v.valid is False
    assert v.checks["row_shape_usable"] is False


def test_zero_rows_with_no_columns_is_fine():
    data = _execution_result(_business_plan(), row_count=0, columns=[], rows=[])
    v = validate_execution_result(data)
    assert v.checks["row_shape_usable"] is True


# ---------------------------------------------------------------------------
# Grouping / output grain plausible
# ---------------------------------------------------------------------------

def test_grouped_result_missing_grain_column_blocks():
    plan = _business_plan(group_by=[{"table_fqn": "dbo.Client", "column_name": "region"}])
    data = _execution_result(
        plan, row_count=2, columns=[{"name": "total", "pii": False}],
        rows=[{"total": 10}, {"total": 20}],
    )
    v = validate_execution_result(data)
    assert v.valid is False
    assert v.checks["grain_plausible"] is False


def test_grouped_result_with_grain_column_passes():
    plan = _business_plan(group_by=[{"table_fqn": "dbo.Client", "column_name": "region"}])
    data = _execution_result(
        plan, row_count=2, columns=[{"name": "region", "pii": False}, {"name": "total", "pii": False}],
        rows=[{"region": "West", "total": 10}, {"region": "East", "total": 20}],
    )
    v = validate_execution_result(data)
    assert v.checks["grain_plausible"] is True


def test_no_group_by_never_blocks_on_grain():
    data = _execution_result(_business_plan(), row_count=0, rows=[])
    v = validate_execution_result(data)
    assert v.checks["grain_plausible"] is True


def test_grouped_time_series_year_alias_passes():
    # data.sql_planning_service._grain_alias("StartDate", "year") -> "start_year";
    # the returned row only ever contains that alias, never the logical
    # "StartDate" column name — the validator must check the alias the plan
    # itself recorded, not the source column.
    plan = _business_plan(group_by=[
        {"table_fqn": "dbo.Student", "column_name": "StartDate", "time_grain": "year", "alias": "start_year"},
    ])
    data = _execution_result(
        plan, row_count=2, columns=[{"name": "start_year", "pii": False}, {"name": "count_StudentID", "pii": False}],
        rows=[{"start_year": 2012, "count_StudentID": 140}, {"start_year": 2013, "count_StudentID": 244}],
    )
    v = validate_execution_result(data)
    assert v.checks["grain_plausible"] is True
    assert v.valid is True


def test_grouped_time_series_month_and_quarter_alias_pass():
    for grain, alias in (("month", "start_month"), ("quarter", "start_quarter"),
                          ("week", "start_week"), ("day", "start_day")):
        plan = _business_plan(group_by=[
            {"table_fqn": "dbo.Student", "column_name": "StartDate", "time_grain": grain, "alias": alias},
        ])
        data = _execution_result(
            plan, row_count=1, columns=[{"name": alias, "pii": False}],
            rows=[{alias: "2026-01"}],
        )
        v = validate_execution_result(data)
        assert v.checks["grain_plausible"] is True, f"grain={grain} alias={alias}"


def test_grouped_time_series_genuinely_missing_output_still_fails():
    # The plan recorded a grain alias, but the returned rows don't actually
    # contain it (or anything else usable as the grouping key) — a real gap,
    # not an alias-naming mismatch, and must still block.
    plan = _business_plan(group_by=[
        {"table_fqn": "dbo.Student", "column_name": "StartDate", "time_grain": "year", "alias": "start_year"},
    ])
    data = _execution_result(
        plan, row_count=1, columns=[{"name": "count_StudentID", "pii": False}],
        rows=[{"count_StudentID": 140}],
    )
    v = validate_execution_result(data)
    assert v.checks["grain_plausible"] is False
    assert v.valid is False
    assert any("start_year" in r for r in v.blocking_reasons)


# ---------------------------------------------------------------------------
# Aggregation matches the semantic plan (warning only, not blocking)
# ---------------------------------------------------------------------------

def test_aggregation_planned_but_tabular_shape_warns_not_blocks():
    # Hand-construct an inconsistent case: aggregation recorded, but the
    # result shape (multi-row, non-scalar, no group_by) doesn't match it.
    plan = _business_plan(aggregation="SUM", select=[
        {"table_fqn": "dbo.Client", "column_name": "amount", "alias": "sum_amount", "aggregation": "SUM"},
    ])
    data = _execution_result(
        plan, row_count=2, columns=[{"name": "sum_amount", "pii": False}],
        rows=[{"sum_amount": 1}, {"sum_amount": 2}],
    )
    v = validate_execution_result(data)
    assert v.checks["aggregation_shape_consistent"] is False
    assert v.valid is True  # warning only
    assert any(w["type"] == "aggregation_shape_mismatch" for w in v.warnings)


def test_aggregation_matching_scalar_shape_no_warning():
    plan = _business_plan(aggregation="SUM", select=[
        {"table_fqn": "dbo.Client", "column_name": "amount", "alias": "sum_amount", "aggregation": "SUM"},
    ])
    data = _execution_result(plan, row_count=1, columns=[{"name": "sum_amount", "pii": False}], rows=[{"sum_amount": 500}])
    v = validate_execution_result(data)
    assert v.checks["aggregation_shape_consistent"] is True
    assert not any(w["type"] == "aggregation_shape_mismatch" for w in v.warnings)


# ---------------------------------------------------------------------------
# Requested date range represented
# ---------------------------------------------------------------------------

def test_date_context_without_matching_where_warns():
    plan = _business_plan(date_context={"label": "this month", "start": "2026-07-01", "end": "2026-07-31"}, where=[])
    data = _execution_result(plan, row_count=1, columns=[{"name": "name", "pii": False}], rows=[{"name": "x"}])
    v = validate_execution_result(data)
    assert v.checks["date_range_represented"] is False
    assert v.valid is True  # warning only
    assert any(w["type"] == "date_range_not_represented" for w in v.warnings)


def test_date_context_with_matching_where_passes():
    plan = _business_plan(
        date_context={"label": "this month", "start": "2026-07-01", "end": "2026-07-31"},
        where=[{"table_fqn": "dbo.Client", "column_name": "created_at", "operator": "BETWEEN",
                "value": ["2026-07-01", "2026-07-31"]}],
    )
    data = _execution_result(plan, row_count=1, columns=[{"name": "name", "pii": False}], rows=[{"name": "x"}])
    v = validate_execution_result(data)
    assert v.checks["date_range_represented"] is True


def test_no_date_context_trivially_passes():
    data = _execution_result(_business_plan(), row_count=0, rows=[])
    v = validate_execution_result(data)
    assert v.checks["date_range_represented"] is True


# ---------------------------------------------------------------------------
# Ranking/order exists when requested
# ---------------------------------------------------------------------------

def test_ranking_requested_but_not_resolved_warns():
    plan = _business_plan(order_intent={"direction": "DESC", "limit": 10}, order_by=[])
    data = _execution_result(plan, row_count=1, columns=[{"name": "name", "pii": False}], rows=[{"name": "x"}])
    v = validate_execution_result(data)
    assert v.checks["ranking_present_when_requested"] is False
    assert v.valid is True  # warning only
    assert any(w["type"] == "ranking_not_applied" for w in v.warnings)


def test_ranking_requested_and_resolved_passes():
    plan = _business_plan(
        order_intent={"direction": "DESC", "limit": 10},
        order_by=[{"table_fqn": "dbo.Client", "column_name": "amount", "direction": "DESC"}],
    )
    data = _execution_result(plan, row_count=1, columns=[{"name": "name", "pii": False}], rows=[{"name": "x"}])
    v = validate_execution_result(data)
    assert v.checks["ranking_present_when_requested"] is True


# ---------------------------------------------------------------------------
# Result is not empty without explanation
# ---------------------------------------------------------------------------

def test_empty_result_with_no_filters_warns():
    data = _execution_result(_business_plan(where=[]), row_count=0, rows=[], columns=[])
    v = validate_execution_result(data)
    assert v.valid is True  # empty is never itself a hard block
    assert any(w["type"] == "unexplained_empty_result" for w in v.warnings)


def test_empty_result_with_filters_applied_no_warning():
    plan = _business_plan(where=[{"table_fqn": "dbo.Client", "column_name": "status",
                                   "operator": "=", "value": "Closed"}])
    data = _execution_result(plan, row_count=0, rows=[], columns=[])
    v = validate_execution_result(data)
    assert v.valid is True
    assert not any(w["type"] == "unexplained_empty_result" for w in v.warnings)


# ---------------------------------------------------------------------------
# Obvious join multiplication signal (where possible, via optional sql_plan)
# ---------------------------------------------------------------------------

def test_high_fanout_join_with_sum_aggregation_warns():
    plan = _business_plan(aggregation="SUM", distinct=False)
    data = _execution_result(plan, row_count=1, columns=[{"name": "sum_amount", "pii": False}], rows=[{"sum_amount": 999}])
    sql_plan = {"joins": [{"left_table": "dbo.Client", "right_table": "dbo.Orders", "fanout_risk": "HIGH"}]}
    v = validate_execution_result(data, sql_plan=sql_plan)
    assert v.checks["no_join_multiplication_signal"] is False
    assert v.valid is True  # warning only
    assert any(w["type"] == "possible_join_multiplication" for w in v.warnings)


def test_high_fanout_join_with_distinct_does_not_warn():
    plan = _business_plan(aggregation="COUNT", distinct=True)
    data = _execution_result(plan, row_count=1, columns=[{"name": "row_count", "pii": False}], rows=[{"row_count": 5}])
    sql_plan = {"joins": [{"left_table": "dbo.Client", "right_table": "dbo.Orders", "fanout_risk": "HIGH"}]}
    v = validate_execution_result(data, sql_plan=sql_plan)
    assert v.checks["no_join_multiplication_signal"] is True


def test_no_sql_plan_supplied_skips_join_signal_gracefully():
    plan = _business_plan(aggregation="SUM")
    data = _execution_result(plan, row_count=1, columns=[{"name": "sum_amount", "pii": False}], rows=[{"sum_amount": 1}])
    v = validate_execution_result(data)  # no sql_plan kwarg at all
    assert v.checks["no_join_multiplication_signal"] is True


def test_low_fanout_join_does_not_warn():
    plan = _business_plan(aggregation="SUM", distinct=False)
    data = _execution_result(plan, row_count=1, columns=[{"name": "sum_amount", "pii": False}], rows=[{"sum_amount": 1}])
    sql_plan = {"joins": [{"left_table": "dbo.Client", "right_table": "dbo.Orders", "fanout_risk": "LOW"}]}
    v = validate_execution_result(data, sql_plan=sql_plan)
    assert v.checks["no_join_multiplication_signal"] is True


# ---------------------------------------------------------------------------
# Comparison periods represented (no-op until period-over-period planning exists)
# ---------------------------------------------------------------------------

def test_comparison_periods_absent_trivially_passes():
    data = _execution_result(_business_plan(), row_count=1, rows=[{"x": 1}], columns=[{"name": "x", "pii": False}])
    v = validate_execution_result(data)
    assert v.checks["comparison_periods_represented"] is True


# ---------------------------------------------------------------------------
# Overall dataclass shape / no chain-of-thought leakage
# ---------------------------------------------------------------------------

def test_result_validation_is_frozen_dataclass_with_expected_fields():
    data = _execution_result(_business_plan(), row_count=0, rows=[], columns=[])
    v = validate_execution_result(data)
    assert isinstance(v, ResultValidation)
    assert hasattr(v, "valid") and hasattr(v, "result_shape")
    assert hasattr(v, "checks") and hasattr(v, "warnings") and hasattr(v, "blocking_reasons")
