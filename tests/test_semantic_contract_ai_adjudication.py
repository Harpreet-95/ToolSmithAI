"""
Day 2C, Task 2/3 — data.semantic_contract_service's AI-adjudication
fallback (discover_entity_contract) and forced revalidation
(revalidate_entity_contract).

adjudicate_candidates() itself is unit-tested in
tests/test_candidate_adjudicator.py; validate_candidate_contract() in
tests/test_semantic_contract_validation_rules.py. These tests only exercise
the wiring: when the fallback/revalidation path runs, what it does with a
mocked AdjudicationResult, and that it never behaves worse than the
deterministic Day 2B path on its own.

Run:
    python -m pytest tests/test_semantic_contract_ai_adjudication.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2c-ai-adjudication-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-day2c-ai-adjudication-salt-long-enough12")

import data.semantic_retrieval_service as semantic_retrieval_service
from core.semantic.candidate_adjudicator import AdjudicationResult
from data.semantic_contract_service import (
    discover_entity_contract,
    get_latest_entity_contract,
    persist_entity_contract,
    revalidate_entity_contract,
)
from tests.test_phase9_query_planning import env, _add_column, _add_domain, _add_fk, _add_table


def _fake_retrieval(table_fqns):
    def _fn(source_id, user_id, question, terms, *, session=None):
        return set(table_fqns), [], []
    return _fn


def _seed_launch_scenario(db):
    _add_table(db, "dbo.EDU_Student", table_class="Transactional")
    _add_column(db, "dbo.EDU_Student", "StudentUserID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.EDU_Student", "Student")

    _add_table(db, "dbo.EDU_Pilot_Participants", table_class="Transactional")
    _add_column(db, "dbo.EDU_Pilot_Participants", "LaunchParticipantID",
                data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Pilot_Participants", "StudentUserID", data_type="INTEGER")
    _add_column(db, "dbo.EDU_Pilot_Participants", "Status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Status", approved=True)
    _add_domain(db, "dbo.EDU_Pilot_Participants", "Launch")
    _add_fk(db, "dbo.EDU_Pilot_Participants", "StudentUserID", "dbo.EDU_Student", "StudentUserID")


# ---------------------------------------------------------------------------
# discover_entity_contract — AI-adjudication fallback (Task 2 wiring)
# ---------------------------------------------------------------------------

def test_ai_fallback_never_runs_when_flag_disabled(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval([]))
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", False)

    calls = []
    monkeypatch.setattr(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        lambda *a, **k: calls.append(1),
    )

    result = discover_entity_contract(1, "u1", "Recruiter")
    assert result["resolution_status"] == "NO_CANDIDATE"
    assert calls == []


def test_ai_fallback_only_runs_after_deterministic_no_candidate(tmp_path, monkeypatch):
    """A deterministic RESOLVED/PARTIAL result must never trigger the AI
    fallback — discover_entity_contract's whole point is "AI only as a
    fallback", not "AI always consulted"."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dnnuser.FIN_Invoice", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dnnuser.FIN_Invoice", "Operations")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dnnuser.FIN_Invoice"]),
    )
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", True)

    calls = []
    monkeypatch.setattr(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        lambda *a, **k: calls.append(1),
    )

    result = discover_entity_contract(1, "u1", "Invoice")
    assert result["resolution_status"] == "RESOLVED"
    assert result["canonical_table_fqn"] == "dnnuser.FIN_Invoice"
    assert calls == []


def test_ai_fallback_persists_validated_selection_when_deterministic_finds_nothing(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_launch_scenario(db)
    # Deterministic retrieval finds nothing at all for "Recruiter" (a
    # different entity than the seeded launch tables) — forces NO_CANDIDATE.
    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval([]))
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", True)

    result = discover_entity_contract(1, "u1", "Recruiter")
    assert result["resolution_status"] == "NO_CANDIDATE"  # empty pool -> never even reaches adjudication


def test_ai_fallback_returns_no_safe_selection_when_ai_declines(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.SomeAmbiguousTable", table_class="Transactional")
    _add_column(db, "dbo.SomeAmbiguousTable", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.SomeAmbiguousTable"]),
    )
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", True)
    monkeypatch.setattr(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        lambda *a, **k: AdjudicationResult(no_safe_selection=True, confidence=0.2),
    )

    result = discover_entity_contract(1, "u1", "Recruiter")
    assert result["resolution_status"] == "NO_SAFE_SELECTION"
    assert result["canonical_table_fqn"] is None
    assert result["generation_method"] == "ai_adjudicated"


def test_ai_fallback_rejects_selection_that_fails_deterministic_validation(tmp_path, monkeypatch):
    """The AI selecting a table is not enough — Task 1's validator still
    has to accept it. Here the AI picks a table with no primary key/
    uniqueness evidence at all, so validation must reject it."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.NoKeyTable", table_class="Transactional")
    _add_column(db, "dbo.NoKeyTable", "notes", data_type="TEXT")
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dbo.NoKeyTable"]),
    )
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", True)
    monkeypatch.setattr(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        lambda *a, **k: AdjudicationResult(selected_table_fqn="dbo.NoKeyTable", confidence=0.8),
    )

    result = discover_entity_contract(1, "u1", "Recruiter")
    assert result["resolution_status"] == "NO_SAFE_SELECTION"
    assert "failed deterministic validation" in result["contract_json"]["evidence"][0]


# ---------------------------------------------------------------------------
# revalidate_entity_contract — Task 3 forced revalidation
# ---------------------------------------------------------------------------

def test_revalidate_is_noop_when_ai_adjudication_disabled(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    persist_entity_contract(
        1, "Pilot Participant", canonical_table_fqn="dbo.EDU_Student", preferred_view_fqn=None,
        resolution_status="RESOLVED", confidence=0.9607, contract_json={"evidence": []}, metadata_revision="1:1",
    )
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", False)

    result = revalidate_entity_contract(1, "u1", "Pilot Participant")
    assert result["canonical_table_fqn"] == "dbo.EDU_Student"
    assert result["contract_version"] == 1


def test_revalidate_is_noop_when_ai_call_itself_fails(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed_launch_scenario(db)
    persist_entity_contract(
        1, "Pilot Participant", canonical_table_fqn="dbo.EDU_Student", preferred_view_fqn=None,
        resolution_status="RESOLVED", confidence=0.9607, contract_json={"evidence": []}, metadata_revision="1:1",
    )
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.EDU_Student", "dbo.EDU_Pilot_Participants"]),
    )
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", True)
    monkeypatch.setattr("core.semantic.candidate_adjudicator.adjudicate_candidates", lambda *a, **k: None)

    result = revalidate_entity_contract(1, "u1", "Pilot Participant")
    assert result["canonical_table_fqn"] == "dbo.EDU_Student"
    assert result["contract_version"] == 1  # unchanged — AI failure keeps the existing contract


def test_revalidate_overrides_a_wrong_resolved_contract_with_evidence(tmp_path, monkeypatch):
    """Mirrors the a real production database finding: Pilot Participant was RESOLVED to
    dbo.EDU_Student (the general student roster) at the same confidence as
    Student itself. Forced revalidation, given real launch-specific
    evidence, must overturn it — even though the existing contract was
    already RESOLVED, not NO_CANDIDATE."""
    db = env(tmp_path, monkeypatch)
    _seed_launch_scenario(db)
    persist_entity_contract(
        1, "Pilot Participant", canonical_table_fqn="dbo.EDU_Student", preferred_view_fqn=None,
        resolution_status="RESOLVED", confidence=0.9607, contract_json={"evidence": []}, metadata_revision="1:1",
    )
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.EDU_Student", "dbo.EDU_Pilot_Participants"]),
    )
    monkeypatch.setattr("core.config.ENABLE_AI_CANDIDATE_ADJUDICATION", True)
    monkeypatch.setattr(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        lambda *a, **k: AdjudicationResult(
            selected_table_fqn="dbo.EDU_Pilot_Participants",
            identity_key="LaunchParticipantID",
            excluded_candidates=(
                {"table_fqn": "dbo.EDU_Student", "reason": "General student roster, not launch-specific."},
            ),
            confidence=0.9,
        ),
    )

    result = revalidate_entity_contract(1, "u1", "Pilot Participant")
    assert result["canonical_table_fqn"] == "dbo.EDU_Pilot_Participants"
    assert result["contract_version"] == 2
    assert result["generation_method"] == "ai_adjudicated"

    latest = get_latest_entity_contract(1, "Pilot Participant")
    assert latest["contract_version"] == 2
    assert latest["canonical_table_fqn"] == "dbo.EDU_Pilot_Participants"
