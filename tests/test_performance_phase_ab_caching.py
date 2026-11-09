"""
PERFORMANCE PHASE A+B — request-scoped memoization tests.

Covers two forensically-measured, purely-additive caching layers, neither of
which changes any selection/ranking/adjudication/SQL-generation semantics:

Phase A (data.semantic_contract_service.gather_candidate_evidence): the
same (source_id, entity_name) evidence-gathering pass is memoized per
request via the existing RequestMetadataSession.get_or_compute primitive --
the SAME mechanism core.semantic.candidate_adjudicator.adjudicate_candidates
and _verified_status_values_for_column already use, applied one layer
higher. Without a session, behavior is completely unchanged (every existing
non-session caller is unaffected).

Phase B (data.semantic_contract_service._probe_view_row_count /
_probe_view_definition): the two restricted-view live probes
(gather_candidate_evidence's own inspect loop, and
_discover_entity_contract_deterministic's bounded restriction check) are
memoized per (source_id, table_fqn) for the life of one request, using the
same primitive.

Run:
    venv/Scripts/python.exe -m pytest tests/test_performance_phase_ab_caching.py -v
"""
from __future__ import annotations

import os
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-perf-phase-ab-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-perf-phase-ab-salt-long-enough-1234567890ab")

import pytest

import data.semantic_retrieval_service as semantic_retrieval_service
from data.semantic_contract_service import (
    _probe_view_definition,
    _probe_view_row_count,
    gather_candidate_evidence,
)
from data.investigation_service import InvestigationResult
from data.request_metadata_session import RequestMetadataSession
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_domain


@pytest.fixture(autouse=True)
def _isolated_metadata_db(tmp_path, monkeypatch):
    """RequestMetadataSession.__init__ opens a metadata DB connection
    unconditionally, even for the Phase B probe-cache tests below that never
    call env() themselves -- a previously missing fixture (Phase 5
    Step 6C)."""
    import data.db as db_module
    import data.models as models

    db_path = str(tmp_path / "performance_phase_ab_default.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    yield


def _fake_retrieval(table_fqns):
    def _fn(source_id, user_id, question, terms, *, session=None):
        return set(table_fqns), [], []
    return _fn


# ---------------------------------------------------------------------------
# Phase A — gather_candidate_evidence request-scoped memoization
# ---------------------------------------------------------------------------

def _fixture_one_table(tmp_path, monkeypatch, table_fqn="dbo.programs"):
    db = env(tmp_path, monkeypatch)
    _add_table(db, table_fqn, table_class="Transactional")
    _add_column(db, table_fqn, "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, table_fqn, "Operations")
    return db


def test_same_entity_twice_with_session_discovers_once(tmp_path, monkeypatch):
    _fixture_one_table(tmp_path, monkeypatch)
    calls = []

    def counting_retrieval(*a, **k):
        calls.append(1)
        return _fake_retrieval(["dbo.programs"])(*a, **k)

    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", counting_retrieval)

    session = RequestMetadataSession(1, "u1")
    try:
        first = gather_candidate_evidence(1, "u1", "program", session=session)
        second = gather_candidate_evidence(1, "u1", "program", session=session)
    finally:
        session.close()

    assert len(calls) == 1  # discovery ran exactly once
    assert first == second
    assert first is second  # a cache hit reuses the identical object, never reinterpreted


def test_different_entity_discovers_separately(tmp_path, monkeypatch):
    _fixture_one_table(tmp_path, monkeypatch)
    calls = []

    def counting_retrieval(*a, **k):
        calls.append(1)
        return _fake_retrieval(["dbo.programs"])(*a, **k)

    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", counting_retrieval)

    session = RequestMetadataSession(1, "u1")
    try:
        gather_candidate_evidence(1, "u1", "program", session=session)
        gather_candidate_evidence(1, "u1", "course", session=session)
    finally:
        session.close()

    assert len(calls) == 2  # different entity_name -> separate discovery, never conflated


def test_different_source_discovers_separately(tmp_path, monkeypatch):
    _fixture_one_table(tmp_path, monkeypatch)
    calls = []

    def counting_retrieval(*a, **k):
        calls.append(a[0])  # source_id
        return _fake_retrieval(["dbo.programs"])(*a, **k)

    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", counting_retrieval)

    session = RequestMetadataSession(1, "u1")
    try:
        gather_candidate_evidence(1, "u1", "program", session=session)
        gather_candidate_evidence(2, "u1", "program", session=session)
    finally:
        session.close()

    assert calls == [1, 2]  # different source_id -> separate discovery, never shared


def test_without_session_no_memoization_legacy_behavior_unchanged(tmp_path, monkeypatch):
    _fixture_one_table(tmp_path, monkeypatch)
    calls = []

    def counting_retrieval(*a, **k):
        calls.append(1)
        return _fake_retrieval(["dbo.programs"])(*a, **k)

    monkeypatch.setattr(semantic_retrieval_service, "get_candidate_tables_with_ranking", counting_retrieval)

    gather_candidate_evidence(1, "u1", "program", session=None)
    gather_candidate_evidence(1, "u1", "program", session=None)

    assert len(calls) == 2  # no session -> every call pays full cost, exactly as before this change


def test_cache_hit_records_observability_stage(tmp_path, monkeypatch):
    _fixture_one_table(tmp_path, monkeypatch)
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(["dbo.programs"]),
    )

    from core.perf import stage_timer

    session = RequestMetadataSession(1, "u1")
    try:
        with stage_timer.start() as timer:
            gather_candidate_evidence(1, "u1", "program", session=session)
            gather_candidate_evidence(1, "u1", "program", session=session)
        stage_names = [s["stage"] for s in timer.stages]
    finally:
        session.close()

    assert stage_names.count("gather_candidate_evidence_cache_hit") == 1


# ---------------------------------------------------------------------------
# Phase B — VIEW row_count / view_definition probe request-scoped memoization
# ---------------------------------------------------------------------------

def _investigation_result(investigation_type, table, value):
    return InvestigationResult(
        investigation_type=investigation_type, table=table, column=None, reason="test",
        summary="ok", row_count=1, sample_values=[value], duration_ms=1.0, valid=True,
    )


def test_view_row_count_probe_same_view_twice_probes_once(tmp_path, monkeypatch):
    calls = []

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append((table_fqn, investigation_type))
        return _investigation_result("row_count", table_fqn, "500")

    session = RequestMetadataSession(1, "u1")
    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)
    try:
        first = _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=session)
        second = _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=session)
    finally:
        session.close()

    assert len(calls) == 1  # the real DB round trip happened exactly once
    assert first is second  # second call reused the exact same InvestigationResult, never re-interpreted


def test_view_definition_probe_same_view_twice_probes_once(tmp_path, monkeypatch):
    calls = []

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append((table_fqn, investigation_type))
        return _investigation_result("view_definition", table_fqn, "CREATE VIEW ...")

    session = RequestMetadataSession(1, "u1")
    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)
    try:
        first = _probe_view_definition(1, "u1", "dbo.v_active_classes", reason="r", session=session)
        second = _probe_view_definition(1, "u1", "dbo.v_active_classes", reason="r", session=session)
    finally:
        session.close()

    assert len(calls) == 1
    assert first is second


def test_view_probe_different_view_probes_separately(tmp_path, monkeypatch):
    calls = []

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append(table_fqn)
        return _investigation_result("row_count", table_fqn, "500")

    session = RequestMetadataSession(1, "u1")
    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)
    try:
        _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=session)
        _probe_view_row_count(1, "u1", "dbo.v_active_programs", reason="r", session=session)
    finally:
        session.close()

    assert calls == ["dbo.v_active_classes", "dbo.v_active_programs"]  # never conflated across views


def test_view_probe_without_session_no_memoization(monkeypatch):
    calls = []

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append(1)
        return _investigation_result("row_count", table_fqn, "500")

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=None)
    _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=None)

    assert len(calls) == 2  # no session -> every call is a real probe, exactly as before this change


def test_view_probe_cache_does_not_survive_a_new_request(tmp_path, monkeypatch):
    calls = []

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append(1)
        return _investigation_result("row_count", table_fqn, "500")

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    session_a = RequestMetadataSession(1, "u1")
    try:
        _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=session_a)
    finally:
        session_a.close()

    session_b = RequestMetadataSession(1, "u1")  # a fresh, separate request
    try:
        _probe_view_row_count(1, "u1", "dbo.v_active_classes", reason="r", session=session_b)
    finally:
        session_b.close()

    assert len(calls) == 2  # the second request's own first probe is real, not inherited from session_a


def test_failed_probe_cached_within_request_not_retried(tmp_path, monkeypatch):
    # Matches the SAME failure-caching discipline _verified_status_values_
    # for_column already applies to the status-probe cache: within one
    # request, a repeated identical live call is expected to fail the same
    # way again, so it is not retried -- but it never survives past this
    # one session (Part 13: never persist a failure across requests).
    calls = []

    def failing_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append(1)
        return InvestigationResult(
            investigation_type=investigation_type, table=table_fqn, column=None, reason="failed",
            summary="timeout", row_count=0, sample_values=[], duration_ms=1.0, valid=False,
        )

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", failing_probe)

    session = RequestMetadataSession(1, "u1")
    try:
        first = _probe_view_row_count(1, "u1", "dbo.v_flaky", reason="r", session=session)
        second = _probe_view_row_count(1, "u1", "dbo.v_flaky", reason="r", session=session)
    finally:
        session.close()

    assert len(calls) == 1  # not retried within the same request
    assert first.valid is False and second.valid is False

    # A brand-new request must NOT inherit the cached failure.
    calls.clear()
    session2 = RequestMetadataSession(1, "u1")
    try:
        _probe_view_row_count(1, "u1", "dbo.v_flaky", reason="r", session=session2)
    finally:
        session2.close()
    assert len(calls) == 1  # the new request's own real probe ran again


def test_view_probe_cache_hit_records_observability_stage(tmp_path, monkeypatch):
    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        return _investigation_result(investigation_type, table_fqn, "500")

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    from core.perf import stage_timer

    session = RequestMetadataSession(1, "u1")
    try:
        with stage_timer.start() as timer:
            _probe_view_row_count(1, "u1", "dbo.v_x", reason="r", session=session)
            _probe_view_row_count(1, "u1", "dbo.v_x", reason="r", session=session)
            _probe_view_definition(1, "u1", "dbo.v_x", reason="r", session=session)
            _probe_view_definition(1, "u1", "dbo.v_x", reason="r", session=session)
        stage_names = [s["stage"] for s in timer.stages]
    finally:
        session.close()

    assert stage_names.count("view_row_count_probe_cache_hit") == 1
    assert stage_names.count("view_definition_probe_cache_hit") == 1
