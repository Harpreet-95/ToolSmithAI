"""
Tests for Day 4, Capability 3 — Automatic Charts:
core.answering.result_formatter._build_chart_spec.

Pure unit-level: no DB, no fixtures — exercises the deterministic
shape/plan/rows -> chart-type selection directly, mirroring
tests/test_insight_service.py's style for a new, isolated piece of logic.

Run from the project root:
    python -m pytest tests/test_chart_spec.py -v
"""
from __future__ import annotations

from core.answering.result_formatter import _build_chart_spec


def _grouped_plan(*, time_grain=None, aggregation="COUNT", measure_label="Students"):
    return {
        "aggregation": aggregation,
        "measure_label": measure_label,
        "entity_label": "students",
        "group_by": [{"table_fqn": "dbo.students", "column_name": "year", "alias": "year", **({"time_grain": time_grain} if time_grain else {})}],
        "select": [
            {"table_fqn": "dbo.students", "column_name": "year", "alias": "year"},
            {"table_fqn": "dbo.students", "column_name": "id", "alias": "count_id", "aggregation": aggregation},
        ],
    }


def _ranked_plan():
    return {
        "aggregation": "COUNT",
        "measure_label": "Students",
        "entity_label": "students",
        "group_by": [{"table_fqn": "dbo.classes", "column_name": "class_name", "alias": "class_name"}],
        "order_intent": {"limit": 5, "direction": "DESC"},
        "select": [
            {"table_fqn": "dbo.classes", "column_name": "class_name", "alias": "class_name"},
            {"table_fqn": "dbo.classes", "column_name": "id", "alias": "count_id", "aggregation": "COUNT"},
        ],
    }


def test_time_series_group_by_year_returns_line_chart():
    plan = _grouped_plan(time_grain="year")
    rows = [{"year": 2021, "count_id": 100}, {"year": 2022, "count_id": 140}, {"year": 2023, "count_id": 180}]
    chart = _build_chart_spec("grouped", plan, rows)
    assert chart == {
        "chart_type": "line",
        "labels": ["2021", "2022", "2023"],
        "series": [{"name": "Students", "data": [100, 140, 180]}],
    }


def test_small_categorical_count_group_returns_donut():
    plan = _grouped_plan(aggregation="COUNT")
    rows = [
        {"year": "Active", "count_id": 40}, {"year": "Stalled", "count_id": 5},
        {"year": "Graduated", "count_id": 60}, {"year": "Not Started", "count_id": 12},
    ]
    chart = _build_chart_spec("grouped", plan, rows)
    assert chart["chart_type"] == "donut"
    assert chart["labels"] == ["Active", "Stalled", "Graduated", "Not Started"]
    assert chart["series"][0]["data"] == [40, 5, 60, 12]


def test_categorical_sum_group_returns_bar_not_donut():
    plan = _grouped_plan(aggregation="SUM", measure_label="Revenue")
    rows = [{"year": "North", "count_id": 500}, {"year": "South", "count_id": 300}]
    chart = _build_chart_spec("grouped", plan, rows)
    assert chart["chart_type"] == "bar"


def test_categorical_count_group_with_too_many_categories_returns_bar_not_donut():
    plan = _grouped_plan(aggregation="COUNT")
    rows = [{"year": f"Cat{i}", "count_id": i} for i in range(8)]  # > _DONUT_MAX_CATEGORIES
    chart = _build_chart_spec("grouped", plan, rows)
    assert chart["chart_type"] == "bar"


def test_ranked_shape_always_returns_bar_horizontal():
    plan = _ranked_plan()
    rows = [{"class_name": "CS101", "count_id": 90}, {"class_name": "Intro to Data Science", "count_id": 75}]
    chart = _build_chart_spec("ranked", plan, rows)
    assert chart["chart_type"] == "bar_horizontal"
    assert chart["labels"] == ["CS101", "Intro to Data Science"]


def test_ranked_shape_returns_bar_horizontal_even_with_few_categories():
    # Day 4 rule: ranked always means bar_horizontal, never donut, regardless
    # of how few categories there are (unlike plain "grouped").
    plan = _ranked_plan()
    rows = [{"class_name": "CS101", "count_id": 90}]
    chart = _build_chart_spec("ranked", plan, rows)
    assert chart["chart_type"] == "bar_horizontal"


def test_scalar_shapes_never_produce_a_chart():
    plan = {"aggregation": "COUNT", "group_by": [], "select": []}
    for shape in ("scalar_count", "scalar_count_distinct", "scalar_sum", "scalar_avg", "scalar_minmax", "null_scalar", "empty"):
        assert _build_chart_spec(shape, plan, [{"count_id": 42}]) is None


def test_tabular_shapes_never_produce_a_chart():
    plan = _grouped_plan()
    rows = [{"year": 2021, "count_id": 100}]
    assert _build_chart_spec("tabular", plan, rows) is None
    assert _build_chart_spec("tabular_fallback", plan, rows) is None


def test_multi_dimension_group_by_returns_none():
    plan = _grouped_plan()
    plan["group_by"] = [
        {"table_fqn": "dbo.students", "column_name": "year", "alias": "year"},
        {"table_fqn": "dbo.students", "column_name": "status", "alias": "status"},
    ]
    rows = [{"year": 2021, "status": "Active", "count_id": 10}]
    assert _build_chart_spec("grouped", plan, rows) is None


def test_no_group_by_returns_none():
    plan = {"aggregation": "COUNT", "group_by": [], "select": [{"alias": "count_id", "aggregation": "COUNT"}]}
    assert _build_chart_spec("grouped", plan, [{"count_id": 42}]) is None


def test_no_measure_aggregation_in_select_returns_none():
    plan = _grouped_plan()
    plan["select"] = [{"table_fqn": "dbo.students", "column_name": "year", "alias": "year"}]  # no aggregation row
    rows = [{"year": 2021, "count_id": 100}]
    assert _build_chart_spec("grouped", plan, rows) is None


def test_empty_rows_returns_none():
    plan = _grouped_plan()
    assert _build_chart_spec("grouped", plan, []) is None


def test_too_many_categories_returns_none():
    plan = _grouped_plan()
    rows = [{"year": 2000 + i, "count_id": i} for i in range(30)]  # > _MAX_CHART_CATEGORIES
    assert _build_chart_spec("grouped", plan, rows) is None


def test_structural_mismatch_between_alias_and_row_keys_returns_none():
    plan = _grouped_plan()
    rows = [{"unexpected_key": 2021, "count_id": 100}]  # missing the "year" dimension alias
    assert _build_chart_spec("grouped", plan, rows) is None


def test_non_numeric_measure_value_returns_none():
    plan = _grouped_plan()
    rows = [{"year": 2021, "count_id": "not-a-number"}]
    assert _build_chart_spec("grouped", plan, rows) is None


def test_boolean_measure_value_returns_none():
    # bool is a subclass of int in Python — must be explicitly excluded so a
    # boolean-typed column is never silently plotted as 0/1.
    plan = _grouped_plan()
    rows = [{"year": 2021, "count_id": True}]
    assert _build_chart_spec("grouped", plan, rows) is None


def test_never_invents_values_chart_matches_rows_exactly():
    plan = _grouped_plan(time_grain="year")
    rows = [{"year": 2019, "count_id": 7}, {"year": 2020, "count_id": 0}, {"year": 2021, "count_id": 250}]
    chart = _build_chart_spec("grouped", plan, rows)
    assert chart["labels"] == ["2019", "2020", "2021"]
    assert chart["series"][0]["data"] == [7, 0, 250]
