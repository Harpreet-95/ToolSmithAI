"""
Period-Comparison Insight — Day 4, Capability 2 (Business Insights).

Bounded, read-only supplementary query: for a live SQL answer that is a
single time-bound aggregate (COUNT/SUM/AVG over a resolved date_context,
no group_by), runs exactly one additional governed query for the
immediately preceding period of equal length and reports the percent
change. Never runs for list/breakdown/non-aggregate/non-time-bound
answers — there is no natural "previous period" baseline for those, and
this module never guesses one.

Reuses the exact sql_plan the primary query already validated/generated
from — only the date WHERE row's literal value changes — and executes
through the same generate_sql()/execute_governed_query() governed path
(PII masking, ownership checks, rate limits all still apply) a second
time, exactly as core.orchestrator.agent's own follow-up flow already
calls the same functions again for a fresh question. Never modifies
query_planning_service/sql_planning_service/sql_generation_service.

Any failure here (refused generation, execution error, no comparable
value) returns None — the primary answer already succeeded independently
of this and must never be affected by a failed insight computation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

ELIGIBLE_AGGREGATIONS = {"COUNT", "SUM", "AVG"}


def _parse_date(value: Any) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _previous_period(start: str, end: str) -> Optional[tuple[str, str]]:
    """The immediately preceding period of equal length — e.g. start/end of
    a 91-day quarter maps to the 91 days immediately before it. Returns None
    for an unparseable or inverted range rather than guessing."""
    start_d, end_d = _parse_date(start), _parse_date(end)
    if start_d is None or end_d is None or end_d < start_d:
        return None
    length_days = (end_d - start_d).days + 1
    previous_end = start_d - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length_days - 1)
    return previous_start.isoformat(), previous_end.isoformat()


def compute_period_comparison_insight(
    source_id: int,
    user_id: str,
    business_plan: dict,
    sql_plan: dict,
    current_value: Any,
) -> Optional[dict]:
    """Returns {type, label, current_value, previous_value, percent_change,
    direction} or None when ineligible or the comparison query itself
    doesn't yield a usable value."""
    if business_plan.get("aggregation") not in ELIGIBLE_AGGREGATIONS:
        return None
    if business_plan.get("group_by"):
        return None
    date_context = business_plan.get("date_context") or {}
    current_start, current_end = date_context.get("start"), date_context.get("end")
    if not current_start or not current_end:
        return None
    if current_value is None:
        return None

    previous_range = _previous_period(current_start, current_end)
    if previous_range is None:
        return None
    previous_start, previous_end = previous_range

    where = sql_plan.get("where") or []
    date_row_index = next(
        (
            i for i, w in enumerate(where)
            if w.get("operator") == "BETWEEN" and w.get("value") == [current_start, current_end]
        ),
        None,
    )
    if date_row_index is None:
        return None

    comparison_plan = dict(sql_plan)
    comparison_where = list(where)
    comparison_where[date_row_index] = {**where[date_row_index], "value": [previous_start, previous_end]}
    comparison_plan["where"] = comparison_where

    from data.query_execution_service import execute_governed_query
    from data.sql_generation_service import detect_dialect, generate_sql

    generated = generate_sql(source_id, user_id, comparison_plan, dialect=detect_dialect(source_id))
    if not generated.get("sql"):
        return None

    try:
        query_result, _gov_warnings = execute_governed_query(
            source_id, user_id, generated["sql"], comparison_plan,
            params=generated["parameters"]["values"],
            execution_kind="insight_comparison",
        )
    except Exception:  # noqa: BLE001 — a failed comparison query must never break the primary answer
        return None

    result = query_result.to_dict()
    if result.get("status") != "success":
        return None
    rows = result.get("rows") or []
    if not rows:
        return None
    previous_value = next(iter(rows[0].values()), None)
    if previous_value is None:
        return None

    try:
        current_num = float(current_value)
        previous_num = float(previous_value)
    except (TypeError, ValueError):
        return None
    if previous_num == 0:
        return None  # no meaningful percent change from a zero baseline

    percent_change = round(((current_num - previous_num) / abs(previous_num)) * 100, 1)
    return {
        "type": "period_comparison",
        "label": "vs. the previous period",
        "current_value": current_num,
        "previous_value": previous_num,
        "percent_change": percent_change,
        "direction": "up" if percent_change > 0 else "down" if percent_change < 0 else "flat",
    }
