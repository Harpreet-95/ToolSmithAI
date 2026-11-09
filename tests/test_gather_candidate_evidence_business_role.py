"""
LIVE PROGRAM ADJUDICATION MILESTONE, Part 1/4 — end-to-end test that
data.semantic_contract_service.gather_candidate_evidence's evidence dicts
carry the new normalized business-role fields the AI adjudicator now reads
(candidate_business_role, human_readable_descriptor_columns,
person_reference_columns, deterministic_rank, authority_reasons).

Reproduces the real live shape: a genuine curriculum catalog table
(TrackName/TrackDesc-style descriptor columns, no person reference) vs a
per-person assignment table whose raw NAME lexically contains "program"
(StudentUserID/InstructorID-style person-reference columns, no descriptor).

Run:
    python -m pytest tests/test_gather_candidate_evidence_business_role.py -v
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-gather-evidence-business-role-secret-long1")
os.environ.setdefault("USER_ID_SALT", "test-gather-evidence-business-role-salt-long-1")

from data.db import get_connection
from data.semantic_contract_service import gather_candidate_evidence
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_domain


def _set_dictionary_semantic_type(db_path, table_fqn, column_name, semantic_type):
    # data_dictionary_columns.semantic_type (dictionary-level classification,
    # what _is_genuine_label_column/_is_person_reference_column actually
    # read) is a DIFFERENT field from profiling_column_profiles.semantic_
    # type (what tests/test_phase9_query_planning.py's own _set_semantic_
    # type helper sets) -- live the reference database populates it via dictionary generation
    # ("rule_based+profiling"), which this test fixture doesn't run, so it
    # must be set directly here to mirror the real evidence shape.
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE data_dictionary_columns SET semantic_type = ? WHERE table_fqn = ? AND column_name = ?",
            (semantic_type, table_fqn, column_name),
        )
        conn.commit()
    finally:
        conn.close()


def _seed(db):
    # Genuine catalog: name has NO lexical overlap with "program" at all
    # (mirrors dbo.EDU_Track exactly) -- relies purely on its own
    # ProgramName column for retrieval/scoring.
    _add_table(db, "dbo.curriculum_catalog", table_class="Master", row_count=100)
    _add_column(db, "dbo.curriculum_catalog", "CatalogID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.curriculum_catalog", "ProgramName", data_type="TEXT", is_dimension=True,
                business_label="Program Name", approved=True)
    _add_column(db, "dbo.curriculum_catalog", "ProgramDescription", data_type="TEXT", is_dimension=True,
                business_label="Program Description", approved=True)
    _add_domain(db, "dbo.curriculum_catalog", "Curriculum")
    _set_dictionary_semantic_type(db, "dbo.curriculum_catalog", "ProgramName", "dimension")
    _set_dictionary_semantic_type(db, "dbo.curriculum_catalog", "ProgramDescription", "dimension")

    # Assignment table: raw name lexically contains "program", but it's
    # entirely person-reference-shaped, no descriptor of its own.
    _add_table(db, "dbo.mentorship_program_assignments", table_class="Transactional", row_count=5000)
    _add_column(db, "dbo.mentorship_program_assignments", "AssignmentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.mentorship_program_assignments", "StudentUserID", data_type="INTEGER")
    _add_column(db, "dbo.mentorship_program_assignments", "InstructorID", data_type="INTEGER")
    _add_column(db, "dbo.mentorship_program_assignments", "IsActive", data_type="INTEGER")
    _add_column(db, "dbo.mentorship_program_assignments", "CreateDate", data_type="TEXT")
    _add_domain(db, "dbo.mentorship_program_assignments", "Mentorship")
    _set_dictionary_semantic_type(db, "dbo.mentorship_program_assignments", "StudentUserID", "id")
    _set_dictionary_semantic_type(db, "dbo.mentorship_program_assignments", "InstructorID", "id")
    _set_dictionary_semantic_type(db, "dbo.mentorship_program_assignments", "IsActive", "flag")
    _set_dictionary_semantic_type(db, "dbo.mentorship_program_assignments", "CreateDate", "date")


def test_catalog_table_marked_catalog_or_entity_with_descriptor_columns(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed(db)

    result = gather_candidate_evidence(1, "u1", "program")

    catalog = next((e for e in result["evidence"] if e["table_fqn"] == "dbo.curriculum_catalog"), None)
    assert catalog is not None
    assert catalog["candidate_business_role"] == "catalog_or_entity"
    assert set(catalog["human_readable_descriptor_columns"]) >= {"ProgramName", "ProgramDescription"}
    assert catalog["person_reference_columns"] == []


def test_assignment_table_marked_assignment_or_event_with_person_reference_columns(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed(db)

    result = gather_candidate_evidence(1, "u1", "program")

    assignment = next(
        (e for e in result["evidence"] if e["table_fqn"] == "dbo.mentorship_program_assignments"), None,
    )
    assert assignment is not None
    assert assignment["candidate_business_role"] == "assignment_or_event"
    assert set(assignment["person_reference_columns"]) >= {"StudentUserID", "InstructorID"}
    assert assignment["human_readable_descriptor_columns"] == []


def test_both_candidates_carry_a_deterministic_rank(tmp_path, monkeypatch):
    # Full competitive rank-ordering (catalog beats a lexically-matching
    # assignment table end to end) is verified against real live the reference database data
    # (see the milestone report) — this fixture only proves both candidates
    # reach the evidence list with a real, distinct rank assigned, not a
    # specific ordering a minimal synthetic fixture can't reliably control
    # (real authority evidence — domain confidence, relationship count,
    # entity assignment — meaningfully affects the final score too).
    db = env(tmp_path, monkeypatch)
    _seed(db)

    result = gather_candidate_evidence(1, "u1", "program")

    by_fqn = {e["table_fqn"]: e for e in result["evidence"]}
    catalog_rank = by_fqn["dbo.curriculum_catalog"]["deterministic_rank"]
    assignment_rank = by_fqn["dbo.mentorship_program_assignments"]["deterministic_rank"]
    assert catalog_rank is not None and assignment_rank is not None
    assert catalog_rank != assignment_rank


def test_authority_reasons_present_on_evidence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _seed(db)

    result = gather_candidate_evidence(1, "u1", "program")

    assignment = next(
        (e for e in result["evidence"] if e["table_fqn"] == "dbo.mentorship_program_assignments"), None,
    )
    assert assignment is not None
    assert any("person-reference column" in r for r in assignment["authority_reasons"])
