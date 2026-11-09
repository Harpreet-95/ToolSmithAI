"""
Focused tests — Phase 7, Step 6G, Part 3/6: deterministic, zero-AI-call
chart/KPI specs (core/sqlservice/result_chart_builder.py).

Pure unit tests against directly-constructed SqlExecutionResult instances --
no database, no provider, no fixture DB. Mirrors tests/test_sqlservice_
result_insight_builder.py's own convention.

Run:
    venv/Scripts/pytest tests/test_sqlservice_result_chart_builder.py -v
"""

from core.sqlservice.result_chart_builder import build_deterministic_chart, build_deterministic_kpi
from core.sqlservice.sql_execution import SqlExecutionResult


def _execution_result(columns, rows, *, truncated=False, truncation_reason=None, row_count=None):
    return SqlExecutionResult(
        status="OK", source_id=90004, build_id=1, schema_fingerprint="fp",
        canonical_referenced_object_fqns=(), columns=tuple(columns), rows=tuple(rows),
        row_count=row_count if row_count is not None else len(rows),
        truncated=truncated, truncation_reason=truncation_reason,
        duration_ms=5, warnings=(), error=None, error_category=None,
    )


# ── KPI: single scalar ──────────────────────────────────────────────────


def test_single_scalar_produces_kpi():
    result = _execution_result(["StudentCount"], [(214,)])
    kpi = build_deterministic_kpi(result)
    assert kpi == {"label": "Student Count", "value": 214}
    assert build_deterministic_chart(result) is None


def test_single_row_multi_column_detail_produces_no_kpi():
    # A one-row DETAIL record (e.g. a single student's profile) is not a
    # KPI -- it stays table-only.
    result = _execution_result(["FirstName", "LastName"], [("Ada", "Lovelace")])
    assert build_deterministic_kpi(result) is None


def test_single_row_with_surrogate_id_and_one_measure_produces_kpi():
    result = _execution_result(["TrackID", "StudentCount"], [(101, 214)])
    kpi = build_deterministic_kpi(result)
    assert kpi == {"label": "Student Count", "value": 214}


def test_single_row_non_numeric_produces_no_kpi():
    result = _execution_result(["ProgramName"], [("Data Analytics",)])
    assert build_deterministic_kpi(result) is None


def test_zero_rows_produces_no_kpi():
    result = _execution_result(["StudentCount"], [])
    assert build_deterministic_kpi(result) is None


def test_truncated_single_row_produces_no_kpi():
    result = _execution_result(["StudentCount"], [(214,)], truncated=True, truncation_reason="cap")
    assert build_deterministic_kpi(result) is None


# ── Chart: suppression guards ───────────────────────────────────────────


def test_zero_rows_produces_no_chart():
    result = _execution_result(["Program", "Count"], [])
    assert build_deterministic_chart(result) is None


def test_single_row_produces_no_chart():
    result = _execution_result(["Program", "Count"], [("A", 10)])
    assert build_deterministic_chart(result) is None


def test_truncated_result_produces_no_chart():
    result = _execution_result(
        ["Program", "Count"], [("A", 500), ("B", 500)], truncated=True, truncation_reason="row cap",
    )
    assert build_deterministic_chart(result) is None


def test_ambiguous_shape_more_than_two_usable_columns_produces_no_chart():
    result = _execution_result(
        ["Program", "Enrolled", "Graduated"], [("A", 10, 5), ("B", 20, 15)],
    )
    assert build_deterministic_chart(result) is None


def test_pii_heavy_detail_result_produces_no_chart():
    # A multi-column detail/PII-shaped result (names + class) -- table
    # only, never charted.
    result = _execution_result(
        ["FirstName", "LastName", "ClassName"],
        [("Ada", "Lovelace", "SQL 101"), ("Alan", "Turing", "SQL 101")],
    )
    assert build_deterministic_chart(result) is None


def test_no_numeric_column_produces_no_chart():
    result = _execution_result(["FirstName", "LastName"], [("Ada", "Lovelace"), ("Alan", "Turing")])
    assert build_deterministic_chart(result) is None


# ── Chart: category + measure → bar / donut ─────────────────────────────


def test_category_measure_produces_bar_chart():
    result = _execution_result(
        ["Program", "StudentCount"],
        [("Data Analytics", 120), ("Data Engineering", 45), ("UX Design", 80), ("AI", 60),
         ("Cloud", 30), ("Cyber", 25), ("DevOps", 15)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "bar"
    assert chart["labels"] == [
        "Data Analytics", "Data Engineering", "UX Design", "AI", "Cloud", "Cyber", "DevOps",
    ]
    assert chart["series"] == [{"name": "Student Count", "data": [120.0, 45.0, 80.0, 60.0, 30.0, 25.0, 15.0]}]


def test_small_non_negative_composition_produces_donut_chart():
    result = _execution_result(
        ["Status", "Count"], [("Active", 120), ("Inactive", 30), ("Pending", 10)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "donut"


def test_rate_measure_never_donut_even_if_small():
    # "rate"/"avg"/etc. measures are never additive -- a donut (which
    # implies summing to a whole) would misrepresent them, so this stays
    # a bar chart even with few categories.
    result = _execution_result(
        ["Program", "CompletionRate"], [("A", 0.8), ("B", 0.6)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "bar"


def test_negative_values_never_donut():
    result = _execution_result(
        ["Category", "NetChange"], [("A", 50), ("B", -20)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "bar"


def test_surrogate_id_column_alongside_category_measure_still_produces_chart():
    result = _execution_result(
        ["TrackID", "ProgramDescription", "StudentCount"],
        [(101, "Data Analytics", 120), (205, "UX Design", 80), (312, "Data Engineering", 45)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] in ("bar", "donut")
    assert "TrackID" not in chart["labels"]


def test_too_many_categories_produces_no_chart():
    rows = [(f"Cat{i}", i) for i in range(30)]
    result = _execution_result(["Category", "Count"], rows)
    assert build_deterministic_chart(result) is None


def test_blank_label_rows_excluded():
    result = _execution_result(
        ["Program", "Count"], [("Data Analytics", 120), ("", 999), ("UX Design", 80)],
    )
    chart = build_deterministic_chart(result)
    assert "" not in chart["labels"]
    assert 999 not in chart["series"][0]["data"]


# ── Chart: date + measure → line ────────────────────────────────────────


def test_time_series_produces_line_chart():
    result = _execution_result(
        ["Month", "EnrollmentCount"],
        [("2026-01", 40), ("2026-02", 55), ("2026-03", 38)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "line"
    assert chart["labels"] == ["2026-01", "2026-02", "2026-03"]
    assert chart["series"] == [{"name": "Enrollment Count", "data": [40.0, 55.0, 38.0]}]


def test_time_series_sorted_chronologically_regardless_of_row_order():
    result = _execution_result(
        ["Month", "FeesPaid"],
        [("2026-03", 300), ("2026-01", 100), ("2026-02", 200)],
    )
    chart = build_deterministic_chart(result)
    assert chart["labels"] == ["2026-01", "2026-02", "2026-03"]
    assert chart["series"][0]["data"] == [100.0, 200.0, 300.0]


def test_real_date_objects_produce_line_chart():
    import datetime
    result = _execution_result(
        ["SignupDate", "Count"],
        [(datetime.date(2026, 1, 1), 10), (datetime.date(2026, 2, 1), 20)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "line"
    assert chart["labels"] == ["2026-01-01", "2026-02-01"]


# ── Chart: split year+month(+day) columns collapse to one date dim ─────


def test_year_month_columns_produce_line_chart():
    # GROUP BY YEAR(date), MONTH(date) -- a very common shape that returns
    # calendar parts as separate integer columns instead of one date column.
    result = _execution_result(
        ["EnrollmentYear", "EnrollmentMonth", "TotalEnrollments"],
        [(2026, 1, 46), (2026, 3, 37), (2026, 2, 19)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "line"
    assert chart["labels"] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert chart["series"] == [{"name": "Total Enrollments", "data": [46.0, 19.0, 37.0]}]


def test_year_month_day_columns_produce_line_chart_with_real_dates():
    result = _execution_result(
        ["SignupYear", "SignupMonth", "SignupDay", "Count"],
        [(2026, 1, 15, 10), (2026, 1, 20, 5)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "line"
    assert chart["labels"] == ["2026-01-15", "2026-01-20"]


def test_year_month_plus_two_measures_produces_grouped_line_chart():
    result = _execution_result(
        ["PaymentYear", "PaymentMonth", "PaymentCount", "TotalAmountPaid"],
        [(2026, 1, 279, 4969), (2026, 2, 268, 7573)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "grouped_line"
    assert len(chart["series"]) == 2


def test_year_column_without_month_stays_ambiguous_no_chart():
    # Only a year-named column, no month pairing -- never guessed at.
    result = _execution_result(
        ["EnrollmentYear", "Enrolled", "Graduated"],
        [(2026, 10, 5), (2025, 20, 15)],
    )
    assert build_deterministic_chart(result) is None


def test_month_named_column_out_of_calendar_range_stays_ambiguous_no_chart():
    # "EnrollmentMonth" with values outside 1-12 is not a real calendar
    # month -- the range guard must refuse to synthesize a date from it.
    result = _execution_result(
        ["EnrollmentYear", "EnrollmentMonth", "Count"],
        [(2026, 13, 5), (2026, 25, 9)],
    )
    assert build_deterministic_chart(result) is None


# ── Chart: date + multiple numeric measures → grouped_line ─────────────


def test_date_plus_two_measures_produces_grouped_line_chart():
    result = _execution_result(
        ["PaymentMonth", "PaymentCount", "TotalAmountPaid"],
        [("2026-01", 279, 4969), ("2026-02", 268, 7573), ("2026-03", 253, 13542)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "grouped_line"
    assert chart["labels"] == ["2026-01", "2026-02", "2026-03"]
    assert chart["series"] == [
        {"name": "Payment Count", "data": [279.0, 268.0, 253.0]},
        {"name": "Total Amount Paid", "data": [4969.0, 7573.0, 13542.0]},
    ]


def test_date_plus_four_measures_produces_grouped_line_chart():
    result = _execution_result(
        ["Month", "A", "B", "C", "D"],
        [("2026-01", 1, 2, 3, 4), ("2026-02", 5, 6, 7, 8)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "grouped_line"
    assert len(chart["series"]) == 4


def test_date_plus_five_measures_stays_ambiguous_no_chart():
    result = _execution_result(
        ["Month", "A", "B", "C", "D", "E"],
        [("2026-01", 1, 2, 3, 4, 5), ("2026-02", 5, 6, 7, 8, 9)],
    )
    assert build_deterministic_chart(result) is None


def test_category_plus_two_measures_stays_ambiguous_no_chart():
    # The multi-measure relaxation is deliberately scoped to a DATE
    # dimension only -- stacking/grouping differing-unit measures across a
    # plain category axis is a separate, riskier design question with no
    # live evidence yet, so this stays table-only.
    result = _execution_result(
        ["Program", "StudentCount", "TotalFees"],
        [("A", 10, 500), ("B", 20, 900)],
    )
    assert build_deterministic_chart(result) is None


def test_date_plus_measures_with_nulls_preserves_null_gaps():
    result = _execution_result(
        ["Month", "Count", "Total"],
        [("2026-01", 10, 100), ("2026-02", None, 200), ("2026-03", 30, None)],
    )
    chart = build_deterministic_chart(result)
    assert chart["series"][0]["data"] == [10.0, None, 30.0]
    assert chart["series"][1]["data"] == [100.0, 200.0, None]


# ── Chart: two numeric measures → scatter (only when meaningful) ───────


def test_two_numeric_measures_with_variance_produces_scatter():
    result = _execution_result(
        ["AttendanceRate", "FinalScore"],
        [(0.9, 95), (0.6, 70), (0.75, 82), (0.5, 60)],
    )
    chart = build_deterministic_chart(result)
    assert chart["chart_type"] == "scatter"
    assert chart["points"] == [
        {"x": 0.9, "y": 95.0}, {"x": 0.6, "y": 70.0}, {"x": 0.75, "y": 82.0}, {"x": 0.5, "y": 60.0},
    ]


def test_two_numeric_measures_no_x_variance_produces_no_chart():
    # Degenerate -- every x value identical, nothing meaningful to plot.
    result = _execution_result(
        ["FixedFee", "AmountPaid"], [(100, 10), (100, 20), (100, 30)],
    )
    assert build_deterministic_chart(result) is None


def test_two_numeric_measures_too_few_points_produces_no_chart():
    result = _execution_result(["X", "Y"], [(1, 2), (3, 4)])
    assert build_deterministic_chart(result) is None


# ── Null-heavy / ambiguous results ──────────────────────────────────────


def test_null_heavy_category_measure_excludes_null_rows_but_still_charts():
    result = _execution_result(
        ["Program", "StudentCount"],
        [("Data Analytics", 120), (None, 50), ("UX Design", None), ("AI", 60)],
    )
    chart = build_deterministic_chart(result)
    assert chart is not None
    assert chart["labels"] == ["Data Analytics", "AI"]
    assert chart["series"][0]["data"] == [120.0, 60.0]


def test_all_null_measure_column_produces_no_chart():
    result = _execution_result(
        ["Program", "StudentCount"], [("A", None), ("B", None), ("C", None)],
    )
    assert build_deterministic_chart(result) is None


def test_mixed_type_column_is_ambiguous_and_produces_no_chart():
    # A column holding a genuine mix of numeric and non-numeric values (not
    # cleanly "numeric", "date", or "other") is never force-classified.
    result = _execution_result(
        ["Label", "MixedValue"], [("A", 10), ("B", "not a number"), ("C", 30)],
    )
    chart = build_deterministic_chart(result)
    # "MixedValue" reads as 'other' (not all-numeric), "Label" also 'other'
    # -- two 'other' columns is not a shape this module understands.
    assert chart is None


# ── Never raises ─────────────────────────────────────────────────────────


def test_none_execution_result_returns_none_safely():
    assert build_deterministic_chart(None) is None
    assert build_deterministic_kpi(None) is None
