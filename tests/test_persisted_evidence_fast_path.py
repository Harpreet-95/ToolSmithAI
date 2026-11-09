"""
Executive Report Performance Recovery milestone (2026-08-24), Part 21.

Unit tests for core.semantic.candidate_adjudicator._evaluate_persisted_
evidence_candidate — the new, narrowly-gated fast path that lets a
candidate-adjudication call reuse an ALREADY-PERSISTED, source-scoped
entity_assignments row instead of a fresh AI round trip, when (and only
when) every one of Part 8's strict conditions holds. Every negative test
below proves the gate fails closed (falls through to "defer to AI") on a
specific, real risk: requested attributes present, low confidence, a stale
snapshot, a competing candidate, a restricted view, a derivative table, a
missing persisted row, or an entity-name mismatch.

Run from the project root:
    python -m pytest tests/test_persisted_evidence_fast_path.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-persisted-evidence-fast-path-secret-123")
os.environ.setdefault("USER_ID_SALT", "test-persisted-evidence-fast-path-salt-456")

import pytest

import data.models as models
from core.semantic.candidate_adjudicator import _evaluate_persisted_evidence_candidate

SOURCE_ID = 1


class _FakeSession:
    def __init__(self, conn, source_id):
        self.conn = conn
        self.source_id = source_id


def _db(tmp_path, name="pe.db"):
    path = str(tmp_path / name)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn, path


def _init_schema(conn, db_path, monkeypatch):
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()


def _insert_snapshot(conn, source_id, snapshot_id, *, status="COMPLETE", snapshot_version=2):
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, status, created_at) "
        "VALUES (?, ?, 1, ?, ?, '2026-08-24T00:00:00Z')",
        (snapshot_id, source_id, snapshot_version, status),
    )
    conn.commit()


def _insert_entity_assignment(conn, source_id, table_fqn, entity, confidence, snapshot_id):
    conn.execute(
        "INSERT INTO entity_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, entity, confidence, evidence_json, "
        " competing_entities_json, assignment_source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '[]', '[]', 'rule', '2026-08-24T00:00:00Z', '2026-08-24T00:00:00Z')",
        (source_id, snapshot_id, table_fqn, entity, confidence),
    )
    conn.commit()


def _catalog_candidate(table_fqn="dbo.EDU_Track", *, name_score=0.9, **overrides):
    cand = {
        "table_fqn": table_fqn,
        "candidate_business_role": "catalog_or_entity",
        "derivative_flag": None,
        "object_type": "TABLE",
        "restricted_view_evidence": None,
        "approx_row_count": 103,
        "name_score": name_score,
        "deterministic_rank": 1,
        "score": 0.75,
    }
    cand.update(overrides)
    return cand


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn, db_path = _db(tmp_path)
    _init_schema(conn, db_path, monkeypatch)
    _insert_snapshot(conn, SOURCE_ID, snapshot_id=100)
    session = _FakeSession(conn, SOURCE_ID)
    return conn, session


class TestHighConfidencePersistedEvidenceReusedSafely:
    def test_single_catalog_candidate_matching_persisted_entity_is_reused(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is not None, reason
        assert result.selected_table_fqn == "dbo.EDU_Track"
        assert result.no_safe_selection is False
        assert result.confidence == 0.75


class TestConflictingEvidenceStillInvokesAI:
    def test_unrelated_second_catalog_candidate_does_not_block_the_real_one(self, env):
        """A weak, unrelated catalog_or_entity candidate that has NO
        persisted entity_assignments row of its own (the common real-world
        shape — live-traced against the reference database: a generic reference table can
        score catalog_or_entity purely from vocabulary overlap) must not
        block the fast path for the ONE candidate that genuinely IS
        persisted under this entity_name."""
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate("dbo.EDU_Track"), _catalog_candidate("dbo.Unrelated_Reference_Table")]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is not None, reason
        assert result.selected_table_fqn == "dbo.EDU_Track"

    def test_two_candidates_both_persisted_under_the_same_entity_defers_to_ai(self, env):
        """Genuine ambiguity: TWO candidates each have their own persisted
        entity_assignments row naming the SAME entity_name — must defer to
        AI, never guess between them."""
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_OtherPrograms", "Program", 0.6, snapshot_id=100)
        evidence = [_catalog_candidate("dbo.EDU_Track"), _catalog_candidate("dbo.EDU_OtherPrograms")]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "2 candidates with matching persisted entity_assignments" in reason


class TestCaseInsensitiveEntityMatch:
    def test_lowercase_question_term_matches_titlecase_persisted_entity(self, env):
        """Real call shape confirmed live against the reference database: ai_physical_planning
        passes lowercase question-derived terms (e.g. "program"), while
        entity_assignments stores canonical Titlecase names (e.g.
        "Program") — the match must be case-insensitive."""
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "program", evidence, None, session=session,
        )
        assert result is not None, reason
        assert result.selected_table_fqn == "dbo.EDU_Track"


class TestStaleSnapshotStillInvokesAI:
    def test_assignment_from_an_older_snapshot_defers_to_ai(self, env):
        conn, session = env
        # Assignment persisted against an OLDER snapshot (99), while the
        # latest is 100 (inserted by the fixture) — schema/relationships may
        # have changed since, so this must not be trusted blindly.
        _insert_snapshot(conn, SOURCE_ID, snapshot_id=99, snapshot_version=1)
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=99)
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "stale" in reason


class TestRequestedAttributesStillInvokeAI:
    def test_requested_attributes_present_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, ["ProgramName"], session=session,
        )
        assert result is None
        assert "requested_attributes" in reason


class TestLowConfidenceStillInvokesAI:
    def test_below_threshold_confidence_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.3, snapshot_id=100)
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "confidence" in reason


class TestEntityNameMismatchStillInvokesAI:
    def test_persisted_entity_differs_from_requested_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Course", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "does not match" in reason


class TestNoPersistedRowStillInvokesAI:
    def test_no_entity_assignments_row_defers_to_ai(self, env):
        conn, session = env
        evidence = [_catalog_candidate()]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "no persisted entity_assignments row" in reason


class TestRestrictedViewStillInvokesAI:
    def test_restricted_view_candidate_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.vw_Path", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate(
            "dbo.vw_Path", object_type="VIEW",
            restricted_view_evidence={"restriction_detected": True},
        )]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "restricted view" in reason

    def test_view_with_unknown_restriction_status_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.vw_Path", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate("dbo.vw_Path", object_type="VIEW", restricted_view_evidence=None)]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "unknown restriction status" in reason


class TestDerivativeCandidateStillInvokesAI:
    def test_derivative_flag_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track_backup", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate("dbo.EDU_Track_backup", derivative_flag="backup")]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "derivative_flag" in reason


class TestNonCatalogRoleStillInvokesAI:
    def test_assignment_or_event_role_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate(candidate_business_role="assignment_or_event")]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "0 catalog_or_entity candidates" in reason


class TestNoSessionStillInvokesAI:
    def test_no_session_defers_to_ai(self):
        evidence = [_catalog_candidate()]
        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=None,
        )
        assert result is None
        assert "no session" in reason


class TestZeroRowCandidateStillInvokesAI:
    def test_zero_row_candidate_defers_to_ai(self, env):
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate(approx_row_count=0)]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is None
        assert "approx_row_count=0" in reason


class TestWeakNameScoreDoesNotBlockPersistedReuse:
    def test_zero_name_score_still_reuses_persisted_evidence(self, env):
        """Deliberately different from _evaluate_fast_path_candidate's own
        name_score gate: a persisted, high-confidence entity_assignments
        classification is exactly what covers a table whose LEXICAL name
        match is weak or zero (the real, live-traced the reference database case — EDU_Track's
        name_score for 'program' is 0.0, since 'path' shares no lexical
        overlap with 'program' at all; its business meaning was established
        through column-name/relationship evidence instead, see Milestone 5).
        Requiring name_score here would make this gate reject exactly the
        cases it exists to serve."""
        conn, session = env
        _insert_entity_assignment(conn, SOURCE_ID, "dbo.EDU_Track", "Program", 0.75, snapshot_id=100)
        evidence = [_catalog_candidate(name_score=0.0)]

        result, reason = _evaluate_persisted_evidence_candidate(
            SOURCE_ID, "Program", evidence, None, session=session,
        )
        assert result is not None, reason
        assert result.selected_table_fqn == "dbo.EDU_Track"
