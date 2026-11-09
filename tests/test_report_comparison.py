"""
Tests for core.reporting.comparison — Executive Report Query Foundation
milestone, Part 5 (real period-over-period comparison).

Run:
    python -m pytest tests/test_report_comparison.py -v
"""
from __future__ import annotations

from datetime import date

import pytest

from core.reporting.comparison import (
    build_comparison_plan, compute_comparison, resolve_comparison_periods,
)
from core.reporting.kpi_spec import ComparisonRequest, KPIMeasure, KPIQuerySpec


def test_month_over_month_periods_are_elapsed_matched():
    current, previous = resolve_comparison_periods("month_over_month", today=date(2026, 8, 17))
    assert current == {"start": "2026-08-01", "end": "2026-08-17"}
    # Previous period is month-to-date-matched: July 1 through the 17th, not
    # the whole month of July — a fair comparison of equal-length windows.
    assert previous == {"start": "2026-07-01", "end": "2026-07-17"}


def test_year_over_year_periods_are_elapsed_matched():
    current, previous = resolve_comparison_periods("year_over_year", today=date(2026, 8, 17))
    assert current == {"start": "2026-01-01", "end": "2026-08-17"}
    assert previous == {"start": "2025-01-01", "end": "2025-08-17"}


def test_quarter_over_quarter_periods():
    current, previous = resolve_comparison_periods("quarter_over_quarter", today=date(2026, 8, 17))
    assert current == {"start": "2026-07-01", "end": "2026-08-17"}
    assert previous == {"start": "2026-04-01", "end": "2026-05-18"}


def test_custom_requires_both_periods():
    with pytest.raises(ValueError):
        resolve_comparison_periods("custom", current_period={"start": "2026-01-01", "end": "2026-01-31"})


def test_build_comparison_plan_derives_two_independent_specs():
    base = KPIQuerySpec(
        id="revenue", title="Total Revenue", business_question="Total revenue this month vs last month",
        entity_name="order", measures=(KPIMeasure(term="revenue", aggregation="SUM"),),
        comparison=ComparisonRequest(comparison_type="month_over_month"),
    )
    plan = build_comparison_plan(base, today=date(2026, 8, 17))
    assert plan.current_spec.date_range == {"start": "2026-08-01", "end": "2026-08-17"}
    assert plan.previous_spec.date_range == {"start": "2026-07-01", "end": "2026-07-17"}
    assert plan.current_spec.comparison is None
    assert plan.previous_spec.comparison is None
    assert plan.current_spec.measures == base.measures
    assert plan.current_spec.id != plan.previous_spec.id


def test_compute_comparison_normal_increase():
    result = compute_comparison("Revenue", 1200.0, 1000.0, {"start": "a"}, {"start": "b"})
    assert result.absolute_change == 200.0
    assert result.percentage_change == 20.0
    assert result.direction == "up"
    assert result.warning is None


def test_compute_comparison_decrease():
    result = compute_comparison("Revenue", 800.0, 1000.0, {}, {})
    assert result.absolute_change == -200.0
    assert result.percentage_change == -20.0
    assert result.direction == "down"


def test_compute_comparison_equal_values_is_flat():
    result = compute_comparison("Revenue", 500.0, 500.0, {}, {})
    assert result.absolute_change == 0
    assert result.percentage_change == 0
    assert result.direction == "flat"


def test_compute_comparison_previous_zero_current_positive_is_undefined_percentage():
    result = compute_comparison("New Signups", 50.0, 0.0, {}, {})
    assert result.absolute_change == 50.0
    assert result.percentage_change is None
    assert result.direction == "up"
    assert result.warning is not None


def test_compute_comparison_both_zero_is_flat_zero_percent():
    result = compute_comparison("New Signups", 0.0, 0.0, {}, {})
    assert result.absolute_change == 0.0
    assert result.percentage_change == 0.0
    assert result.direction == "flat"


def test_compute_comparison_missing_value_is_unknown():
    result = compute_comparison("Revenue", None, 1000.0, {}, {})
    assert result.percentage_change is None
    assert result.direction == "unknown"
    assert result.warning is not None

    result2 = compute_comparison("Revenue", 1000.0, None, {}, {})
    assert result2.direction == "unknown"
