"""
PERFORMANCE PHASE D — deterministic candidate-adjudication fast path.

Covers the single-candidate-only fast path added to
core.semantic.candidate_adjudicator (_evaluate_fast_path_candidate,
adjudicate_candidates's simple_selection_only wiring, and shadow-mode
logging). Live shadow-mode evidence against the real 37-question suite
found ZERO eligible calls in real traffic (gather_candidate_evidence's own
promoted/lineage-candidate widening means evidence is never a true
singleton in practice) -- these tests instead prove the MECHANISM itself
is correct against synthetic evidence covering the Part 8 adversarial
matrix, so the safety logic is verified and available should a future
milestone find (or create) a genuine single-candidate shape.

Run:
    venv/Scripts/python.exe -m pytest tests/test_performance_phase_d_fast_path.py -v
"""
from __future__ import annotations

import os
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-perf-phase-d-secret-long-enough-1234567890ab")
os.environ.setdefault("USER_ID_SALT", "test-perf-phase-d-salt-long-enough-1234567890abcd")

import core.semantic.candidate_adjudicator as ca
from core.semantic.candidate_adjudicator import (
    AdjudicationResult,
    _evaluate_fast_path_candidate,
    adjudicate_candidates,
    disable_shadow_log,
    enable_shadow_log,
    get_shadow_log,
)


def _cand(
    table_fqn="dbo.EDU_Track", *, role="catalog_or_entity", name_score=0.72, score=0.72,
    derivative_flag=None, object_type="TABLE", restricted_view_evidence=None,
    approx_row_count=103, deterministic_rank=1,
):
    return {
        "table_fqn": table_fqn, "object_type": object_type,
        "candidate_business_role": role, "name_score": name_score, "score": score,
        "derivative_flag": derivative_flag, "restricted_view_evidence": restricted_view_evidence,
        "approx_row_count": approx_row_count, "deterministic_rank": deterministic_rank,
    }


def setup_function(_fn):
    disable_shadow_log()
    ca._FAST_PATH_ACTIVE = False


def teardown_function(_fn):
    disable_shadow_log()
    ca._FAST_PATH_ACTIVE = False


# ---------------------------------------------------------------------------
# _evaluate_fast_path_candidate — pure mechanism tests (Part 8 adversarial matrix)
# ---------------------------------------------------------------------------

def test_strong_single_catalog_candidate_is_eligible():
    result, reason = _evaluate_fast_path_candidate("program", [_cand()], None)
    assert result is not None, reason
    assert result.selected_table_fqn == "dbo.EDU_Track"
    assert result.no_safe_selection is False


def test_weak_lexical_only_candidate_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate("program", [_cand(name_score=0.2, score=0.2)], None)
    assert result is None
    assert "name_score" in reason


def test_two_candidates_always_defers_to_ai():
    # Part 6 (dominant-candidate) is explicitly NOT implemented this
    # milestone -- candidate_count != 1 must always defer, regardless of
    # how lopsided the scores are.
    result, reason = _evaluate_fast_path_candidate(
        "program", [_cand("dbo.EDU_Track", score=0.95), _cand("dbo.Other", score=0.05)], None,
    )
    assert result is None
    assert "candidate_count=2" in reason


def test_identical_scores_two_candidates_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate(
        "program", [_cand("dbo.A", score=0.5), _cand("dbo.B", score=0.5)], None,
    )
    assert result is None


def test_catalog_vs_assignment_role_single_candidate_defers_to_ai():
    # THE Program adversarial case: a single assignment/event-shaped
    # candidate must never fast-path, even alone -- this is exactly the
    # live-confirmed D4b regression shape (gather_candidate_evidence's own
    # retrieval returning only wrong, assignment-shaped candidates with the
    # true catalog table absent from evidence entirely).
    result, reason = _evaluate_fast_path_candidate(
        "program", [_cand("dbo.EDU_Mentoring_Program", role="assignment_or_event")], None,
    )
    assert result is None
    assert "candidate_business_role" in reason


def test_unknown_business_role_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate("x", [_cand(role="unknown")], None)
    assert result is None


def test_restricted_view_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate(
        "enrollment",
        [_cand("dbo.vw_EDU_Enrollments", object_type="VIEW",
               restricted_view_evidence={"restriction_detected": True, "restriction_kinds": ["where_literal_filter"]})],
        None,
    )
    assert result is None
    assert "restricted view" in reason


def test_unrestricted_view_with_known_status_is_eligible():
    result, reason = _evaluate_fast_path_candidate(
        "enrollment",
        [_cand("dbo.vw_AllEnrollments", object_type="VIEW",
               restricted_view_evidence={"restriction_detected": False, "restriction_kinds": []})],
        None,
    )
    assert result is not None, reason
    assert result.selected_table_fqn == "dbo.vw_AllEnrollments"


def test_view_with_unknown_restriction_status_defers_to_ai():
    # Incomplete evidence about restriction status is never treated as
    # evidence of safety -- mirrors _apply_restricted_view_backstop's own
    # "no lineage source == reject" discipline.
    result, reason = _evaluate_fast_path_candidate(
        "enrollment", [_cand("dbo.vw_X", object_type="VIEW", restricted_view_evidence=None)], None,
    )
    assert result is None
    assert "unknown restriction status" in reason


def test_zero_row_candidate_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate("x", [_cand(approx_row_count=0)], None)
    assert result is None
    assert "source-data-unusable" in reason


def test_unknown_row_count_still_eligible_for_table():
    # None (profiling depth varies) is tolerated for a real TABLE -- only
    # an explicit, KNOWN zero blocks the fast path.
    result, reason = _evaluate_fast_path_candidate("x", [_cand(approx_row_count=None)], None)
    assert result is not None, reason


def test_derivative_flagged_candidate_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate(
        "x", [_cand(derivative_flag="Naming penalty: backup table")], None,
    )
    assert result is None
    assert "derivative_flag" in reason


def test_requested_attributes_present_defers_to_ai():
    result, reason = _evaluate_fast_path_candidate("x", [_cand()], ["email"])
    assert result is None
    assert "requested_attributes" in reason


def test_missing_table_fqn_defers_to_ai():
    broken = _cand()
    broken["table_fqn"] = None
    result, reason = _evaluate_fast_path_candidate("x", [broken], None)
    assert result is None


def test_enrollment_canonical_shape_is_eligible():
    # Enrollment's own canonical entity (base table, catalog_or_entity,
    # not a restricted view) -- the shape that SHOULD be safe to fast-path
    # if it were ever the sole candidate.
    result, reason = _evaluate_fast_path_candidate(
        "enrollment", [_cand("dbo.EDU_ClassSignups", role="catalog_or_entity")], None,
    )
    assert result is not None, reason
    assert result.selected_table_fqn == "dbo.EDU_ClassSignups"


# ---------------------------------------------------------------------------
# adjudicate_candidates wiring — activation, shadow mode, scoping
# ---------------------------------------------------------------------------

def test_inactive_fast_path_still_calls_ai_even_when_eligible(monkeypatch):
    # _FAST_PATH_ACTIVE=False (the default/current state) -- eligible
    # evidence must still go through the real AI call every time.
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

    result = adjudicate_candidates("program", [_cand()], None, simple_selection_only=True)
    assert len(calls) == 1  # AI was called despite eligibility -- not yet activated
    assert result.selected_table_fqn == "dbo.EDU_Track"


def test_activated_fast_path_skips_ai_call_entirely(monkeypatch):
    ca._FAST_PATH_ACTIVE = True
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "test-key")

    def _explode(*a, **kw):
        raise AssertionError("AI must not be called when the fast path is active and eligible")

    monkeypatch.setattr(ca, "_openai", type("M", (), {"OpenAI": _explode}))
    monkeypatch.setattr(ca, "_OPENAI_AVAILABLE", True)

    result = adjudicate_candidates("program", [_cand()], None, simple_selection_only=True)
    assert result is not None
    assert result.selected_table_fqn == "dbo.EDU_Track"
    assert "Fast path" in result.evidence_references[0]


def test_ineligible_evidence_still_calls_ai_when_active(monkeypatch):
    # Activation only ever short-circuits the PROVABLY-unambiguous class --
    # a Program-adversarial single assignment-shaped candidate must still
    # reach the real AI adjudicator even with the fast path turned on.
    ca._FAST_PATH_ACTIVE = True
    monkeypatch.setattr("core.config.OPENAI_API_KEY", "test-key")
    calls = []

    class _FakeResponse:
        class _Choice:
            class _Msg:
                content = '{"no_safe_selection": true}'
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
        "program", [_cand("dbo.EDU_Mentoring_Program", role="assignment_or_event")], None,
        simple_selection_only=True,
    )
    assert len(calls) == 1  # AI was called -- Program adversarial case never fast-paths


def test_simple_selection_only_false_never_evaluates_fast_path(monkeypatch):
    # data.semantic_contract_service._discover_entity_contract_via_
    # adjudication (the ONE other caller) never passes this flag and must
    # be completely unaffected -- even with the fast path active and
    # evidence that WOULD be eligible, the default (False) call must still
    # reach the real AI.
    ca._FAST_PATH_ACTIVE = True
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

    adjudicate_candidates("program", [_cand()], None)  # simple_selection_only defaults to False
    assert len(calls) == 1


def test_shadow_mode_never_skips_ai_even_when_eligible_and_active(monkeypatch):
    ca._FAST_PATH_ACTIVE = True
    enable_shadow_log()
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

    result = adjudicate_candidates("program", [_cand()], None, simple_selection_only=True)
    assert len(calls) == 1  # shadow mode never substitutes, even though eligible+active
    assert result.selected_table_fqn == "dbo.EDU_Track"

    log = get_shadow_log()
    assert len(log) == 1
    assert log[0]["fast_path_eligible"] is True
    assert log[0]["fast_path_selected_table_fqn"] == "dbo.EDU_Track"
    assert log[0]["real_selected_table_fqn"] == "dbo.EDU_Track"


def test_no_extra_ai_calls_when_fast_path_ineligible_and_inactive(monkeypatch):
    # Baseline sanity: with the fast path never active, behavior is
    # byte-identical to before this milestone -- exactly one AI call, no
    # extra calls introduced by the eligibility check itself.
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
        "x", [_cand("dbo.A", score=0.9), _cand("dbo.B", score=0.1)], None, simple_selection_only=True,
    )
    assert len(calls) == 1
