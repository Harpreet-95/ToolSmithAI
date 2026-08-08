"""
Tests for Day 4, Capability 4 — Natural-Language Answer Quality:
core.answering.result_formatter's new template helpers and their effect on
build_business_answer(). Pure unit-level, no DB, no fixtures — mirrors
tests/test_chart_spec.py's style.

Run from the project root:
    python -m pytest tests/test_natural_language_answers.py -v
"""
from __future__ import annotations

from core.answering.result_formatter import (
    _business_noun,
    _extract_series_for_summary,
    _grouped_answer,
    _insight_clause,
    _ranked_answer,
    _tabular_answer,
    build_business_answer,
)


# ---------------------------------------------------------------------------
# _business_noun
# ---------------------------------------------------------------------------

def test_business_noun_prefers_remembered_terminology():
    plan = {"remembered_terminology": [{"original_term": "clients", "canonical_term": "client"}], "entity_label": "vw_Clients"}
    assert _business_noun(plan, "vw_Clients") == "clients"


def test_business_noun_falls_back_to_generated_vocabulary():
    plan = {"generated_vocabulary_evidence": [{"original_term": "students", "generated_term": "student"}]}
    assert _business_noun(plan, "ADF_Student") == "students"


def test_business_noun_falls_back_to_stripped_pluralized_entity_label_when_no_evidence():
    # Live-verification finding: a grouped/dimension query sometimes carries
    # no vocabulary evidence even when a scalar query against the SAME
    # table did — "Adf student peaked... with... adf student" is not
    # acceptable business language, so the fallback strips the technical
    # prefix and pluralizes rather than humanizing the raw table name as-is.
    assert _business_noun({}, "ADF_Student") == "students"


def test_business_noun_never_invents_when_evidence_present_but_empty():
    plan = {"remembered_terminology": [], "generated_vocabulary_evidence": []}
    assert _business_noun(plan, "vw_Clients") == "clients"


def test_business_noun_fallback_does_not_double_pluralize_already_plural_word():
    assert _business_noun({}, "dbo.Orders") == "orders"


def test_business_noun_fallback_handles_default_record_label_without_mangling():
    assert _business_noun({}, "record(s)") == "record(s)"


def test_business_noun_fallback_preserves_multi_word_label_unchanged():
    assert _business_noun({}, "active clients") == "active clients"


# ---------------------------------------------------------------------------
# _insight_clause
# ---------------------------------------------------------------------------

def test_insight_clause_up():
    assert _insight_clause({"percent_change": 200.0, "direction": "up"}) == "That's up 200% from the previous period."


def test_insight_clause_down():
    assert _insight_clause({"percent_change": -40.0, "direction": "down"}) == "That's down 40% from the previous period."


def test_insight_clause_flat():
    assert _insight_clause({"percent_change": 0.0, "direction": "flat"}) == "That's flat compared to the previous period."


def test_insight_clause_decimal_percent_not_truncated():
    assert _insight_clause({"percent_change": 41.5, "direction": "up"}) == "That's up 41.5% from the previous period."


def test_insight_clause_none_when_insight_absent():
    assert _insight_clause(None) == ""


def test_insight_clause_none_when_fields_missing():
    assert _insight_clause({"type": "period_comparison"}) == ""


# ---------------------------------------------------------------------------
# _extract_series_for_summary
# ---------------------------------------------------------------------------

_GROUPED_PLAN = {
    "group_by": [{"alias": "year", "column_name": "year", "time_grain": "year"}],
    "select": [
        {"alias": "year", "column_name": "year"},
        {"alias": "count_id", "column_name": "id", "aggregation": "COUNT"},
    ],
}


def test_extract_series_grouped():
    rows = [{"year": 2021, "count_id": 100}, {"year": 2022, "count_id": 140}]
    assert _extract_series_for_summary(_GROUPED_PLAN, rows) == (["2021", "2022"], [100, 140])


def test_extract_series_multi_dimension_returns_none():
    plan = {**_GROUPED_PLAN, "group_by": _GROUPED_PLAN["group_by"] + [{"alias": "region", "column_name": "region"}]}
    assert _extract_series_for_summary(plan, [{"year": 2021, "region": "West", "count_id": 5}]) is None


def test_extract_series_ranked_without_group_by_uses_first_non_aggregated_select():
    plan = {
        "group_by": [],
        "select": [{"alias": "class_name", "column_name": "class_name"}, {"alias": "count_id", "aggregation": "COUNT"}],
    }
    rows = [{"class_name": "CS101", "count_id": 90}]
    assert _extract_series_for_summary(plan, rows) == (["CS101"], [90])


def test_extract_series_no_measure_returns_none():
    plan = {"group_by": [{"alias": "year"}], "select": [{"alias": "year"}]}
    assert _extract_series_for_summary(plan, [{"year": 2021}]) is None


def test_extract_series_structural_mismatch_returns_none():
    assert _extract_series_for_summary(_GROUPED_PLAN, [{"unexpected": 1, "count_id": 100}]) is None


def test_extract_series_empty_rows_returns_none():
    assert _extract_series_for_summary(_GROUPED_PLAN, []) is None


def test_extract_series_blank_string_category_gets_neutral_placeholder():
    # Live-verification finding: a real, verified row can have an empty
    # string (not NULL) on file for the dimension column — must still be
    # named (never dropped), but "" breaks sentence grammar downstream.
    rows = [{"year": "TX", "count_id": 100}, {"year": "", "count_id": 50}, {"year": "NA", "count_id": 30}]
    labels, values = _extract_series_for_summary(_GROUPED_PLAN, rows)
    assert labels == ["TX", "(unspecified)", "NA"]
    assert values == [100, 50, 30]


def test_extract_series_null_category_becomes_none_string():
    rows = [{"year": None, "count_id": 50}]
    labels, _ = _extract_series_for_summary(_GROUPED_PLAN, rows)
    assert labels == ["None"]


# ---------------------------------------------------------------------------
# _grouped_answer
# ---------------------------------------------------------------------------

def test_grouped_answer_time_series_mentions_peak_and_range():
    rows = [{"year": 2012, "count_id": 140}, {"year": 2019, "count_id": 1583}, {"year": 2026, "count_id": 60}]
    plan = {**_GROUPED_PLAN, "aggregation": "COUNT", "dimension_labels": {"year": "Start Date"}}
    answer, summary = _grouped_answer(plan, rows, "students")
    assert "peaked in 2019 with 1,583 students" in answer
    assert "3 yearly groups from 2012 through 2026" in answer
    assert "1,583" in summary


def test_grouped_answer_categorical_small_n_lists_full_breakdown():
    plan = {
        "group_by": [{"alias": "status", "column_name": "status"}],
        "select": [{"alias": "status"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "aggregation": "COUNT",
        "dimension_labels": {"status": "Current Status"},
    }
    rows = [
        {"status": "Stalled", "count_id": 83}, {"status": "Graduated", "count_id": 76},
        {"status": "Active", "count_id": 5}, {"status": "Not Started", "count_id": 3},
    ]
    answer, summary = _grouped_answer(plan, rows, "launch participants")
    assert answer.startswith("Most launch participants are Stalled or Graduated.")
    assert "83 stalled, 76 graduated, 5 active, and 3 not started" in answer.lower()


def test_grouped_answer_categorical_large_n_states_group_count_not_full_list():
    plan = {
        "group_by": [{"alias": "state", "column_name": "state"}],
        "select": [{"alias": "state"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "aggregation": "COUNT",
        "dimension_labels": {"state": "State"},
    }
    rows = [{"state": f"S{i}", "count_id": 10 - i} for i in range(10)]
    answer, _ = _grouped_answer(plan, rows, "clients")
    assert "10 state groups" in answer
    assert "S0" in answer  # top category still named
    assert "S9" not in answer  # not every category dumped into prose


def test_grouped_answer_sum_aggregation_uses_measure_label_not_most_phrasing():
    plan = {
        "group_by": [{"alias": "region", "column_name": "region"}],
        "select": [{"alias": "region"}, {"alias": "sum_rev", "aggregation": "SUM"}],
        "aggregation": "SUM", "measure_label": "Revenue",
        "dimension_labels": {"region": "Region"},
    }
    rows = [{"region": "North", "sum_rev": 500}, {"region": "South", "sum_rev": 300}]
    answer, _ = _grouped_answer(plan, rows, "regions")
    assert "Revenue is highest for North" in answer
    assert "Most regions are" not in answer


def test_grouped_answer_falls_back_when_series_extraction_fails():
    plan = {"group_by": [], "select": []}
    answer, summary = _grouped_answer(plan, [{"a": 1}], "widgets")
    assert answer == "Widgets are grouped below by category."
    assert summary == "Grouped by category."


# ---------------------------------------------------------------------------
# _ranked_answer
# ---------------------------------------------------------------------------

def test_ranked_answer_top_leads_with_followed_by():
    plan = {
        "group_by": [{"alias": "state", "column_name": "state"}],
        "select": [{"alias": "state"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "aggregation": "COUNT",
    }
    rows = [{"state": "TX", "count_id": 12000}, {"state": "CA", "count_id": 4000}, {"state": "FL", "count_id": 2400}]
    answer, summary = _ranked_answer(plan, rows, "clients", "top", 3)
    assert answer == "TX leads with 12,000 clients, followed by CA and FL."


def test_ranked_answer_bottom_direction_wording():
    plan = {
        "group_by": [{"alias": "state", "column_name": "state"}],
        "select": [{"alias": "state"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "aggregation": "COUNT",
    }
    rows = [{"state": "WY", "count_id": 5}]
    answer, _ = _ranked_answer(plan, rows, "clients", "bottom", 1)
    assert answer == "WY has the fewest, with 5 clients."


def test_ranked_answer_single_result_no_followed_by():
    plan = {
        "group_by": [{"alias": "state", "column_name": "state"}],
        "select": [{"alias": "state"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "aggregation": "COUNT",
    }
    rows = [{"state": "TX", "count_id": 12000}]
    answer, _ = _ranked_answer(plan, rows, "clients", "top", 1)
    assert answer == "TX leads with 12,000 clients."


def test_ranked_answer_never_produces_broken_grammar_for_a_blank_category():
    # Regression for the exact live bug: "TX leads with 11,916 clients,
    # followed by and NA." (a real row had an empty-string state on file).
    plan = {
        "group_by": [{"alias": "state", "column_name": "state"}],
        "select": [{"alias": "state"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "aggregation": "COUNT",
    }
    rows = [{"state": "TX", "count_id": 11916}, {"state": "", "count_id": 11572}, {"state": "NA", "count_id": 6140}]
    answer, _ = _ranked_answer(plan, rows, "clients", "top", 10)
    assert answer == "TX leads with 11,916 clients, followed by (unspecified) and NA."


def test_ranked_answer_falls_back_when_series_extraction_fails():
    plan = {"group_by": [], "select": [], "measure_label": "value"}
    answer, summary = _ranked_answer(plan, [{"a": 1}], "widgets", "top", 5)
    assert answer == "The top 5 widgets are shown below."


# ---------------------------------------------------------------------------
# _tabular_answer
# ---------------------------------------------------------------------------

def test_tabular_answer_mentions_related_entities():
    plan = {"dimension_labels": {"enroll_date": "Enrollment Date", "course_title": "Course Title"}}
    answer, summary = _tabular_answer(plan, 42, "students")
    assert answer == "42 students are shown below, including enrollment date and course title."
    assert summary == "42 students."


def test_tabular_answer_no_related_entities():
    answer, _ = _tabular_answer({}, 5, "widgets")
    assert answer == "5 widgets are shown below."


# ---------------------------------------------------------------------------
# build_business_answer — full integration, matching the capability brief's
# own worked examples where the synthetic data allows an exact match.
# ---------------------------------------------------------------------------

def _base_data(**overrides):
    data = {
        "status": "success", "row_count": 1, "truncated": False,
        "rows": [{"count_id": 10918}],
        "business_plan": {
            "aggregation": "COUNT", "entity_label": "ADF_Student",
            "generated_vocabulary_evidence": [{"original_term": "students", "generated_term": "student"}],
            "group_by": [], "where": [], "select": [{"alias": "count_id", "aggregation": "COUNT"}],
            "source_tables": ["dbo.ADF_Student"],
        },
    }
    data.update(overrides)
    return data


def test_scalar_count_matches_brief_example():
    answer = build_business_answer(_base_data())
    assert answer["answer"] == "There are 10,918 students in the database."
    assert answer["actual_value"] == 10918  # numeric value unchanged


def test_scalar_count_with_insight_matches_brief_example():
    plan = {
        "aggregation": "COUNT", "entity_label": "vw_Clients",
        "generated_vocabulary_evidence": [{"original_term": "clients", "generated_term": "client"}],
        "group_by": [], "where": [], "select": [{"alias": "count_id", "aggregation": "COUNT"}],
        "source_tables": ["dbo.vw_Clients"],
        "date_context": {"label": "last quarter", "start": "2026-04-01", "end": "2026-06-30"},
    }
    data = _base_data(
        rows=[{"count_id": 6}], business_plan=plan,
        insight={"type": "period_comparison", "label": "vs. the previous period", "current_value": 6, "previous_value": 2, "percent_change": 200.0, "direction": "up"},
    )
    answer = build_business_answer(data)
    assert answer["answer"] == "There were 6 clients last quarter. That's up 200% from the previous period."
    assert answer["insight"]["percent_change"] == 200.0  # insight payload itself untouched


def test_grouped_categorical_matches_brief_example():
    plan = {
        "aggregation": "COUNT", "entity_label": "V_launch_participant_current_status",
        "generated_vocabulary_evidence": [{"original_term": "launch participants", "generated_term": "launch_participant"}],
        "group_by": [{"alias": "status", "column_name": "status"}],
        "select": [{"alias": "status"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "dimension_labels": {"status": "Current Status"}, "where": [],
        "source_tables": ["dbo.V_launch_participant_current_status"],
    }
    rows = [
        {"status": "Stalled", "count_id": 83}, {"status": "Graduated", "count_id": 76},
        {"status": "Active", "count_id": 5}, {"status": "Not Started", "count_id": 3},
    ]
    data = _base_data(rows=rows, row_count=4, business_plan=plan)
    answer = build_business_answer(data)
    assert answer["answer"] == (
        "Most launch participants are Stalled or Graduated. "
        "The current breakdown is 83 stalled, 76 graduated, 5 active, and 3 not started."
    )


def test_scalar_count_with_date_context_humanizes_underscored_label_in_prose_only():
    plan = {
        "aggregation": "COUNT", "entity_label": "vw_Clients",
        "generated_vocabulary_evidence": [{"original_term": "clients", "generated_term": "client"}],
        "group_by": [], "where": [], "select": [{"alias": "count_id", "aggregation": "COUNT"}],
        "source_tables": ["dbo.vw_Clients"],
        "date_context": {"label": "last_quarter", "start": "2026-04-01", "end": "2026-06-30"},
    }
    data = _base_data(rows=[{"count_id": 6}], business_plan=plan)
    answer = build_business_answer(data)
    assert answer["answer"] == "There were 6 clients last quarter."
    # The raw, underscored label is untouched in date_context itself.
    assert answer["date_context"]["label"] == "last_quarter"


def test_grouped_time_series_with_no_vocabulary_evidence_still_reads_naturally():
    # Regression for the exact live bug: "Adf student peaked in 2023 with
    # 1,798 adf student." — a grouped query sometimes carries no
    # vocabulary evidence even when a scalar query against the same table
    # did; the fallback noun must still be natural business language.
    plan = {
        "aggregation": "COUNT", "entity_label": "ADF_Student",
        "group_by": [{"alias": "year", "column_name": "year", "time_grain": "year"}],
        "select": [{"alias": "year"}, {"alias": "count_id", "aggregation": "COUNT"}],
        "dimension_labels": {"year": "Start Date"}, "where": [],
        "source_tables": ["dbo.ADF_Student"],
    }
    rows = [{"year": 2012, "count_id": 140}, {"year": 2023, "count_id": 1798}, {"year": 2026, "count_id": 60}]
    data = _base_data(rows=rows, row_count=3, business_plan=plan)
    answer = build_business_answer(data)
    assert answer["answer"] == (
        "Students peaked in 2023 with 1,798 students. "
        "The dataset shows 3 yearly groups from 2012 through 2026."
    )


def test_never_changes_numeric_values_across_shapes():
    # Scalar sum
    plan = {"aggregation": "SUM", "measure_label": "Invoice Amount", "entity_label": "invoices", "group_by": [], "where": []}
    data = _base_data(rows=[{"count_id": 123456.789}], business_plan=plan)
    answer = build_business_answer(data)
    assert answer["actual_value"] == 123456.789


def test_refusal_and_clarification_paths_unaffected_not_covered_by_this_module():
    # build_business_answer is only reached on a successful execution;
    # refusal/clarification wording lives entirely in
    # core.answering.explanation_builder, untouched by this capability —
    # this test just documents that boundary.
    import inspect
    from core.answering import result_formatter
    assert "clarification" not in inspect.getsource(result_formatter.build_business_answer).lower()
