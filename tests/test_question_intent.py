"""
Tests for Milestone M-1 — Enterprise Question Intelligence.

Pure unit tests for core.semantic.concept_resolver.extract_query_intent() —
no DB, no fixtures, matching the file's own "deterministic regex only, no
AI/LLM" contract. A fixed `today` is passed to every date-range test so the
assertions are stable regardless of when the suite runs; extract_query_intent
itself defaults to the real wall-clock date when `today` is omitted.

Run from the project root:
    venv/Scripts/pytest tests/test_question_intent.py -v
"""
from datetime import date

from core.semantic.concept_resolver import extract_query_intent

_TODAY = date(2026, 7, 12)  # a Sunday


# ---------------------------------------------------------------------------
# Aggregation detection
# ---------------------------------------------------------------------------

def test_count_how_many():
    assert extract_query_intent("How many clients?")["aggregation"] == "COUNT"


def test_count_number_of():
    assert extract_query_intent("Number of students")["aggregation"] == "COUNT"


def test_count_total_number_of_not_sum():
    # "total number of invoices" must resolve to COUNT, not SUM, even though
    # it contains the word "total" — the intent is a row count.
    assert extract_query_intent("Total number of invoices")["aggregation"] == "COUNT"


def test_sum_total():
    assert extract_query_intent("Total payroll")["aggregation"] == "SUM"


def test_sum_total_revenue():
    assert extract_query_intent("Total revenue")["aggregation"] == "SUM"


def test_average():
    assert extract_query_intent("Average salary")["aggregation"] == "AVG"
    assert extract_query_intent("Average invoice amount")["aggregation"] == "AVG"


def test_minimum():
    assert extract_query_intent("Lowest salary")["aggregation"] == "MIN"
    assert extract_query_intent("Smallest invoice")["aggregation"] == "MIN"


def test_maximum():
    assert extract_query_intent("Highest payroll")["aggregation"] == "MAX"
    assert extract_query_intent("Largest payment")["aggregation"] == "MAX"


def test_no_aggregation_language():
    assert extract_query_intent("revenue by status")["aggregation"] is None


# ---------------------------------------------------------------------------
# Distinct
# ---------------------------------------------------------------------------

def test_distinct_unique_clients():
    intent = extract_query_intent("How many unique clients?")
    assert intent["aggregation"] == "COUNT"
    assert intent["distinct"] is True


def test_distinct_students():
    intent = extract_query_intent("Distinct students")
    assert intent["distinct"] is True


def test_distinct_recruiters_unique_keyword():
    intent = extract_query_intent("Unique recruiters")
    assert intent["distinct"] is True


def test_not_distinct_when_absent():
    assert extract_query_intent("Total payroll")["distinct"] is False


# ---------------------------------------------------------------------------
# Ranking / ordering
# ---------------------------------------------------------------------------

def test_top_n():
    order = extract_query_intent("Top 10 clients")["order"]
    assert order == {"direction": "DESC", "limit": 10}


def test_top_n_different_number():
    order = extract_query_intent("Top 20 recruiters")["order"]
    assert order == {"direction": "DESC", "limit": 20}


def test_bottom_n():
    order = extract_query_intent("Bottom 5 performers")["order"]
    assert order == {"direction": "ASC", "limit": 5}


def test_latest():
    order = extract_query_intent("Latest invoices")["order"]
    assert order["direction"] == "DESC"
    assert order["target"] == "date"
    assert order["limit"] > 0


def test_newest():
    order = extract_query_intent("Newest students")["order"]
    assert order["direction"] == "DESC"
    assert order["target"] == "date"


def test_earliest():
    order = extract_query_intent("Earliest enrollments")["order"]
    assert order["direction"] == "ASC"
    assert order["target"] == "date"


def test_first_enrollments_leading_first():
    order = extract_query_intent("First enrollments")["order"]
    assert order is not None
    assert order["direction"] == "ASC"


def test_first_name_is_not_earliest():
    # "first name" is the classic false positive for a bare "first" keyword —
    # must NOT be interpreted as an EARLIEST ordering request.
    assert extract_query_intent("What is the first name column?")["order"] is None


def test_no_ranking_language():
    assert extract_query_intent("revenue by status")["order"] is None


# ---------------------------------------------------------------------------
# Date intelligence
# ---------------------------------------------------------------------------

def test_today():
    dr = extract_query_intent("Active students today", today=_TODAY)["date_range"]
    assert dr == {"label": "today", "start": "2026-07-12", "end": "2026-07-12"}


def test_yesterday():
    dr = extract_query_intent("Payments yesterday", today=_TODAY)["date_range"]
    assert dr == {"label": "yesterday", "start": "2026-07-11", "end": "2026-07-11"}


def test_this_week():
    dr = extract_query_intent("Enrollments this week", today=_TODAY)["date_range"]
    assert dr["label"] == "this_week"
    # 2026-07-12 is a Sunday -> week = Mon 2026-07-06 .. Sun 2026-07-12
    assert dr["start"] == "2026-07-06"
    assert dr["end"] == "2026-07-12"


def test_last_week():
    dr = extract_query_intent("Enrollments last week", today=_TODAY)["date_range"]
    assert dr["label"] == "last_week"
    assert dr["start"] == "2026-06-29"
    assert dr["end"] == "2026-07-05"


def test_this_month():
    dr = extract_query_intent("Revenue this month", today=_TODAY)["date_range"]
    assert dr == {"label": "this_month", "start": "2026-07-01", "end": "2026-07-31"}


def test_last_month():
    dr = extract_query_intent("Revenue last month", today=_TODAY)["date_range"]
    assert dr == {"label": "last_month", "start": "2026-06-01", "end": "2026-06-30"}


def test_this_quarter():
    dr = extract_query_intent("Placements this quarter", today=_TODAY)["date_range"]
    # July 2026 -> Q3 = Jul 1 .. Sep 30
    assert dr == {"label": "this_quarter", "start": "2026-07-01", "end": "2026-09-30"}


def test_last_quarter():
    dr = extract_query_intent("Placements last quarter", today=_TODAY)["date_range"]
    # previous quarter (Q2 2026) = Apr 1 .. Jun 30
    assert dr == {"label": "last_quarter", "start": "2026-04-01", "end": "2026-06-30"}


def test_this_year():
    dr = extract_query_intent("Revenue this year", today=_TODAY)["date_range"]
    assert dr == {"label": "this_year", "start": "2026-01-01", "end": "2026-12-31"}


def test_last_year():
    dr = extract_query_intent("Revenue last year", today=_TODAY)["date_range"]
    assert dr == {"label": "last_year", "start": "2025-01-01", "end": "2025-12-31"}


def test_between_dates_iso():
    dr = extract_query_intent("Invoices between 2026-01-01 and 2026-03-31")["date_range"]
    assert dr == {"label": "between", "start": "2026-01-01", "end": "2026-03-31"}


def test_between_dates_us_format():
    dr = extract_query_intent("Invoices between 01/15/2026 and 03/01/2026")["date_range"]
    assert dr == {"label": "between", "start": "2026-01-15", "end": "2026-03-01"}


def test_between_dates_unparsable_returns_none_not_a_guess():
    # Malformed dates must never produce a fabricated range.
    assert extract_query_intent("Invoices between last tuesday and now")["date_range"] is None


def test_no_date_language():
    assert extract_query_intent("revenue by status")["date_range"] is None


def test_default_today_uses_real_wall_clock():
    # Without an explicit `today`, extract_query_intent must not crash and
    # must return a well-formed range using the real current date.
    dr = extract_query_intent("Active students today")["date_range"]
    assert dr["label"] == "today"
    assert dr["start"] == dr["end"] == date.today().isoformat()


# ---------------------------------------------------------------------------
# Status filters
# ---------------------------------------------------------------------------

def test_status_active():
    assert extract_query_intent("Active students")["status_value"] == "Active"


def test_status_open():
    assert extract_query_intent("Open jobs")["status_value"] == "Open"


def test_status_closed():
    assert extract_query_intent("Closed placements")["status_value"] == "Closed"


def test_status_completed():
    assert extract_query_intent("Completed enrollments")["status_value"] == "Completed"


def test_status_cancelled():
    assert extract_query_intent("Cancelled interviews")["status_value"] == "Cancelled"


def test_status_cancelled_american_spelling_normalized():
    assert extract_query_intent("Canceled interviews")["status_value"] == "Cancelled"


def test_status_pending():
    assert extract_query_intent("Pending submissions")["status_value"] == "Pending"


def test_status_inactive():
    assert extract_query_intent("Inactive candidates")["status_value"] == "Inactive"


def test_no_status_language():
    assert extract_query_intent("revenue by status")["status_value"] is None


# ---------------------------------------------------------------------------
# Combined intents
# ---------------------------------------------------------------------------

def test_combined_top_n_status_and_date():
    intent = extract_query_intent("Top 10 active clients this month", today=_TODAY)
    assert intent["order"] == {"direction": "DESC", "limit": 10}
    assert intent["status_value"] == "Active"
    assert intent["date_range"]["label"] == "this_month"


def test_combined_count_distinct_and_status():
    intent = extract_query_intent("How many unique open jobs")
    assert intent["aggregation"] == "COUNT"
    assert intent["distinct"] is True
    assert intent["status_value"] == "Open"


# ---------------------------------------------------------------------------
# Negative / unsupported questions — must not fabricate anything
# ---------------------------------------------------------------------------

def test_unsupported_question_returns_all_none():
    intent = extract_query_intent("What is a foreign key?")
    assert intent == {
        "aggregation": None, "aggregation_target": None, "distinct": False,
        "order": None, "date_range": None, "status_value": None,
    }


def test_empty_question_returns_all_none():
    intent = extract_query_intent("")
    assert intent["aggregation"] is None
    assert intent["distinct"] is False
    assert intent["order"] is None
    assert intent["date_range"] is None
    assert intent["status_value"] is None


def test_none_question_does_not_crash():
    intent = extract_query_intent(None)  # type: ignore[arg-type]
    assert intent["aggregation"] is None
