"""
Executive Report Query Foundation milestone, Part 5 — real period-over-period
comparison.

Never implemented as a text annotation over a single query: a ComparisonPlan
is TWO independently-planned, independently-validated, independently-
executed KPIQuerySpecs (current period / previous period), combined by
deterministic arithmetic only. The LLM is never asked to compute the
comparison numbers — see compute_comparison below.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Literal

from core.reporting.kpi_spec import ComparisonRequest, KPIQuerySpec

Direction = Literal["up", "down", "flat", "unknown"]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _quarter_start(d: date) -> date:
    quarter_month = ((d.month - 1) // 3) * 3 + 1
    return date(d.year, quarter_month, 1)


def resolve_comparison_periods(
    comparison_type: str,
    current_period: dict | None = None,
    previous_period: dict | None = None,
    *,
    today: date | None = None,
) -> tuple[dict, dict]:
    """Resolves a ComparisonRequest into concrete {"start","end"} date-range
    dicts for the current and previous period. "custom" requires both
    periods to already be supplied (never guessed). The three named types
    compare "period-to-date" against the SAME elapsed portion of the prior
    period (month-to-date vs month-to-date last month, etc.) — a fair,
    like-for-like window, not a partial period against a full one."""
    today = today or date.today()
    iso = "%Y-%m-%d"

    if comparison_type == "custom":
        if not current_period or not previous_period:
            raise ValueError("custom comparison_type requires both current_period and previous_period")
        return current_period, previous_period

    if comparison_type == "month_over_month":
        cur_start = _month_start(today)
        prev_start = _add_months(cur_start, -1)
        prev_end = min(_add_months(prev_start, 1) - timedelta(days=1), prev_start + (today - cur_start))
    elif comparison_type == "quarter_over_quarter":
        cur_start = _quarter_start(today)
        prev_start = _add_months(cur_start, -3)
        prev_end = min(_add_months(prev_start, 3) - timedelta(days=1), prev_start + (today - cur_start))
    elif comparison_type == "year_over_year":
        cur_start = date(today.year, 1, 1)
        prev_start = date(today.year - 1, 1, 1)
        prev_end = prev_start + (today - cur_start)
    else:
        raise ValueError(f"unknown comparison_type: {comparison_type!r}")

    return (
        {"start": cur_start.strftime(iso), "end": today.strftime(iso)},
        {"start": prev_start.strftime(iso), "end": prev_end.strftime(iso)},
    )


@dataclass(frozen=True)
class ComparisonPlan:
    metric: str
    current_spec: KPIQuerySpec
    previous_spec: KPIQuerySpec
    comparison_type: str
    current_period: dict
    previous_period: dict


def build_comparison_plan(base_spec: KPIQuerySpec, *, today: date | None = None) -> ComparisonPlan:
    """Derives a ComparisonPlan from a KPIQuerySpec carrying a
    ComparisonRequest (base_spec.comparison). The current/previous specs are
    the SAME KPI (same entity/measures/dimensions) with only date_range
    overridden — each independently goes through
    core.orchestrator.ai_physical_planning.attempt_structured_kpi_plan, never
    a combined/special comparison query shape."""
    comparison = base_spec.comparison
    if comparison is None:
        raise ValueError("base_spec has no ComparisonRequest")

    current_period, previous_period = resolve_comparison_periods(
        comparison.comparison_type, comparison.current_period, comparison.previous_period, today=today,
    )
    current_spec = replace(base_spec, id=f"{base_spec.id}::current", date_range=current_period, comparison=None)
    previous_spec = replace(
        base_spec, id=f"{base_spec.id}::previous", date_range=previous_period, comparison=None,
    )
    return ComparisonPlan(
        metric=base_spec.title, current_spec=current_spec, previous_spec=previous_spec,
        comparison_type=comparison.comparison_type, current_period=current_period, previous_period=previous_period,
    )


@dataclass(frozen=True)
class ComparisonResult:
    metric: str
    current_value: float | None
    previous_value: float | None
    absolute_change: float | None
    percentage_change: float | None
    direction: Direction
    current_period: dict
    previous_period: dict
    warning: str | None = None


def compute_comparison(
    metric: str, current_value: float | None, previous_value: float | None,
    current_period: dict, previous_period: dict,
) -> ComparisonResult:
    """Deterministic comparison arithmetic — never delegated to the LLM.
    Handles every documented edge case explicitly rather than letting a
    ZeroDivisionError/None propagate:

      - either value missing (None)      -> percentage_change=None, direction="unknown"
      - previous_value == 0, current > 0 -> percentage_change=None (undefined), direction="up", warning
      - previous_value == 0, current == 0 -> absolute/percentage_change=0, direction="flat"
      - equal values                     -> absolute_change=0, percentage_change=0, direction="flat"
    """
    if current_value is None or previous_value is None:
        return ComparisonResult(
            metric=metric, current_value=current_value, previous_value=previous_value,
            absolute_change=None, percentage_change=None, direction="unknown",
            current_period=current_period, previous_period=previous_period,
            warning="One or both periods returned no value — comparison is incomplete.",
        )

    absolute_change = current_value - previous_value
    warning = None
    if previous_value == 0:
        if current_value == 0:
            percentage_change = 0.0
        else:
            percentage_change = None
            warning = "Previous period value was zero — percentage change is undefined."
    else:
        percentage_change = round((absolute_change / abs(previous_value)) * 100, 2)

    if absolute_change > 0:
        direction: Direction = "up"
    elif absolute_change < 0:
        direction = "down"
    else:
        direction = "flat"

    return ComparisonResult(
        metric=metric, current_value=current_value, previous_value=previous_value,
        absolute_change=absolute_change, percentage_change=percentage_change, direction=direction,
        current_period=current_period, previous_period=previous_period, warning=warning,
    )
