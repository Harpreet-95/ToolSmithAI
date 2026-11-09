"""
Tests for core/semantic/candidate_adjudicator.py — Day 2C, Task 2 (AI-
Assisted Candidate Adjudication).

Mirrors tests/test_ai_question_interpreter.py's structure:
  - Strict schema validation, scoped to the evidence given: an identifier
    (table_fqn/column_name) not literally present in the evidence raises
    SchemaViolation.
  - adjudicate_candidates() fail-closed contract: no API key, no openai
    package, empty evidence, API exception, malformed JSON, and schema/
    identifier violations all return None rather than raising.
  - adjudicate_candidates() happy path: mocked OpenAI client -> validated
    AdjudicationResult, JSON mode + temperature=0 enforced.

Run:
    python -m pytest tests/test_candidate_adjudicator.py -v
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.semantic.candidate_adjudicator import (
    AdjudicationResult,
    SchemaViolation,
    _apply_business_role_backstop,
    _apply_restricted_view_backstop,
    _validate,
    adjudicate_candidates,
)


def _evidence():
    return [
        {
            "table_fqn": "dbo.EDU_Pilot_Participants",
            "object_type": "TABLE",
            "business_name": "Adf Cap Pilot Participants",
            "description": None,
            "columns": [
                {"column_name": "LaunchParticipantID", "data_type": "int", "is_primary_key": True,
                 "is_metric": False, "is_dimension": False, "is_date": False},
                {"column_name": "Status", "data_type": "varchar", "is_primary_key": False,
                 "is_metric": False, "is_dimension": True, "is_date": False},
            ],
            "primary_key": {"column_name": "LaunchParticipantID"},
            "approx_row_count": 500,
            "max_column_uniqueness": 1.0,
            "is_root_table": False,
            "is_leaf_table": True,
            "relationships": [{"table_fqn": "dbo.EDU_Student", "relationship_types": ["FK_OUTBOUND"], "confidence": 0.9}],
            "domain": "Launch",
            "entity": "Pilot Participant",
            "status_evidence": {"column_name": "Status", "verified_values": ["Active", "Graduated"], "value_source": "cached"},
            "derivative_flag": None,
            "name_score": 0.8,
            "score": 0.85,
        },
        {
            "table_fqn": "dbo.EDU_Student",
            "object_type": "TABLE",
            "business_name": "Adf Student",
            "description": "General student roster",
            "columns": [
                {"column_name": "StudentUserID", "data_type": "int", "is_primary_key": True,
                 "is_metric": False, "is_dimension": False, "is_date": False},
            ],
            "primary_key": {"column_name": "StudentUserID"},
            "approx_row_count": 50000,
            "max_column_uniqueness": 1.0,
            "is_root_table": True,
            "is_leaf_table": False,
            "relationships": [],
            "domain": "Student",
            "entity": "Student",
            "status_evidence": None,
            "derivative_flag": None,
            "name_score": 0.96,
            "score": 0.96,
        },
    ]


def _mock_client_returning(content: str):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=content))]
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


# ---------------------------------------------------------------------------
# Schema / identifier validation
# ---------------------------------------------------------------------------

def test_validate_accepts_well_formed_response():
    raw = {
        "selected_table_fqn": "dbo.EDU_Pilot_Participants",
        "no_safe_selection": False,
        "preferred_analytical_view_fqn": None,
        "grain": "One row per launch participant.",
        "identity_key": "LaunchParticipantID",
        "key_attributes": ["Status"],
        "status_semantics": {"column_name": "Status", "verified_values": ["Active", "Graduated"]},
        "date_semantics": {},
        "relationships": ["dbo.EDU_Student"],
        "excluded_candidates": [{"table_fqn": "dbo.EDU_Student", "reason": "General roster, not launch-specific."}],
        "confidence": 0.9,
        "evidence_references": ["dbo.EDU_Pilot_Participants has a verified Status column."],
    }
    result = _validate(raw, _evidence())
    assert isinstance(result, AdjudicationResult)
    assert result.selected_table_fqn == "dbo.EDU_Pilot_Participants"
    assert result.identity_key == "LaunchParticipantID"
    assert result.key_attributes == ("Status",)
    assert result.status_semantics == {"column_name": "Status", "verified_values": ["Active", "Graduated"]}
    assert result.relationships == ("dbo.EDU_Student",)
    assert result.confidence == 0.9


def test_validate_accepts_no_safe_selection():
    result = _validate({"no_safe_selection": True}, _evidence())
    assert result.no_safe_selection is True
    assert result.selected_table_fqn is None


def test_validate_requires_selection_or_no_safe_selection():
    with pytest.raises(SchemaViolation):
        _validate({}, _evidence())


def test_validate_rejects_hallucinated_table_fqn():
    with pytest.raises(SchemaViolation):
        _validate({"selected_table_fqn": "dbo.MadeUpTable"}, _evidence())


def test_validate_rejects_hallucinated_preferred_view():
    with pytest.raises(SchemaViolation):
        _validate(
            {"selected_table_fqn": "dbo.EDU_Student", "preferred_analytical_view_fqn": "dbo.NotReal"},
            _evidence(),
        )


def test_validate_drops_invalid_identity_key_instead_of_failing():
    # V1.3 — identity_key is a supplementary annotation ABOUT the already-
    # validated selected_table_fqn (it doesn't define WHAT was selected),
    # and is only ever consumed downstream as "use if present and valid,
    # else None" (data.semantic_contract_service.py re-checks it against
    # real column names before use) — so a hallucinated value must degrade
    # the same way status_semantics/date_semantics already do: dropped,
    # result still returned, never discarding an otherwise-valid table
    # selection. Reproduces the live, recurring failure this milestone
    # traced (entity=employee, identity_key='D-LocationCode', not a real
    # column on the selected candidate).
    result = _validate(
        {"selected_table_fqn": "dbo.EDU_Student", "identity_key": "LaunchParticipantID"},
        _evidence(),
    )
    assert result.selected_table_fqn == "dbo.EDU_Student"
    assert result.identity_key is None


def test_validate_drops_invalid_key_attribute_instead_of_failing():
    # V1.3 — key_attributes is a supplementary display-attribute list ABOUT
    # the already-validated selected_table_fqn, not something that defines
    # WHAT was selected; a hallucinated entry degrades the same way
    # identity_key/status_semantics/date_semantics already do (dropped,
    # result still returned) rather than discarding an otherwise-valid
    # table selection. Reproduces the live, recurring failure this
    # milestone traced (entity=employee, key_attributes containing
    # 'In-FirstName', not a real column).
    result = _validate(
        {"selected_table_fqn": "dbo.EDU_Student", "key_attributes": ["Status"]},
        _evidence(),
    )
    assert result.selected_table_fqn == "dbo.EDU_Student"
    assert result.key_attributes == ()


def test_validate_keeps_valid_key_attributes_and_drops_only_the_bad_one():
    result = _validate(
        {
            "selected_table_fqn": "dbo.EDU_Pilot_Participants",
            "key_attributes": ["Status", "NotARealColumn"],
        },
        _evidence(),
    )
    assert result.key_attributes == ("Status",)


def test_validate_drops_unverified_status_value_instead_of_failing():
    # status_semantics is documented as "omit if it does not apply" — an
    # invalid value must degrade (dropped, result still returned), never
    # discard an otherwise-valid table selection (see candidate_adjudicator's
    # own comment on this block).
    result = _validate(
        {
            "selected_table_fqn": "dbo.EDU_Pilot_Participants",
            "status_semantics": {"column_name": "Status", "verified_values": ["MadeUpValue"]},
        },
        _evidence(),
    )
    assert result.selected_table_fqn == "dbo.EDU_Pilot_Participants"
    assert result.status_semantics is None


def test_validate_drops_invalid_date_role_instead_of_failing():
    result = _validate(
        {"selected_table_fqn": "dbo.EDU_Student", "date_semantics": {"not_a_role": "StudentUserID"}},
        _evidence(),
    )
    assert result.selected_table_fqn == "dbo.EDU_Student"
    assert result.date_semantics == {}


def test_validate_drops_only_the_invalid_date_semantics_entry():
    result = _validate(
        {
            "selected_table_fqn": "dbo.EDU_Student",
            "date_semantics": {"started": "StudentUserID", "not_a_role": "StudentUserID"},
        },
        _evidence(),
    )
    assert result.date_semantics == {"started": "StudentUserID"}


def test_validate_rejects_relationship_outside_evidence():
    with pytest.raises(SchemaViolation):
        _validate(
            {"selected_table_fqn": "dbo.EDU_Student", "relationships": ["dbo.NeverMentioned"]},
            _evidence(),
        )


def test_validate_rejects_excluded_candidate_outside_evidence():
    with pytest.raises(SchemaViolation):
        _validate(
            {
                "selected_table_fqn": "dbo.EDU_Student",
                "excluded_candidates": [{"table_fqn": "dbo.NeverMentioned", "reason": "x"}],
            },
            _evidence(),
        )


def test_validate_clamps_confidence_to_unit_range():
    result = _validate({"selected_table_fqn": "dbo.EDU_Student", "confidence": 5}, _evidence())
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# adjudicate_candidates() — fail-closed contract
# ---------------------------------------------------------------------------

def test_adjudicate_returns_none_without_api_key():
    assert adjudicate_candidates("Pilot Participant", _evidence(), api_key="") is None


def test_adjudicate_returns_none_on_empty_evidence():
    assert adjudicate_candidates("Pilot Participant", [], api_key="sk-test") is None


def test_adjudicate_returns_none_when_openai_package_missing():
    with patch("core.semantic.candidate_adjudicator._OPENAI_AVAILABLE", False):
        assert adjudicate_candidates("Pilot Participant", _evidence(), api_key="sk-test") is None


def test_adjudicate_returns_none_on_api_exception():
    with patch("core.semantic.candidate_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.side_effect = RuntimeError("boom")
        assert adjudicate_candidates("Pilot Participant", _evidence(), api_key="sk-test") is None


def test_adjudicate_returns_none_on_hallucinated_identifier():
    mock_client = _mock_client_returning(json.dumps({"selected_table_fqn": "dbo.MadeUpTable"}))
    with patch("core.semantic.candidate_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        assert adjudicate_candidates("Pilot Participant", _evidence(), api_key="sk-test") is None


def test_adjudicate_survives_hallucinated_status_semantics():
    # Regression test for the adjudication_unavailable_or_failed bug: a bad
    # status_semantics value must no longer nuke an otherwise-valid table
    # selection (e.g. "How many students are there?" against dbo.EDU_Student,
    # which has status_evidence=None in this fixture).
    response = {
        "selected_table_fqn": "dbo.EDU_Student",
        "status_semantics": {"column_name": "NotARealColumn", "verified_values": ["Active"]},
        "confidence": 0.9,
    }
    mock_client = _mock_client_returning(json.dumps(response))
    with patch("core.semantic.candidate_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = adjudicate_candidates("Student", _evidence(), api_key="sk-test")

    assert result is not None
    assert result.selected_table_fqn == "dbo.EDU_Student"
    assert result.status_semantics is None


def test_adjudicate_happy_path_selects_launch_specific_table_over_student():
    response = {
        "selected_table_fqn": "dbo.EDU_Pilot_Participants",
        "preferred_analytical_view_fqn": None,
        "grain": "One row per launch participant.",
        "identity_key": "LaunchParticipantID",
        "status_semantics": {"column_name": "Status", "verified_values": ["Active", "Graduated"]},
        "relationships": ["dbo.EDU_Student"],
        "excluded_candidates": [{"table_fqn": "dbo.EDU_Student", "reason": "General student roster, not launch-specific."}],
        "confidence": 0.9,
    }
    mock_client = _mock_client_returning(json.dumps(response))
    with patch("core.semantic.candidate_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = adjudicate_candidates("Pilot Participant", _evidence(), api_key="sk-test")

    assert result is not None
    assert result.selected_table_fqn == "dbo.EDU_Pilot_Participants"
    assert result.excluded_candidates == (
        {"table_fqn": "dbo.EDU_Student", "reason": "General student roster, not launch-specific."},
    )
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# LIVE PROGRAM ADJUDICATION MILESTONE, Part 6 — deterministic backstop.
#
# Reproduces the exact live regression: "program" evidence contains a
# higher-ranked catalog_or_entity candidate (dbo.EDU_Track-shaped) and a
# lower-ranked assignment_or_event candidate (dbo.EDU_Mentoring_Program-
# shaped) whose table name happens to lexically contain "program" — the
# model selects the assignment table anyway.
# ---------------------------------------------------------------------------

def _program_evidence():
    return [
        {
            "table_fqn": "dbo.EDU_Track", "deterministic_rank": 1,
            "candidate_business_role": "catalog_or_entity",
            "human_readable_descriptor_columns": ["TrackName", "TrackDesc"],
            "person_reference_columns": [],
        },
        {
            "table_fqn": "dbo.EDU_Mentoring_Program", "deterministic_rank": 2,
            "candidate_business_role": "assignment_or_event",
            "human_readable_descriptor_columns": [],
            "person_reference_columns": ["StudentUserID", "InstructorID"],
        },
    ]


def _result(selected_fqn, *, relationships=(), excluded_candidates=(), no_safe_selection=False):
    return AdjudicationResult(
        selected_table_fqn=selected_fqn, no_safe_selection=no_safe_selection,
        relationships=tuple(relationships), excluded_candidates=tuple(excluded_candidates),
        confidence=0.7,
    )


def test_backstop_rejects_unacknowledged_assignment_table_over_better_catalog():
    result = _result("dbo.EDU_Mentoring_Program")
    outcome = _apply_business_role_backstop(result, _program_evidence())
    assert outcome is None


def test_backstop_allows_assignment_table_when_catalog_alternative_acknowledged_in_relationships():
    result = _result("dbo.EDU_Mentoring_Program", relationships=["dbo.EDU_Track"])
    outcome = _apply_business_role_backstop(result, _program_evidence())
    assert outcome is result


def test_backstop_allows_assignment_table_when_catalog_alternative_in_excluded_candidates():
    result = _result(
        "dbo.EDU_Mentoring_Program",
        excluded_candidates=[{"table_fqn": "dbo.EDU_Track", "reason": "Curriculum catalog, not this question's concept."}],
    )
    outcome = _apply_business_role_backstop(result, _program_evidence())
    assert outcome is result


def test_backstop_allows_catalog_selection_unconditionally():
    result = _result("dbo.EDU_Track")
    outcome = _apply_business_role_backstop(result, _program_evidence())
    assert outcome is result


def test_backstop_allows_assignment_table_when_no_catalog_alternative_exists():
    only_assignment = [_program_evidence()[1]]  # just dbo.EDU_Mentoring_Program
    result = _result("dbo.EDU_Mentoring_Program")
    outcome = _apply_business_role_backstop(result, only_assignment)
    assert outcome is result


def test_backstop_never_touches_no_safe_selection():
    result = _result(None, no_safe_selection=True)
    outcome = _apply_business_role_backstop(result, _program_evidence())
    assert outcome is result


def test_backstop_never_forces_a_different_table():
    # Part 6: "Do NOT simply force candidate A" -- the backstop only ever
    # returns the original result unchanged or None, never a substituted
    # selected_table_fqn.
    result = _result("dbo.EDU_Mentoring_Program")
    outcome = _apply_business_role_backstop(result, _program_evidence())
    assert outcome is None or outcome.selected_table_fqn == "dbo.EDU_Mentoring_Program"


def test_adjudicate_candidates_applies_backstop_end_to_end():
    response = {
        "selected_table_fqn": "dbo.EDU_Mentoring_Program",
        "grain": "One row per mentorship assignment.",
        "confidence": 0.7,
    }
    mock_client = _mock_client_returning(json.dumps(response))
    with patch("core.semantic.candidate_adjudicator._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = adjudicate_candidates("program", _program_evidence(), api_key="sk-test")
    assert result is None  # backstop rejected it -- caller falls back to safe disqualification


# ---------------------------------------------------------------------------
# 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — restricted-view backstop.
#
# Reproduces the exact live regression: "enrollments" evidence contains a
# lexically-dominant restricted VIEW (dbo.vw_EDU_Enrollments, whose own
# definition is scoped to WHERE TrackID IN (1000)) and, alongside it, the
# broader base table data.semantic_contract_service.gather_candidate_
# evidence's own lineage extraction already added
# (lineage_source_view == the restricted view's fqn) — the model selects
# the restricted view anyway.
# ---------------------------------------------------------------------------

def _enrollment_evidence():
    return [
        {
            "table_fqn": "dbo.vw_EDU_Enrollments",
            "restricted_view_evidence": {
                "restriction_detected": True, "restriction_kinds": ["where_literal_filter"],
            },
            "lineage_source_view": None,
        },
        {
            "table_fqn": "dbo.EDU_ClassSignups",
            "restricted_view_evidence": None,
            "lineage_source_view": "dbo.vw_EDU_Enrollments",
            "primary_key": {"column_name": "ClassSignupsID"},
            "human_readable_descriptor_columns": ["ClassName"],
        },
    ]


def test_restricted_view_backstop_substitutes_verified_lineage_source():
    result = _result("dbo.vw_EDU_Enrollments")
    outcome = _apply_restricted_view_backstop(result, _enrollment_evidence())
    assert outcome is not None
    assert outcome.selected_table_fqn == "dbo.EDU_ClassSignups"
    assert outcome.preferred_analytical_view_fqn == "dbo.vw_EDU_Enrollments"
    assert outcome.identity_key == "ClassSignupsID"


def test_restricted_view_backstop_rejects_when_no_lineage_source():
    only_view = [_enrollment_evidence()[0]]  # just the restricted view, no lineage candidate
    result = _result("dbo.vw_EDU_Enrollments")
    outcome = _apply_restricted_view_backstop(result, only_view)
    assert outcome is None


def test_restricted_view_backstop_rejects_ambiguous_lineage():
    two_roots = _enrollment_evidence() + [{
        "table_fqn": "dbo.EDU_Student",
        "restricted_view_evidence": None,
        "lineage_source_view": "dbo.vw_EDU_Enrollments",
    }]
    result = _result("dbo.vw_EDU_Enrollments")
    outcome = _apply_restricted_view_backstop(result, two_roots)
    assert outcome is None


def test_restricted_view_backstop_leaves_unrestricted_selection_untouched():
    result = _result("dbo.EDU_ClassSignups")
    outcome = _apply_restricted_view_backstop(result, _enrollment_evidence())
    assert outcome is result


def test_restricted_view_backstop_never_touches_no_safe_selection():
    result = _result(None, no_safe_selection=True)
    outcome = _apply_restricted_view_backstop(result, _enrollment_evidence())
    assert outcome is result
