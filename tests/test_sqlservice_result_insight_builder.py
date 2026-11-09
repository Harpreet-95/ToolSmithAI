"""
Focused tests — Phase 7, Step 6E, Part 3: deterministic, zero-AI-call result
insights (core/sqlservice/result_insight_builder.py).

Pure unit tests against directly-constructed SqlExecutionResult instances --
no database, no provider, no fixture DB.

Run:
    venv/Scripts/pytest tests/test_sqlservice_result_insight_builder.py -v
"""

from core.sqlservice.result_insight_builder import build_deterministic_insights
from core.sqlservice.sql_execution import SqlExecutionResult


def _execution_result(columns, rows, *, truncated=False, truncation_reason=None, row_count=None):
    return SqlExecutionResult(
        status="OK", source_id=90004, build_id=1, schema_fingerprint="fp",
        canonical_referenced_object_fqns=(), columns=tuple(columns), rows=tuple(rows),
        row_count=row_count if row_count is not None else len(rows),
        truncated=truncated, truncation_reason=truncation_reason,
        duration_ms=5, warnings=(), error=None, error_category=None,
    )


# ── Suppression guards ──────────────────────────────────────────────────


def test_zero_rows_produces_no_insights():
    result = _execution_result(["Count"], [])
    assert build_deterministic_insights(result) == ()


def test_single_row_produces_no_insights():
    result = _execution_result(["Count"], [(42,)])
    assert build_deterministic_insights(result) == ()


def test_truncated_result_produces_no_insights():
    result = _execution_result(
        ["Program", "Count"], [("A", 500), ("B", 500)], truncated=True, truncation_reason="row cap",
    )
    assert build_deterministic_insights(result) == ()


def test_ambiguous_shape_more_than_one_numeric_column_produces_no_insights():
    result = _execution_result(
        ["Program", "Enrolled", "Graduated"], [("A", 10, 5), ("B", 20, 15)],
    )
    assert build_deterministic_insights(result) == ()


def test_surrogate_id_column_alongside_category_measure_still_produces_insights():
    # A realistic GROUP BY result shape: a numeric surrogate key (TrackID)
    # plus a genuine category+measure pair -- must not be treated as
    # "more than one numeric column" ambiguity.
    result = _execution_result(
        ["TrackID", "ProgramDescription", "StudentCount"],
        [(101, "Data Analytics", 120), (205, "UX Design", 80), (312, "Data Engineering", 45)],
    )
    insights = build_deterministic_insights(result)
    titles = [i.title for i in insights]
    assert "Highest and lowest" in titles
    hi_lo = next(i for i in insights if i.title == "Highest and lowest")
    assert "Data Analytics" in hi_lo.finding
    # The surrogate key column itself is never cited as evidence.
    assert all(c.name != "TrackID" for c in hi_lo.evidence_columns)


def test_two_surrogate_id_columns_alongside_one_measure_still_suppressed():
    # Stripping id-like columns must leave exactly 2 usable columns to
    # proceed -- here only 1 remains (StudentCount), so it stays suppressed
    # rather than guessing.
    result = _execution_result(
        ["TrackID", "InstructorID", "StudentCount"], [(1, 9, 10), (2, 8, 20)],
    )
    assert build_deterministic_insights(result) == ()


def test_no_numeric_column_produces_no_insights():
    result = _execution_result(["FirstName", "LastName"], [("Ada", "Lovelace"), ("Alan", "Turing")])
    assert build_deterministic_insights(result) == ()


# ── Category/measure breakdown ──────────────────────────────────────────


def test_highest_and_lowest_category_grounded():
    result = _execution_result(
        ["Program", "StudentCount"], [("Data Analytics", 120), ("Data Engineering", 45), ("UX Design", 80)],
    )
    insights = build_deterministic_insights(result)
    titles = [i.title for i in insights]
    assert "Highest and lowest" in titles
    hi_lo = next(i for i in insights if i.title == "Highest and lowest")
    assert "Data Analytics" in hi_lo.finding
    assert "Data Engineering" in hi_lo.finding
    assert "120" in hi_lo.finding and "45" in hi_lo.finding
    assert hi_lo.provenance == "deterministic_result_analysis"
    # Every evidence row number must be a real 1-indexed position into rows.
    assert all(1 <= n <= result.row_count for n in hi_lo.evidence_row_numbers)


def test_gap_between_top_two_is_recomputable():
    result = _execution_result(
        ["Program", "StudentCount"], [("Data Analytics", 120), ("Data Engineering", 45), ("UX Design", 80)],
    )
    insights = build_deterministic_insights(result)
    gap = next(i for i in insights if i.title == "Gap between top two")
    assert "Data Analytics" in gap.finding and "UX Design" in gap.finding
    assert "40" in gap.finding  # 120 - 80


def test_share_of_total_is_recomputable():
    result = _execution_result(
        ["Program", "StudentCount"], [("Data Analytics", 60), ("UX Design", 40)],
    )
    insights = build_deterministic_insights(result)
    share = next(i for i in insights if i.title == "Share of total")
    assert "60.0%" in share.finding  # 60 / (60+40) * 100


def test_average_measure_column_never_summed_for_share():
    # "AverageScore" is not additive -- summing it across rows would be a
    # meaningless number, so "Share of total" must be suppressed even
    # though highest/lowest and the top-two gap are still safe.
    result = _execution_result(
        ["Program", "AverageScore"], [("Data Analytics", 88), ("UX Design", 72)],
    )
    insights = build_deterministic_insights(result)
    titles = [i.title for i in insights]
    assert "Share of total" not in titles
    assert "Highest and lowest" in titles


def test_duplicate_labels_reported_as_distinct_categories():
    result = _execution_result(
        ["Status", "Count"], [("Active", 10), ("Active", 5), ("Inactive", 3)],
    )
    insights = build_deterministic_insights(result)
    distinct = next(i for i in insights if i.title == "Distinct categories")
    assert "3 rows" in distinct.finding and "2 distinct" in distinct.finding


def test_tied_maximum_worded_as_tied_not_a_false_unique_claim():
    result = _execution_result(
        ["Program", "Count"], [("A", 50), ("B", 50), ("C", 10)],
    )
    insights = build_deterministic_insights(result)
    hi_lo = next(i for i in insights if i.title == "Highest and lowest")
    assert "(tied)" in hi_lo.finding


def test_blank_string_label_rows_are_skipped_like_null():
    result = _execution_result(
        ["Program", "Count"], [("A", 50), ("", 0), ("   ", 0), ("B", 10)],
    )
    insights = build_deterministic_insights(result)
    hi_lo = next(i for i in insights if i.title == "Highest and lowest")
    assert "A" in hi_lo.finding and "B" in hi_lo.finding
    assert ",  ," not in hi_lo.finding and ", ," not in hi_lo.finding


def test_null_label_or_value_rows_are_skipped_not_crashed_on():
    result = _execution_result(
        ["Program", "Count"], [("A", 50), (None, 30), ("B", None), ("C", 10)],
    )
    insights = build_deterministic_insights(result)
    hi_lo = next(i for i in insights if i.title == "Highest and lowest")
    assert "A" in hi_lo.finding and "C" in hi_lo.finding


def test_zero_total_avoids_divide_by_zero_in_share_insight():
    result = _execution_result(
        ["Program", "Count"], [("A", 0), ("B", 0)],
    )
    insights = build_deterministic_insights(result)
    titles = [i.title for i in insights]
    assert "Share of total" not in titles  # total == 0, safely suppressed
    assert "Highest and lowest" not in titles  # max == min == 0, no real high/low


# ── Time-series (period-over-period) ────────────────────────────────────


def test_period_over_period_change_is_recomputable():
    result = _execution_result(
        ["Month", "Enrollments"], [("2026-06", 100), ("2026-07", 120)],
    )
    insights = build_deterministic_insights(result)
    assert len(insights) == 1
    change = insights[0]
    assert change.title == "Period-over-period change"
    assert "increased" in change.finding
    assert "20.0%" in change.finding  # (120-100)/100 * 100


def test_period_over_period_decrease_worded_correctly():
    result = _execution_result(
        ["Month", "Enrollments"], [("2026-06", 100), ("2026-07", 80)],
    )
    insights = build_deterministic_insights(result)
    change = insights[0]
    assert "decreased" in change.finding
    assert "20.0%" in change.finding


def test_period_over_period_zero_previous_value_suppressed():
    result = _execution_result(
        ["Month", "Enrollments"], [("2026-06", 0), ("2026-07", 80)],
    )
    assert build_deterministic_insights(result) == ()


# ── Split year+month(+day) columns collapse to one date dimension ──────


def test_year_month_columns_produce_period_over_period_insight():
    # GROUP BY YEAR(date), MONTH(date) -- calendar parts as separate integer
    # columns instead of one date column; must still be recognized as a
    # time series, not left ambiguous as 3 "numeric" columns.
    result = _execution_result(
        ["EnrollmentYear", "EnrollmentMonth", "TotalEnrollments"],
        [(2026, 1, 46), (2026, 2, 19), (2026, 3, 37)],
    )
    insights = build_deterministic_insights(result)
    assert len(insights) == 1
    change = insights[0]
    assert change.title == "Period-over-period change"
    assert "increased" in change.finding  # 19 -> 37


def test_year_column_without_month_stays_ambiguous_no_insights():
    result = _execution_result(
        ["EnrollmentYear", "Enrolled", "Graduated"],
        [(2026, 10, 5), (2025, 20, 15)],
    )
    assert build_deterministic_insights(result) == ()


def test_month_named_column_out_of_calendar_range_stays_ambiguous_no_insights():
    result = _execution_result(
        ["EnrollmentYear", "EnrollmentMonth", "Count"],
        [(2026, 13, 5), (2026, 25, 9)],
    )
    assert build_deterministic_insights(result) == ()


# ── Never raises ─────────────────────────────────────────────────────────


def test_builder_never_raises_on_malformed_input():
    assert build_deterministic_insights(None) == ()
    result = _execution_result(["A", "B"], [("x", "y"), ("z", "w")])  # non-numeric, non-date, 2 text cols
    assert build_deterministic_insights(result) == ()
