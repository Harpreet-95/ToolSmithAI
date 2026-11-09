"""
Tests for data/request_metadata_session.py — Phase 3.2A / Tasks 1-3, 6, 7.
"""
from __future__ import annotations

import threading

import pytest

from data.request_metadata_session import (
    MetadataSearchFailedError,
    RequestMetadataSession,
)


def _init_db(tmp_path, monkeypatch):
    import data.db as db_module
    import data.models as models
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "session_test.db"))
    models.init_db()


def test_session_opens_exactly_one_connection(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    try:
        assert session.connections_opened == 1
        # The same connection object is reused for arbitrary reads.
        row1 = session.conn.execute("SELECT 1 AS x").fetchone()
        row2 = session.conn.execute("SELECT 2 AS x").fetchone()
        assert row1["x"] == 1
        assert row2["x"] == 2
    finally:
        session.close()


def test_session_closes_connection_on_normal_exit(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    with RequestMetadataSession(1, "u1") as session:
        conn = session.conn
    with pytest.raises(Exception):
        conn.execute("SELECT 1")  # closed connections raise ProgrammingError


def test_session_closes_connection_even_on_exception(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    conn_ref = {}
    with pytest.raises(ValueError):
        with RequestMetadataSession(1, "u1") as session:
            conn_ref["conn"] = session.conn
            raise ValueError("boom")
    with pytest.raises(Exception):
        conn_ref["conn"].execute("SELECT 1")


def test_get_or_compute_calls_compute_fn_once_per_key(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    calls = []

    def compute():
        calls.append(1)
        return "value-A"

    r1 = session.get_or_compute("latest_schema_snapshot_id", compute)
    r2 = session.get_or_compute("latest_schema_snapshot_id", compute)
    r3 = session.get_or_compute("latest_schema_snapshot_id", compute)

    assert r1 == r2 == r3 == "value-A"
    assert len(calls) == 1
    assert session.request_context_misses == 1
    assert session.request_context_hits == 2
    session.close()


def test_get_or_compute_different_keys_are_independent(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")

    a = session.get_or_compute("key_a", lambda: "A")
    b = session.get_or_compute("key_b", lambda: "B")

    assert a == "A"
    assert b == "B"
    assert session.request_context_misses == 2
    session.close()


def test_get_or_compute_caches_falsy_and_none_values(tmp_path, monkeypatch):
    """Presence in the cache, not truthiness, decides hit vs. miss — a
    constant that legitimately resolves to None/0/False must not be
    recomputed on every call."""
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    calls = []

    def compute():
        calls.append(1)
        return None

    session.get_or_compute("maybe_none", compute)
    session.get_or_compute("maybe_none", compute)

    assert len(calls) == 1
    assert session.request_context_hits == 1
    session.close()


def test_search_dedup_map_executes_identical_search_once(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    calls = []

    def do_search():
        calls.append(1)
        return ["table_a", "table_b"]

    key = (1, ("launch", "participants"), "table", None)
    r1 = session.get_or_compute_search(key, do_search)
    r2 = session.get_or_compute_search(key, do_search)

    assert r1 == r2 == ["table_a", "table_b"]
    assert len(calls) == 1
    assert session.search_cache_misses == 1
    assert session.search_cache_hits == 1
    session.close()


def test_search_dedup_map_differing_params_do_not_collide(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")

    r1 = session.get_or_compute_search((1, ("launch",), "table", None), lambda: ["A"])
    r2 = session.get_or_compute_search((1, ("launch",), "column", None), lambda: ["B"])
    r3 = session.get_or_compute_search((2, ("launch",), "table", None), lambda: ["C"])

    assert r1 == ["A"]
    assert r2 == ["B"]
    assert r3 == ["C"]
    assert session.search_cache_misses == 3
    assert session.search_cache_hits == 0
    session.close()


def test_constants_and_search_cache_are_independent_namespaces(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")

    session.get_or_compute("shared_key", lambda: "constant-value")
    session.get_or_compute_search(("shared_key",), lambda: "search-value")

    assert session.get_or_compute("shared_key", lambda: "should-not-run") == "constant-value"
    assert session.get_or_compute_search(("shared_key",), lambda: "should-not-run") == "search-value"
    session.close()


def test_mark_search_failed_records_reason(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    assert session.search_failed is False
    session.mark_search_failed("MemoryError during column search")
    assert session.search_failed is True
    assert "MemoryError" in session.search_failed_reason
    session.close()


def test_concurrent_sessions_do_not_share_state(tmp_path, monkeypatch):
    """Two RequestMetadataSession instances (simulating two concurrent
    requests) must never see each other's cached constants or connections."""
    _init_db(tmp_path, monkeypatch)
    session_a = RequestMetadataSession(1, "user-a")
    session_b = RequestMetadataSession(2, "user-b")

    session_a.get_or_compute("k", lambda: "value-for-a")
    session_b.get_or_compute("k", lambda: "value-for-b")

    assert session_a.get_or_compute("k", lambda: "unused") == "value-for-a"
    assert session_b.get_or_compute("k", lambda: "unused") == "value-for-b"
    assert session_a.conn is not session_b.conn

    session_a.close()
    session_b.close()


def test_close_is_idempotent(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    session.close()
    session.close()  # must not raise


def test_get_or_compute_thread_safe_under_concurrent_first_access(tmp_path, monkeypatch):
    """Many threads racing to fill the same key must still only observe the
    compute function's result once (first-writer-wins), never a torn or
    duplicated cache entry."""
    _init_db(tmp_path, monkeypatch)
    session = RequestMetadataSession(1, "u1")
    call_count = {"n": 0}
    lock = threading.Lock()

    def compute():
        with lock:
            call_count["n"] += 1
        return "resolved-once"

    results = []

    def worker():
        results.append(session.get_or_compute("racy_key", compute))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == "resolved-once" for r in results)
    session.close()
