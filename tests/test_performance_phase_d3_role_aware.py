"""
PERFORMANCE PHASE D3 — role context threading + role-aware multi-candidate
dominance gate (shadow-only; _ROLE_SAFE_FOR_DOMINANCE is empty by default,
so no production behavior changes).

Real shadow-mode evidence against the final 37 (see the Phase D3 report)
found: ENTITY role near-complete deterministic-#1 agreement (one real false-safe,
"course"->EDU_Track when the correct answer was EDU_Course); MEASURE partial agreement
(the known "fee" danger case); DIMENSION/RELATIONSHIP_ENTITY had small or
zero eligible-case coverage. Critically, the one false-safe ("course") is
STRUCTURALLY IDENTICAL on every available signal (name_score=0.5,
candidate_business_role=catalog_or_entity, rank#1) to multiple genuinely
safe cases ("class") -- no scalar threshold or role restriction separates
them. These tests prove the MECHANISM (role propagation, gating logic,
protections) is correct, not that any role is safe to activate.

Run:
    venv/Scripts/python.exe -m pytest tests/test_performance_phase_d3_role_aware.py -v
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-perf-phase-d3-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-perf-phase-d3-salt-long-enough-1234567890ab")

import core.semantic.candidate_adjudicator as ca
from core.semantic.candidate_adjudicator import (
    ROLE_DIMENSION,
    ROLE_ENTITY,
    ROLE_MEASURE,
    ROLE_RELATIONSHIP_ENTITY,
    _evaluate_role_dominance_candidate,
    adjudicate_candidates,
    disable_shadow_log,
    enable_shadow_log,
    get_shadow_log,
)


def _cand(
    table_fqn="dbo.EDU_Track", *, rank=1, role="catalog_or_entity", name_score=0.72, score=0.72,
    derivative_flag=None, object_type="TABLE", restricted_view_evidence=None,
    approx_row_count=103,
):
    return {
        "table_fqn": table_fqn, "object_type": object_type, "deterministic_rank": rank,
        "candidate_business_role": role, "name_score": name_score, "score": score,
        "derivative_flag": derivative_flag, "restricted_view_evidence": restricted_view_evidence,
        "approx_row_count": approx_row_count,
    }


def setup_function(_fn):
    disable_shadow_log()
    ca._FAST_PATH_ACTIVE = False
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset()


def teardown_function(_fn):
    disable_shadow_log()
    ca._FAST_PATH_ACTIVE = False
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset()


# ---------------------------------------------------------------------------
# Role gate mechanism
# ---------------------------------------------------------------------------

def test_role_not_in_safe_set_defers_to_ai():
    result, reason = _evaluate_role_dominance_candidate("class", [_cand()], None, ROLE_ENTITY)
    assert result is None
    assert "not in empirically-safe role set" in reason


def test_role_in_safe_set_with_good_evidence_is_eligible():
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    result, reason = _evaluate_role_dominance_candidate(
        "class", [_cand("dbo.EDU_Class", rank=1, role="catalog_or_entity", name_score=0.5)], None, ROLE_ENTITY,
    )
    assert result is not None, reason
    assert result.selected_table_fqn == "dbo.EDU_Class"


def test_the_real_false_safe_case_reproduced_and_structurally_identical_to_safe_case():
    # "course" (unsafe, real false-safe) vs "class" (safe, real match) --
    # both name_score=0.5, both catalog_or_entity, both rank#1 -- byte-
    # identical on every field this gate inspects. This test documents,
    # rather than prevents, the finding: the mechanism CANNOT distinguish
    # them, which is exactly why no role was activated.
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    course_cand = _cand("dbo.EDU_Track", rank=1, role="catalog_or_entity", name_score=0.5, score=1.0)
    class_cand = _cand("dbo.EDU_Class", rank=1, role="catalog_or_entity", name_score=0.5, score=0.88)

    course_result, _ = _evaluate_role_dominance_candidate("course", [course_cand], None, ROLE_ENTITY)
    class_result, _ = _evaluate_role_dominance_candidate("class", [class_cand], None, ROLE_ENTITY)

    # Both are ELIGIBLE (the gate cannot tell them apart) -- course_result
    # proposes EDU_Track, which real production data proves is WRONG (the
    # real answer was EDU_Course). This is exactly why ROLE_ENTITY is not
    # in the activated set.
    assert course_result is not None
    assert course_result.selected_table_fqn == "dbo.EDU_Track"  # the false-safe proposal
    assert class_result is not None
    assert class_result.selected_table_fqn == "dbo.EDU_Class"  # the genuinely correct proposal


def test_weak_name_score_protects_program_and_fee_cases():
    # The EXISTING _FAST_PATH_MIN_NAME_SCORE gate (reused, not new) already
    # independently protects both real "program" (DIMENSION) and "fee"
    # (MEASURE) danger cases -- both have name_score=0.0 in real data.
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_DIMENSION, ROLE_MEASURE})
    program_cand = _cand("dbo.EDU_Track", role="catalog_or_entity", name_score=0.0, score=0.736)
    fee_cand = _cand("dbo.EDU_Class", role="catalog_or_entity", name_score=0.0, score=0.664)

    program_result, program_reason = _evaluate_role_dominance_candidate("program", [program_cand], None, ROLE_DIMENSION)
    fee_result, fee_reason = _evaluate_role_dominance_candidate("fee", [fee_cand], None, ROLE_MEASURE)

    assert program_result is None
    assert "name_score" in program_reason
    assert fee_result is None
    assert "name_score" in fee_reason


def test_unknown_business_role_ineligible_even_when_role_context_safe():
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_DIMENSION})
    location_cand = _cand("dbo.EDU_Location", role="unknown", name_score=0.5)
    result, reason = _evaluate_role_dominance_candidate("location", [location_cand], None, ROLE_DIMENSION)
    assert result is None
    assert "candidate_business_role" in reason


def test_assignment_role_with_better_catalog_alternative_ineligible():
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    weak = _cand("dbo.EDU_Mentoring_Program", rank=1, role="assignment_or_event", name_score=0.8)
    strong_catalog = _cand("dbo.EDU_Track", rank=2, role="catalog_or_entity", name_score=0.2)
    result, reason = _evaluate_role_dominance_candidate(
        "program", [weak, strong_catalog], None, ROLE_ENTITY,
    )
    assert result is None
    assert "catalog_or_entity alternative present" in reason


def test_assignment_role_with_no_catalog_alternative_still_eligible():
    # Mirrors real "student"/"enrollment" safe shape: assignment_or_event
    # rank#1, no catalog_or_entity competitor anywhere in evidence.
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    student = _cand("dbo.EDU_Student", rank=1, role="assignment_or_event", name_score=0.5)
    other = _cand("dbo.EDU_StudentSurveyScores", rank=2, role="unknown", name_score=0.25)
    result, reason = _evaluate_role_dominance_candidate("student", [student, other], None, ROLE_ENTITY)
    assert result is not None, reason
    assert result.selected_table_fqn == "dbo.EDU_Student"


def test_restricted_view_rank1_ineligible():
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    restricted = _cand(
        "dbo.vw_EDU_Enrollments", object_type="VIEW",
        restricted_view_evidence={"restriction_detected": True, "restriction_kinds": ["where_literal_filter"]},
    )
    result, reason = _evaluate_role_dominance_candidate("enrollment", [restricted], None, ROLE_ENTITY)
    assert result is None
    assert "restricted view" in reason


def test_zero_row_rank1_ineligible():
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    result, reason = _evaluate_role_dominance_candidate(
        "x", [_cand(approx_row_count=0)], None, ROLE_ENTITY,
    )
    assert result is None
    assert "source-data-unusable" in reason


def test_requested_attributes_present_ineligible():
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    result, reason = _evaluate_role_dominance_candidate("student", [_cand("dbo.EDU_Student")], ["email"], ROLE_ENTITY)
    assert result is None
    assert "requested_attributes" in reason


# ---------------------------------------------------------------------------
# Wiring — role propagation, cache key, shadow logging, no extra AI calls
# ---------------------------------------------------------------------------

def test_role_context_recorded_in_shadow_log(monkeypatch):
    enable_shadow_log()
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "test-key")

    class _FakeResponse:
        class _Choice:
            class _Msg:
                content = '{"selected_table_fqn": "dbo.EDU_Track", "confidence": 0.9}'
            message = _Msg()
        choices = [_Choice()]

    class _FakeClient:
        def __init__(self, **kw): pass
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return _FakeResponse()

    monkeypatch.setattr(ca, "_openai", type("M", (), {"OpenAI": _FakeClient}))
    monkeypatch.setattr(ca, "_OPENAI_AVAILABLE", True)

    adjudicate_candidates(
        "program", [_cand()], None, simple_selection_only=True, role_context=ROLE_DIMENSION,
    )
    log = get_shadow_log()
    assert len(log) == 1
    assert log[0]["role_context"] == ROLE_DIMENSION


def test_role_context_never_active_still_calls_ai_even_if_role_marked_safe(monkeypatch):
    # _FAST_PATH_ACTIVE stays False in this milestone's shipped state --
    # even marking a role as empirically-safe (as a test would) must never
    # skip the AI call while the module-level switch is off.
    ca._ROLE_SAFE_FOR_DOMINANCE = frozenset({ROLE_ENTITY})
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "test-key")
    calls = []

    class _FakeResponse:
        class _Choice:
            class _Msg:
                content = '{"selected_table_fqn": "dbo.EDU_Class", "confidence": 0.9}'
            message = _Msg()
        choices = [_Choice()]

    class _FakeClient:
        def __init__(self, **kw): pass
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(1)
                    return _FakeResponse()

    monkeypatch.setattr(ca, "_openai", type("M", (), {"OpenAI": _FakeClient}))
    monkeypatch.setattr(ca, "_OPENAI_AVAILABLE", True)

    result = adjudicate_candidates(
        "class", [_cand("dbo.EDU_Class")], None, simple_selection_only=True, role_context=ROLE_ENTITY,
    )
    assert len(calls) == 1  # AI still called -- _FAST_PATH_ACTIVE is False
    assert result.selected_table_fqn == "dbo.EDU_Class"


def test_role_included_in_cache_key_prevents_cross_role_conflation(tmp_path, monkeypatch):
    from data.request_metadata_session import RequestMetadataSession
    from tests.test_phase9_query_planning import env

    env(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "test-key")
    calls = []

    class _FakeResponse:
        class _Choice:
            class _Msg:
                content = '{"selected_table_fqn": "dbo.EDU_Track", "confidence": 0.9}'
            message = _Msg()
        choices = [_Choice()]

    class _FakeClient:
        def __init__(self, **kw): pass
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(1)
                    return _FakeResponse()

    monkeypatch.setattr(ca, "_openai", type("M", (), {"OpenAI": _FakeClient}))
    monkeypatch.setattr(ca, "_OPENAI_AVAILABLE", True)

    session = RequestMetadataSession(1, "u1")
    try:
        adjudicate_candidates(
            "program", [_cand()], None, session=session, simple_selection_only=True, role_context=ROLE_ENTITY,
        )
        adjudicate_candidates(
            "program", [_cand()], None, session=session, simple_selection_only=True, role_context=ROLE_DIMENSION,
        )
    finally:
        session.close()

    # Identical entity_name and evidence but DIFFERENT role_context ->
    # two separate cache entries -> two real calls, never one cached hit
    # silently reused across roles.
    assert len(calls) == 2


def test_no_extra_ai_calls_from_role_evaluation(monkeypatch):
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "test-key")
    calls = []

    class _FakeResponse:
        class _Choice:
            class _Msg:
                content = '{"selected_table_fqn": "dbo.A", "confidence": 0.9}'
            message = _Msg()
        choices = [_Choice()]

    class _FakeClient:
        def __init__(self, **kw): pass
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(1)
                    return _FakeResponse()

    monkeypatch.setattr(ca, "_openai", type("M", (), {"OpenAI": _FakeClient}))
    monkeypatch.setattr(ca, "_OPENAI_AVAILABLE", True)

    adjudicate_candidates(
        "x", [_cand("dbo.A")], None, simple_selection_only=True, role_context=ROLE_MEASURE,
    )
    assert len(calls) == 1
