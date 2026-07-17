"""
Smoke tests for docs/validation/run_acceptance_suite.py — Milestone M-23
(Phase 6.5).

Proves the grading logic itself is correct against hand-built fixture
questions with known, controlled pipeline outputs, before it is trusted to
grade the real 98-question suite against live CCPP data. Every pipeline call
is mocked here — no database, no real question text from the locked suite.

Run from the project root:
    venv/Scripts/pytest tests/test_acceptance_suite_runner.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-acceptance-runner-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-acceptance-runner-salt-long-enough-1")

_SCRIPT_PATH = Path(__file__).parent.parent / "docs" / "validation" / "run_acceptance_suite.py"
_spec = importlib.util.spec_from_file_location("run_acceptance_suite", _SCRIPT_PATH)
runner = importlib.util.module_from_spec(_spec)
sys.modules["run_acceptance_suite"] = runner
_spec.loader.exec_module(runner)


def _resolved(intent_value: str):
    return SimpleNamespace(intent_type=SimpleNamespace(value=intent_value))


def _sql_question(qid="EBVS-X-01", category="Counts", expected_outcome="SUCCESS"):
    return {
        "id": qid, "category": category, "question": "How many things are there?",
        "expected_intent": "sql_request", "expected_outcome": expected_outcome,
    }


def _metadata_question(qid="EBVS-M-01", expected_outcome="SUCCESS"):
    return {
        "id": qid, "category": "Metadata", "question": "Show me the schema",
        "expected_intent": "metadata_lookup", "expected_outcome": expected_outcome,
    }


def _patch_pipeline(monkeypatch, *, intent="sql_request", sql="SELECT 1"):
    monkeypatch.setattr(
        "core.orchestrator.intent_resolver.IntentResolver.resolve",
        lambda self, q: _resolved(intent),
    )
    monkeypatch.setattr(
        "core.semantic.concept_resolver.extract_terms",
        lambda q: (["thing"], ["thing"], []),
    )
    monkeypatch.setattr("data.query_planning_service.plan_business_query", lambda *a, **k: {"fake": "plan"})
    monkeypatch.setattr("data.sql_planning_service.build_sql_plan", lambda *a, **k: {"validation": {"valid": bool(sql)}})
    monkeypatch.setattr(
        "data.sql_generation_service.generate_sql",
        lambda *a, **k: {"sql": sql, "explanation": [] if sql else ["Refused: no confident match."]},
    )


def test_sql_request_success_when_sql_generated(monkeypatch):
    _patch_pipeline(monkeypatch, sql="SELECT COUNT(*) FROM t")
    q = _sql_question(expected_outcome="SUCCESS")
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is True
    assert entry["sql_generated"] is True


def test_sql_request_fails_when_expected_success_but_refused(monkeypatch):
    _patch_pipeline(monkeypatch, sql=None)
    q = _sql_question(expected_outcome="SUCCESS")
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is False
    assert entry["sql_generated"] is False
    assert entry["reason"]


def test_refusal_expected_outcomes_pass_when_refused(monkeypatch):
    _patch_pipeline(monkeypatch, sql=None)
    for outcome in ("REFUSED_SAFE", "AMBIGUOUS", "CLARIFICATION_NEEDED", "NOT_SUPPORTED"):
        q = _sql_question(qid=f"EBVS-X-{outcome}", expected_outcome=outcome)
        entry = runner._grade_one(1, "u1", q)
        assert entry["passed"] is True, f"{outcome} should pass on safe refusal"


def test_refusal_expected_outcomes_fail_when_sql_fabricated(monkeypatch):
    _patch_pipeline(monkeypatch, sql="SELECT 1")
    q = _sql_question(expected_outcome="AMBIGUOUS")
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is False


def test_known_defect_always_passes(monkeypatch):
    _patch_pipeline(monkeypatch, sql="SELECT wrong_thing")
    q = _sql_question(expected_outcome="KNOWN_DEFECT")
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is True
    assert entry["improved_known_defect"] is False


def test_known_defect_flags_improvement_when_now_refused(monkeypatch):
    _patch_pipeline(monkeypatch, sql=None)
    q = _sql_question(expected_outcome="KNOWN_DEFECT")
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is True
    assert entry["improved_known_defect"] is True


def test_non_sql_intent_grades_on_intent_match_only(monkeypatch):
    _patch_pipeline(monkeypatch, intent="metadata_lookup")
    q = _metadata_question()
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is True
    assert entry["sql_generated"] is None  # SQL pipeline never invoked


def test_intent_mismatch_fails_even_with_correct_outcome(monkeypatch):
    _patch_pipeline(monkeypatch, intent="question_answering")
    q = _metadata_question()
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is False
    assert "mismatch" in entry["reason"].lower()


def test_sql_intent_mismatch_fails_regardless_of_sql_outcome(monkeypatch):
    _patch_pipeline(monkeypatch, intent="question_answering", sql="SELECT COUNT(*) FROM t")
    q = _sql_question(expected_outcome="SUCCESS")
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is False


def test_exception_never_propagates(monkeypatch):
    def _boom(self, q):
        raise RuntimeError("simulated pipeline failure")
    monkeypatch.setattr("core.orchestrator.intent_resolver.IntentResolver.resolve", _boom)
    q = _sql_question()
    entry = runner._grade_one(1, "u1", q)
    assert entry["passed"] is False
    assert "ERROR" in entry["reason"]


def test_category_table_aggregates_correctly():
    results = [
        {"category": "Counts", "passed": True},
        {"category": "Counts", "passed": False},
        {"category": "Lists", "passed": True},
    ]
    rows = dict((cat, (p, t)) for cat, p, t in runner._category_table(results))
    assert rows["Counts"] == (1, 2)
    assert rows["Lists"] == (1, 1)


def test_load_suite_reads_locked_json():
    questions = runner._load_suite(runner._SUITE_PATH)
    assert len(questions) == 98
    ids = {q["id"] for q in questions}
    assert "EBVS-C-01" in ids
    assert all("category" in q for q in questions)
