"""
Tests for Phase 3.2A / Task 6 (infrastructure-failure handling) and the
remaining Task 7 verification requirements not already covered by
test_request_metadata_session.py or test_business_knowledge_batch.py:
  - a metadata-search infrastructure failure (MemoryError, sqlite3.Error)
    is never silently reported as "zero candidates"
  - an ordinary application-level exception still degrades gracefully
    (unchanged pre-existing contract)
  - passing a RequestMetadataSession never changes retrieval output
  - legacy callers with no session are unaffected
  - two sessions for different sources never see each other's state
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-metadata-infra-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-metadata-infra-salt-long-enough-1234567890")

import pytest

import data.semantic_retrieval_service as srs
from data.query_planning_service import plan_business_query
from data.request_metadata_session import MetadataSearchFailedError, RequestMetadataSession
from tests.test_semantic_retrieval_service import env, _add_table, _add_column


def test_memory_error_raises_metadata_search_failed_not_empty_candidates(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")

    def _boom(*a, **kw):
        raise MemoryError("simulated exhaustion")
    monkeypatch.setattr(srs, "_search_all", _boom)

    with pytest.raises(MetadataSearchFailedError):
        srs.get_candidate_tables_with_ranking(1, "u1", "students", ["students"])


def test_sqlite_error_raises_metadata_search_failed_not_empty_candidates(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(srs, "_search_all", _boom)

    with pytest.raises(MetadataSearchFailedError):
        srs.get_candidate_tables(1, "u1", "students", ["students"])


def test_ordinary_exception_still_degrades_to_empty_candidates(tmp_path, monkeypatch):
    """Pre-existing contract preserved: an application-level bug (not an
    infrastructure failure) must not become a hard crash — only the
    MemoryError/sqlite3.Error/OSError category is promoted."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")

    def _boom(*a, **kw):
        raise KeyError("some unrelated bug")
    monkeypatch.setattr(srs, "_search_all", _boom)

    result = srs.get_candidate_tables_with_ranking(1, "u1", "students", ["students"])
    assert result == (set(), [], [])


def test_plan_business_query_reports_metadata_search_failed_flag(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")
    _add_column(db, "dbo.students", "id")

    def _boom(*a, **kw):
        raise MemoryError("simulated exhaustion")
    monkeypatch.setattr(srs, "_search_all", _boom)

    result = plan_business_query(1, "u1", {"question": "students", "concepts": ["students"]})

    assert result is not None
    assert result["metadata_search_failed"] is True
    assert result["metadata_search_failed_reason"] is not None
    assert result["tables"] == []


def test_plan_business_query_no_failure_flag_on_genuine_no_match(tmp_path, monkeypatch):
    """A real "nothing matched" outcome (no infrastructure problem) must NOT
    set metadata_search_failed — the two cases must stay distinguishable."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")

    result = plan_business_query(1, "u1", {"question": "zzz_no_such_term", "concepts": ["zzz_no_such_term"]})

    assert result is not None
    assert result["metadata_search_failed"] is False
    assert result["metadata_search_failed_reason"] is None


def test_session_does_not_change_retrieval_output(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students", domain="Academics", domain_confidence=0.9)
    _add_table(db, "dbo.homework", business_name="Homework")
    _add_column(db, "dbo.students", "name", business_label="Student Name")
    _add_column(db, "dbo.homework", "status", business_label="Homework Status")

    without_session = srs.get_candidate_tables_with_ranking(1, "u1", "students homework", ["students", "homework"])

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db)
    session = RequestMetadataSession(1, "u1")
    try:
        with_session = srs.get_candidate_tables_with_ranking(
            1, "u1", "students homework", ["students", "homework"], session=session,
        )
    finally:
        session.close()

    assert with_session == without_session


def test_plan_business_query_with_session_matches_without(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")
    _add_column(db, "dbo.students", "id")

    request = {"question": "students", "concepts": ["students"], "measures": ["students"]}
    without_session = plan_business_query(1, "u1", request)

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db)
    session = RequestMetadataSession(1, "u1")
    try:
        with_session = plan_business_query(1, "u1", request, session=session)
    finally:
        session.close()

    assert with_session == without_session


def test_legacy_callers_without_session_are_unaffected(tmp_path, monkeypatch):
    """No session argument at all (the pre-Phase-3.2A calling convention)
    must keep working exactly as before — every positional-only call site
    across the existing codebase relies on this."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")

    result = srs.get_candidate_tables(1, "u1", "students", ["students"])
    assert "dbo.students" in result


def test_two_sessions_for_different_sources_stay_isolated(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.students", business_name="Students")

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db)

    session_a = RequestMetadataSession(1, "user-a")
    session_b = RequestMetadataSession(999, "user-b")  # unowned/unknown source
    try:
        result_a = srs.get_candidate_tables_with_ranking(1, "user-a", "students", ["students"], session=session_a)
        result_b = srs.get_candidate_tables_with_ranking(999, "user-b", "students", ["students"], session=session_b)

        assert "dbo.students" in result_a[0]
        assert result_b[0] == set()  # source 999 doesn't exist — no cross-contamination
        assert session_a.conn is not session_b.conn
    finally:
        session_a.close()
        session_b.close()
