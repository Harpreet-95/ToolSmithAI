"""
Business Knowledge Package service tests -- Phase 7, Step 3J, Part 6.

Covers Part 6's package-level requirements:
  - packages are source-scoped and owner-scoped
  - authoritative facts cannot be overwritten by AI
  - insert/update operations are idempotent
  - package versioning/activation lifecycle

Run: python -m pytest tests/test_business_knowledge_package_service.py -v
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

import data.models as models_module
import data.business_knowledge_package_service as svc


def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("template") / "template.db")
    original_get_connection = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original_get_connection
    return path


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(svc, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-27T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


# ── Schema shape ────────────────────────────────────────────────────────


def test_tables_and_indexes_exist(db_path):
    conn = _make_connection(db_path)
    for table in (
        "business_knowledge_packages", "business_knowledge_concepts",
        "business_knowledge_relationships", "business_knowledge_facts",
        "business_knowledge_mappings", "business_knowledge_mapping_runs",
    ):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None, f"missing table {table}"
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(business_knowledge_mappings)").fetchall()}
    assert "idx_bkm_package_concept_object_role" in indexes
    conn.close()


# ── Package lifecycle: source/owner scoping, versioning, activation ──────


def test_create_or_get_draft_package_is_source_and_owner_scoped(db_path):
    _insert_source(db_path, 90004, user_id="alice")

    assert svc.create_or_get_draft_package(90004, "bob", "bob") is None  # not owned

    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")
    assert pkg["source_id"] == 90004
    assert pkg["user_id"] == "alice"
    assert pkg["package_version"] == 1
    assert pkg["status"] == "DRAFT"


def test_create_or_get_draft_package_is_idempotent(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    first = svc.create_or_get_draft_package(90004, "alice", "alice")
    second = svc.create_or_get_draft_package(90004, "alice", "alice")
    assert first["id"] == second["id"]
    assert second["package_version"] == 1


def test_activate_package_supersedes_prior_active(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    v1 = svc.create_or_get_draft_package(90004, "alice", "alice")
    activated_v1 = svc.activate_package(90004, "alice", v1["package_version"])
    assert activated_v1["status"] == "ACTIVE"

    # Force a new draft (v2) by superseding v1's DRAFT slot: simulate a
    # second edit round by inserting directly at the next version via the
    # same idempotent path -- create_or_get_draft_package only returns a new
    # version once no DRAFT remains, which is the case now that v1 is ACTIVE.
    v2 = svc.create_or_get_draft_package(90004, "alice", "alice")
    assert v2["package_version"] == 2

    activated_v2 = svc.activate_package(90004, "alice", v2["package_version"])
    assert activated_v2["status"] == "ACTIVE"

    reloaded_v1 = svc.get_package(90004, "alice", package_version=1)
    assert reloaded_v1["status"] == "SUPERSEDED"

    active = svc.get_active_package(90004, "alice")
    assert active["package_version"] == 2


def test_get_active_package_never_returns_draft(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    svc.create_or_get_draft_package(90004, "alice", "alice")
    assert svc.get_active_package(90004, "alice") is None


# ── Concepts: idempotency + authoritative-write guard ─────────────────────


def test_upsert_concept_insert_and_idempotent_update(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")

    created = svc.upsert_concept(
        pkg["id"], "STUDENT", "ENTITY", "Student",
        origin="AUTHORITATIVE", description="A person enrolled in training.",
    )
    assert created["skipped_authoritative_protection"] is False
    assert created["concept_type"] == "ENTITY"

    updated = svc.upsert_concept(
        pkg["id"], "STUDENT", "ENTITY", "Student",
        origin="AUTHORITATIVE", description="Updated description.",
    )
    assert updated["id"] == created["id"]
    assert updated["description"] == "Updated description."

    concepts = svc.list_concepts(pkg["id"])
    assert len(concepts) == 1


def test_ai_cannot_overwrite_authoritative_concept(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")

    svc.upsert_concept(
        pkg["id"], "IPLAN_PROGRAM", "PROGRAM", "IPLAN Training",
        origin="AUTHORITATIVE", description="Owner-confirmed program.",
    )
    ai_attempt = svc.upsert_concept(
        pkg["id"], "IPLAN_PROGRAM", "PROGRAM", "IPLAN Training (AI guess)",
        origin="AI_INFERRED", description="AI-written description -- must not apply.",
    )
    assert ai_attempt["skipped_authoritative_protection"] is True
    assert ai_attempt["description"] == "Owner-confirmed program."
    assert ai_attempt["origin"] == "AUTHORITATIVE"

    stored = svc.get_concept(pkg["id"], "IPLAN_PROGRAM")
    assert stored["description"] == "Owner-confirmed program."


def test_authoritative_write_can_correct_prior_ai_inferred_concept(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")

    svc.upsert_concept(pkg["id"], "SQL_PROGRAM", "PROGRAM", "SQL Program", origin="AI_INFERRED")
    corrected = svc.upsert_concept(
        pkg["id"], "SQL_PROGRAM", "PROGRAM", "Basic SQL Training",
        origin="AUTHORITATIVE", description="Owner correction.",
    )
    assert corrected["skipped_authoritative_protection"] is False
    assert corrected["origin"] == "AUTHORITATIVE"
    assert corrected["display_name"] == "Basic SQL Training"


def test_upsert_concept_rejects_invalid_concept_type(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")
    with pytest.raises(svc.BusinessKnowledgePackageError):
        svc.upsert_concept(pkg["id"], "X", "NOT_A_TYPE", "X", origin="AUTHORITATIVE")


# ── Relationships: idempotency + authoritative-write guard ────────────────


def test_upsert_relationship_idempotent_and_authoritative_guard(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")

    svc.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    svc.upsert_concept(pkg["id"], "SQL_PROGRAM", "PROGRAM", "SQL Program", origin="AUTHORITATIVE")

    rel = svc.upsert_relationship(
        pkg["id"], "STUDENT", "PARTICIPATES_IN", "SQL_PROGRAM",
        origin="AUTHORITATIVE", evidence=["owner-confirmed"],
    )
    assert rel["skipped_authoritative_protection"] is False

    rel_again = svc.upsert_relationship(
        pkg["id"], "STUDENT", "PARTICIPATES_IN", "SQL_PROGRAM", origin="AUTHORITATIVE",
    )
    assert rel_again["id"] == rel["id"]
    assert len(svc.list_relationships(pkg["id"])) == 1

    ai_attempt = svc.upsert_relationship(
        pkg["id"], "STUDENT", "PARTICIPATES_IN", "SQL_PROGRAM",
        origin="AI_INFERRED", confidence=0.1,
    )
    assert ai_attempt["skipped_authoritative_protection"] is True
    assert ai_attempt["origin"] == "AUTHORITATIVE"


# ── Authoritative facts: sole writer, idempotent, never AI-writable ───────


def test_store_authoritative_fact_idempotent(db_path):
    _insert_source(db_path, 90004, user_id="alice")
    pkg = svc.create_or_get_draft_package(90004, "alice", "alice")

    fact = svc.store_authoritative_fact(
        pkg["id"], "SQL_PROGRAM_DURATION", "The SQL program lasts approximately three months.",
        related_concept_id="SQL_PROGRAM", created_by="alice",
    )
    assert fact["fact_key"] == "SQL_PROGRAM_DURATION"

    updated = svc.store_authoritative_fact(
        pkg["id"], "SQL_PROGRAM_DURATION", "Updated: about three months, sometimes longer.",
        created_by="alice",
    )
    assert updated["id"] == fact["id"]
    assert len(svc.list_facts(pkg["id"])) == 1


def test_business_knowledge_facts_module_has_no_origin_parameter():
    """business_knowledge_facts rows are always authoritative by
    construction -- store_authoritative_fact has no `origin` parameter at
    all, so there is no code path in this module that could write an
    AI-labeled fact row."""
    import inspect
    sig = inspect.signature(svc.store_authoritative_fact)
    assert "origin" not in sig.parameters
