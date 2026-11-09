"""
PERFORMANCE PHASE C — bounded, read-only parallelization of the VIEW
row_count/view_definition probe prefetch inside
data.semantic_contract_service._gather_candidate_evidence_uncached's inspect
loop.

Architecture under test: a bounded ThreadPoolExecutor prefetch batch warms
the SAME Phase A+B session-scoped probe cache
(_probe_view_row_count/_probe_view_definition, unchanged) the fully serial
inspect loop already consults -- the loop itself, its ordering, its lineage-
candidate recursion, and its evidence/candidates_by_fqn mutation are all
completely unmodified. This gives a clean "serial reference" for free:
calling gather_candidate_evidence with session=None skips the prefetch
branch entirely (see the `if session is not None:` guard) and runs the
exact pre-Phase-C code path -- so a session=None call IS the serial
reference implementation Part 13 asks to compare against.

Run:
    venv/Scripts/python.exe -m pytest tests/test_performance_phase_c_parallel_probes.py -v
"""
from __future__ import annotations

import os
import threading
import time

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-perf-phase-c-secret-long-enough-1234567890ab")
os.environ.setdefault("USER_ID_SALT", "test-perf-phase-c-salt-long-enough-1234567890abcd")

import data.semantic_retrieval_service as semantic_retrieval_service
from data.semantic_contract_service import _MAX_VIEW_PROBE_WORKERS, gather_candidate_evidence
from data.investigation_service import InvestigationResult
from data.request_metadata_session import RequestMetadataSession
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_domain, _c


def _fake_retrieval(table_fqns):
    def _fn(source_id, user_id, question, terms, *, session=None):
        return set(table_fqns), [], []
    return _fn


def _mark_profiling_view(db, table_fqn):
    # The object_type == "VIEW" gate _build_evidence_entry (and
    # _discover_entity_contract_deterministic) check reads ctx["table"]
    # ["table_type"], sourced from profiling_table_profiles.table_type --
    # data.business_knowledge_service.get_table_business_context's own
    # SELECT (confirmed by direct read), NOT data_dictionary_tables.
    # table_type (a separate, unrelated field the existing test helper
    # _add_table_type updates for a different purpose elsewhere).
    conn = _c(db)
    conn.execute(
        "UPDATE profiling_table_profiles SET table_type = 'VIEW' WHERE source_id=1 AND table_fqn = ?",
        (table_fqn,),
    )
    conn.commit()
    conn.close()


def _three_view_fixture(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    for name in ("dbo.v_a", "dbo.v_b", "dbo.v_c"):
        _add_table(db, name, table_class="Master")
        _add_column(db, name, "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
        _add_domain(db, name, "Operations")
        _mark_profiling_view(db, name)
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking",
        _fake_retrieval(["dbo.v_a", "dbo.v_b", "dbo.v_c"]),
    )
    return db


def _view_probe(table_fqn, investigation_type, *, delay=0.0):
    if delay:
        time.sleep(delay)
    return InvestigationResult(
        investigation_type=investigation_type, table=table_fqn, column=None, reason="test",
        summary="ok", row_count=1, sample_values=["not a restricted view definition"], duration_ms=1.0, valid=True,
    )


# ---------------------------------------------------------------------------
# Structural equivalence: session=None (serial reference) vs session=<real>
# (parallel prefetch) must produce byte-identical evidence.
# ---------------------------------------------------------------------------

def test_parallel_result_structurally_equivalent_to_serial(tmp_path, monkeypatch):
    _three_view_fixture(tmp_path, monkeypatch)

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        return _view_probe(table_fqn, investigation_type)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    serial = gather_candidate_evidence(1, "u1", "widget", session=None)

    session = RequestMetadataSession(1, "u1")
    try:
        parallel = gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()

    assert [e["table_fqn"] for e in serial["evidence"]] == [e["table_fqn"] for e in parallel["evidence"]]
    assert serial["pool_table_fqns"] == parallel["pool_table_fqns"]
    for s_entry, p_entry in zip(serial["evidence"], parallel["evidence"]):
        # Compare every field EXCEPT nothing -- these two runs must be
        # completely indistinguishable to any downstream consumer (the
        # adjudicator, the physical planner) except for wall-clock time.
        assert s_entry == p_entry
    assert set(serial["candidates_by_fqn"].keys()) == set(parallel["candidates_by_fqn"].keys())


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------

def test_concurrency_never_exceeds_configured_bound(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    view_names = [f"dbo.v_{i}" for i in range(6)]  # more than _MAX_VIEW_PROBE_WORKERS
    for name in view_names:
        _add_table(db, name, table_class="Master")
        _add_column(db, name, "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
        _add_domain(db, name, "Operations")
        _mark_profiling_view(db, name)
    monkeypatch.setattr(
        semantic_retrieval_service, "get_candidate_tables_with_ranking", _fake_retrieval(view_names),
    )

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.05)  # hold the "slot" long enough for real overlap to show up
        with lock:
            state["current"] -= 1
        return _view_probe(table_fqn, investigation_type)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    session = RequestMetadataSession(1, "u1")
    try:
        result = gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()

    assert len(result["evidence"]) == min(6, 5)  # _MAX_ADJUDICATION_INSPECTED still governs pool size
    assert state["peak"] <= _MAX_VIEW_PROBE_WORKERS
    assert state["peak"] > 1  # genuine overlap actually happened, this isn't accidentally sequential


# ---------------------------------------------------------------------------
# Ordering preserved regardless of completion order
# ---------------------------------------------------------------------------

def test_slow_candidate_does_not_reorder_evidence(tmp_path, monkeypatch):
    _three_view_fixture(tmp_path, monkeypatch)

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        # dbo.v_a is deliberately the SLOWEST -- if completion order leaked
        # into evidence order, it would land last instead of first.
        delay = 0.15 if table_fqn == "dbo.v_a" else 0.0
        return _view_probe(table_fqn, investigation_type, delay=delay)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    session = RequestMetadataSession(1, "u1")
    try:
        result = gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()

    fqns_in_evidence_order = [e["table_fqn"] for e in result["evidence"]]
    fqns_in_serial_order = sorted(fqns_in_evidence_order)  # to_inspect's own ranked order is deterministic
    # The exact contract: evidence order must match to_inspect's order,
    # never the order workers happened to finish in.
    assert fqns_in_evidence_order == [e["table_fqn"] for e in
                                       gather_candidate_evidence(1, "u1", "widget", session=None)["evidence"]]


# ---------------------------------------------------------------------------
# Failure isolation: one failed probe never corrupts another candidate's
# result, and preserves the same "failed probe" semantics a synchronous
# call would have produced.
# ---------------------------------------------------------------------------

def test_one_failed_probe_does_not_corrupt_other_candidates(tmp_path, monkeypatch):
    _three_view_fixture(tmp_path, monkeypatch)

    def fake_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        if table_fqn == "dbo.v_b":
            return InvestigationResult(
                investigation_type=investigation_type, table=table_fqn, column=None, reason="failed",
                summary="timeout", row_count=0, sample_values=[], duration_ms=1.0, valid=False,
            )
        return _view_probe(table_fqn, investigation_type)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", fake_probe)

    session = RequestMetadataSession(1, "u1")
    try:
        result = gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()

    by_fqn = {e["table_fqn"]: e for e in result["evidence"]}
    assert set(by_fqn) == {"dbo.v_a", "dbo.v_b", "dbo.v_c"}
    # v_b's failed probe -> no restriction evidence could be derived (same
    # as _detect_view_restriction(None) would produce for a synchronous
    # failure) -- but v_a/v_c are completely unaffected.
    assert by_fqn["dbo.v_b"]["restricted_view_evidence"] is None
    assert by_fqn["dbo.v_a"]["restricted_view_evidence"] is not None
    assert by_fqn["dbo.v_c"]["restricted_view_evidence"] is not None


def test_worker_exception_does_not_prevent_other_candidates_prefetching(tmp_path, monkeypatch):
    # inspect_targeted_values's real contract is "never raises" (it fails
    # closed to InvestigationResult(valid=False) on any error) -- a genuine
    # exception is an out-of-contract situation the pre-Phase-C serial code
    # never specially handled either (it would propagate up exactly the
    # same way it does here). The prefetch try/except backstop's actual job
    # is narrower and still real: absorb ONE worker's crash so it can never
    # prevent OTHER, unrelated workers in the SAME bounded batch from
    # completing their own prefetch -- proven directly below by checking
    # v_b/v_c were both still probed even though v_a's worker raised.
    _three_view_fixture(tmp_path, monkeypatch)
    probed = []

    def flaky_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        probed.append(table_fqn)
        if table_fqn == "dbo.v_a":
            raise RuntimeError("simulated worker crash")
        return _view_probe(table_fqn, investigation_type)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", flaky_probe)

    session = RequestMetadataSession(1, "u1")
    try:
        # dbo.v_a's crash is never cached (the exception propagates instead
        # of returning a value get_or_compute could store), so the serial
        # loop's own later, direct call for dbo.v_a hits the SAME crash --
        # this is IDENTICAL to what session=None (no prefetch at all) would
        # do, proving Phase C changes no error-propagation semantics.
        with __import__("pytest").raises(RuntimeError, match="simulated worker crash"):
            gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()

    # v_b and v_c were still reached by the prefetch batch despite v_a's
    # worker crashing -- one bad worker did not abort the others.
    assert "dbo.v_b" in probed
    assert "dbo.v_c" in probed


def test_worker_exception_propagates_identically_with_and_without_session(tmp_path, monkeypatch):
    def make_flaky_probe():
        def flaky_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
            if table_fqn == "dbo.v_a":
                raise RuntimeError("simulated crash")
            return _view_probe(table_fqn, investigation_type)
        return flaky_probe

    import pytest as _pytest

    _three_view_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", make_flaky_probe())
    with _pytest.raises(RuntimeError, match="simulated crash"):
        gather_candidate_evidence(1, "u1", "widget", session=None)

    session = RequestMetadataSession(1, "u1")
    try:
        with _pytest.raises(RuntimeError, match="simulated crash"):
            gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Cache interaction: prefetch must not launch workers for already-cached
# candidates, and duplicate probes must remain deduplicated.
# ---------------------------------------------------------------------------

def test_already_cached_probe_launches_no_worker(tmp_path, monkeypatch):
    _three_view_fixture(tmp_path, monkeypatch)
    calls = []

    def counting_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append((table_fqn, investigation_type))
        return _view_probe(table_fqn, investigation_type)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", counting_probe)

    session = RequestMetadataSession(1, "u1")
    try:
        gather_candidate_evidence(1, "u1", "widget", session=session)  # warms the cache for all 3 views
        calls_after_first = len(calls)
        gather_candidate_evidence(1, "u1", "widget", session=session)  # identical entity_name -> gce cache hit
    finally:
        session.close()

    assert len(calls) == calls_after_first  # zero additional live probes on the second (fully-cached) call


def test_no_extra_ai_calls_during_prefetch(tmp_path, monkeypatch):
    _three_view_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "data.investigation_service.inspect_targeted_values",
        lambda *a, **k: _view_probe(a[2], k["investigation_type"]),
    )

    def _explode(*a, **k):
        raise AssertionError("gather_candidate_evidence must never call the adjudicator itself")

    monkeypatch.setattr("core.semantic.candidate_adjudicator.adjudicate_candidates", _explode)

    session = RequestMetadataSession(1, "u1")
    try:
        gather_candidate_evidence(1, "u1", "widget", session=session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Request isolation
# ---------------------------------------------------------------------------

def test_new_request_session_gets_fresh_probes(tmp_path, monkeypatch):
    _three_view_fixture(tmp_path, monkeypatch)
    calls = []

    def counting_probe(source_id, user_id, table_fqn, column_name, *, investigation_type, reason):
        calls.append(1)
        return _view_probe(table_fqn, investigation_type)

    monkeypatch.setattr("data.investigation_service.inspect_targeted_values", counting_probe)

    session_a = RequestMetadataSession(1, "u1")
    try:
        gather_candidate_evidence(1, "u1", "widget", session=session_a)
    finally:
        session_a.close()
    first_request_calls = len(calls)
    assert first_request_calls > 0

    session_b = RequestMetadataSession(1, "u1")
    try:
        gather_candidate_evidence(1, "u1", "widget", session=session_b)
    finally:
        session_b.close()

    assert len(calls) == first_request_calls * 2  # session_b repeats every probe -- nothing inherited
