"""
CURRENT-STATE MILESTONE, Parts 2-3 — current-state phrase detection and
schema-driven candidate discovery. No database, no live connection:
detect_current_state/extract_query_intent are pure regex; discover_current_
state_candidates is a pure function over hand-built table_contexts dicts
shaped exactly like data.business_knowledge_service.get_table_business_
context's own output (mirrors tests/test_data_quality_observations.py's own
fixture style).
"""
from __future__ import annotations

from unittest.mock import patch

from core.answering.current_state_semantics import (
    CurrentStateCandidate,
    candidate_already_satisfied,
    discover_current_state_candidates,
    plan_table_fqns,
)
from core.semantic.concept_resolver import extract_query_intent, extract_terms


def test_currently_enrolled_sets_current_state_required():
    intent = extract_query_intent("How many students are currently enrolled in each program?")
    assert intent["current_state_required"] is True
    assert intent["current_state_phrase"] == "currently enrolled"
    assert intent["current_state_subject"] == "enrolled"


def test_still_active_detected():
    intent = extract_query_intent("List clients who are still active")
    assert intent["current_state_required"] is True
    assert intent["current_state_subject"] == "active"


def test_plain_question_has_no_current_state_requirement():
    intent = extract_query_intent("How many students are in each program?")
    assert intent["current_state_required"] is False
    assert intent["current_state_phrase"] is None
    assert intent["current_state_subject"] is None


def test_currently_and_subject_stripped_from_search_terms():
    # Previously "currently"/"enrolled" survived as bogus measure-search
    # terms (see core.orchestrator.context_builder's own docstring for the
    # live-confirmed regression) — both words must now be excluded entirely,
    # the same "only the matched phrase's own words are removed" discipline
    # _date_phrase_words/_idiom_phrase_words already use.
    concepts, measures, dimensions = extract_terms(
        "How many students are currently enrolled in each program?"
    )
    assert "currently" not in concepts and "currently" not in measures
    assert "enrolled" not in concepts and "enrolled" not in measures
    assert "students" in concepts
    assert dimensions == ["program"]


def _ctx(table_fqn, columns, *, business_name=None):
    return {
        table_fqn: {
            "dictionary": {"business_name": business_name} if business_name else {},
            "entity": {},
            "columns": columns,
        },
    }


def _col(name, *, nullable=True, data_type="DATETIME"):
    return {
        "column_name": name,
        "schema": {"data_type": data_type, "is_nullable": nullable},
        "dictionary": {},
        "profiling": {},
    }


def test_discovers_not_cancelled_and_not_ended_candidates_by_name_token():
    table_contexts = _ctx("dbo.EDU_ClassSignups", [
        _col("CancelID", data_type="INTEGER"),
        _col("EndDate"),
        _col("EnrollmentDate"),  # contains "enroll" -> excluded from not_ended via START_TOKENS
    ])
    with patch(
        "data.query_planning_service._compatible_status_filter", return_value=None,
    ):
        candidates = discover_current_state_candidates(
            source_id=1, table_contexts=table_contexts,
            candidate_table_fqns=["dbo.EDU_ClassSignups"],
        )

    kinds = {c.kind: c for c in candidates}
    assert "not_cancelled" in kinds
    assert kinds["not_cancelled"].column_name == "CancelID"
    assert kinds["not_cancelled"].operator == "IS NULL"
    assert "not_ended" in kinds
    assert kinds["not_ended"].column_name == "EndDate"
    assert all(c.column_name != "EnrollmentDate" for c in candidates)


def test_not_nullable_column_never_becomes_a_candidate():
    table_contexts = _ctx("dbo.EDU_ClassSignups", [_col("CancelID", nullable=False, data_type="INTEGER")])
    with patch("data.query_planning_service._compatible_status_filter", return_value=None):
        candidates = discover_current_state_candidates(
            source_id=1, table_contexts=table_contexts, candidate_table_fqns=["dbo.EDU_ClassSignups"],
        )
    assert candidates == []


def test_active_flag_candidate_reuses_compatible_status_filter():
    table_contexts = _ctx("dbo.EDU_Class", [_col("IsActive", data_type="BOOLEAN")], business_name="Class")
    with patch(
        "data.query_planning_service._compatible_status_filter",
        return_value=("dbo.EDU_Class", "IsActive", "=", 1),
    ):
        candidates = discover_current_state_candidates(
            source_id=1, table_contexts=table_contexts, candidate_table_fqns=["dbo.EDU_Class"],
        )
    assert len(candidates) == 1
    assert candidates[0].kind == "active_flag"
    assert candidates[0].operator == "="
    assert candidates[0].value == 1
    assert "Class" in candidates[0].label


def test_candidate_already_satisfied_true_when_filter_matches():
    candidates = [CurrentStateCandidate("dbo.T", "EndDate", "not_ended", "IS NULL", None, "x")]
    existing = [{"table_fqn": "dbo.T", "column": "EndDate", "operator": "IS NULL", "resolved": True}]
    assert candidate_already_satisfied(candidates, existing) is True


def test_candidate_already_satisfied_false_when_no_match():
    candidates = [CurrentStateCandidate("dbo.T", "EndDate", "not_ended", "IS NULL", None, "x")]
    assert candidate_already_satisfied(candidates, []) is False


def test_plan_table_fqns_includes_join_hops():
    query_plan = {
        "tables": ["dbo.EDU_Student", "dbo.EDU_Track"],
        "join_plan": {"steps": [
            {"from_table": "dbo.EDU_Student", "to_table": "dbo.EDU_ClassSignups"},
            {"from_table": "dbo.EDU_ClassSignups", "to_table": "dbo.EDU_Class"},
            {"from_table": "dbo.EDU_Class", "to_table": "dbo.EDU_Track"},
        ]},
    }
    fqns = plan_table_fqns(query_plan)
    assert set(fqns) == {
        "dbo.EDU_Student", "dbo.EDU_Track", "dbo.EDU_ClassSignups", "dbo.EDU_Class",
    }
