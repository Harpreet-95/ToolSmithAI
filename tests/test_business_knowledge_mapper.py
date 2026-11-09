"""
Business Knowledge mapper tests -- Phase 7, Step 3J, Part 6.

Covers:
  - object/column hallucinations are rejected
  - Tier 3/4 objects cannot become verified mappings
  - unsafe-dependency objects cannot become verified mappings
  - uncertain (low-confidence) mappings remain unresolved, never verified
  - unchanged schema/package causes zero repeat provider calls
  - a NEW concept in a changed package only triggers a provider call for
    that concept, not a full re-map
  - schema fingerprint changes invalidate the "reuse" fast path
  - no package for a source is a clean no-op (None), never an error

Reuses the SQLite fixture plumbing and fakes already established in
tests/test_sqlservice_schema_frontier.py / tests/sqlservice_fixtures.py.
"""
from __future__ import annotations

import shutil

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as schema_object_authority_module
import data.business_knowledge_mapping_store as mapping_store_module
import data.business_knowledge_package_service as package_service_module
import data.db as data_db_module

from core.sqlservice.business_knowledge_mapper import (
    _verify_proposal,
    compute_package_content_hash,
    run_business_knowledge_mapping,
)
from core.sqlservice.schema_object_authority import (
    AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
    AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
    AUTHORITY_CLASS_UNSAFE_DEPENDENCY,
)
from core.sqlservice.business_knowledge_mapping_providers import ProviderMappingProposalResponse
from core.sqlservice.business_concept_index import clear_business_concept_index_cache
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    DIMS, MODEL, PROVIDER, SOURCE_ID, USER_ID,
    FakeEmbeddingProvider, _insert_snapshot, _insert_source, _make_connection,
    template_db_path,  # noqa: F401
)
from tests.test_sqlservice_schema_frontier import _build_with_docs, _doc, _unit


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_object_authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(package_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(mapping_store_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_caches():
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()


class FakeMappingProvider:
    """Returns a caller-controlled sequence of raw mapping-proposal
    responses, one per call, and counts calls made."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model-1"

    def propose_mappings(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        response = self._responses[self.calls] if self.calls < len(self._responses) else {"mappings": []}
        self.calls += 1
        return ProviderMappingProposalResponse(raw=response, model=self.model)


def _standard_build(db_path):
    docs = [
        _doc("dbo.students", tier=1, column_names=("student_id", "full_name", "enrollment_date")),
        _doc("dbo.archive_students_2019", tier=3, column_names=("student_id", "full_name")),
    ]
    vectors = {
        docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        docs[1].document_hash: _unit(0.0, 1.0, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)
    return build


def _make_package_with_student_concept(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(
        pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE",
    )
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])
    return package_service_module.get_active_package(SOURCE_ID, USER_ID)


def test_no_active_package_is_a_clean_noop(db_path):
    build = _standard_build(db_path)
    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([]),
    )
    assert result is None


def test_valid_mapping_is_verified(db_path):
    # _insert_source is called twice (once by _standard_build's helper via
    # _build_with_docs, once here) -- both target the same source_id, so we
    # build first, then create the package against the already-inserted source.
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": ["student_id", "full_name"],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": ["object name and columns match the Student concept"],
        }],
    }
    mapping_provider = FakeMappingProvider([fake_response])

    report = run_business_knowledge_mapping(SOURCE_ID, USER_ID, build.id, embedding_provider, mapping_provider)
    assert report.reused is False
    assert report.provider_calls_made == 1
    assert report.verified_count == 1
    assert report.rejected_count == 0

    mappings = mapping_store_module.list_mappings(pkg["id"])
    assert len(mappings) == 1
    assert mappings[0]["status"] == "VERIFIED"
    assert mappings[0]["object_fqn"] == "dbo.students"
    assert set(mappings[0]["columns"]) == {"student_id", "full_name"}


def test_hallucinated_object_is_rejected(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.this_table_does_not_exist", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.95,
            "evidence": ["looks right"],
        }],
    }
    report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([fake_response]),
    )
    assert report.verified_count == 0
    assert report.rejected_count == 1
    assert report.rejected_reasons.get("object_not_found") == 1


def test_hallucinated_column_is_dropped_but_mapping_still_verified(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.students",
            "columns": ["student_id", "not_a_real_column"],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": [],
        }],
    }
    report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([fake_response]),
    )
    assert report.verified_count == 1
    mappings = mapping_store_module.list_mappings(pkg["id"])
    assert mappings[0]["columns"] == ["student_id"]
    assert any("discarded non-existent column" in e for e in mappings[0]["evidence"])


def test_tier3_object_cannot_become_verified(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.archive_students_2019", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.95,
            "evidence": [],
        }],
    }
    report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([fake_response]),
    )
    assert report.verified_count == 0
    assert report.rejected_reasons.get("ineligible_tier") == 1


def test_low_confidence_stays_unresolved_never_verified(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "CONDITIONAL", "confidence": 0.2,
            "evidence": ["weak match"],
        }],
    }
    report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([fake_response]),
    )
    assert report.verified_count == 0
    assert report.unresolved_count == 1
    assert report.rejected_count == 0


def test_unknown_concept_id_is_rejected(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "NOT_A_REAL_CONCEPT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.95,
            "evidence": [],
        }],
    }
    report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([fake_response]),
    )
    assert report.rejected_reasons.get("unknown_concept") == 1


def test_unchanged_package_and_schema_causes_zero_repeat_provider_calls(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    fake_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": [],
        }],
    }
    first_provider = FakeMappingProvider([fake_response])
    first_report = run_business_knowledge_mapping(SOURCE_ID, USER_ID, build.id, embedding_provider, first_provider)
    assert first_report.provider_calls_made == 1

    second_provider = FakeMappingProvider([fake_response])
    second_report = run_business_knowledge_mapping(SOURCE_ID, USER_ID, build.id, embedding_provider, second_provider)
    assert second_report.reused is True
    assert second_report.provider_calls_made == 0
    assert second_provider.calls == 0


def test_new_concept_in_changed_package_only_maps_the_new_concept(db_path):
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    first_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": [],
        }],
    }
    first_report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([first_response]),
    )
    assert first_report.provider_calls_made == 1

    # Package content changes: a brand-new concept is added. This changes
    # compute_package_content_hash's output, so the (schema_fingerprint,
    # content_hash) generation key is new -- but STUDENT already has a
    # mapping tagged with a DIFFERENT generation, so only the new concept
    # should trigger a provider call this time (batch_size=1 makes the
    # per-concept call boundary explicit and easy to assert on).
    package_service_module.upsert_concept(pkg["id"], "SQL_PROGRAM", "PROGRAM", "SQL Program", origin="AUTHORITATIVE")

    second_response = {
        "mappings": [{
            "concept_id": "SQL_PROGRAM", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "DETAIL_ACTIVITY_SOURCE", "mapping_kind": "CONDITIONAL", "confidence": 0.7,
            "evidence": [],
        }],
    }
    second_provider = FakeMappingProvider([second_response])
    second_report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, second_provider, batch_size=1,
    )
    assert second_report.reused is False
    assert second_report.provider_calls_made == 1  # only SQL_PROGRAM was pending
    assert second_provider.calls == 1


class _FakeDoc:
    def __init__(self, object_fqn, retrieval_tier=1, columns=("id",), object_type="TABLE"):
        self.object_fqn = object_fqn
        self.retrieval_tier = retrieval_tier
        self.metadata = {"column_names": columns}
        self.object_type = object_type


class _FakeAuthority:
    def __init__(self, authority_class, restriction_status="UNKNOWN", grain_class="UNKNOWN"):
        self.authority_class = authority_class
        self.restriction_status = restriction_status
        self.grain_class = grain_class


_NO_CONCEPTS: dict = {}
_NO_FAMILIES: dict = {}


def test_verify_proposal_rejects_unsafe_dependency_regardless_of_confidence():
    doc = _FakeDoc("dbo.vw_risky", retrieval_tier=1)
    authority = _FakeAuthority(AUTHORITY_CLASS_UNSAFE_DEPENDENCY)
    raw = {
        "concept_id": "STUDENT", "object_fqn": "dbo.vw_risky", "columns": [],
        "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.99,
        "evidence": [],
    }
    verified = _verify_proposal(
        raw, known_concept_ids={"STUDENT"},
        documents_by_fqn_lower={"dbo.vw_risky": doc},
        authority_by_fqn_lower={"dbo.vw_risky": authority},
        min_verified_confidence=0.6,
        concepts_by_id=_NO_CONCEPTS, program_family_by_concept_id=_NO_FAMILIES, package_id=1,
    )
    assert verified.status == "REJECTED"
    assert verified.rejection_reason == "unsafe_dependency"


def test_verify_proposal_records_restriction_and_grain_evidence_without_blocking():
    """Restriction/grain evidence recording applies to any non-PREFERRED_
    BUSINESS_VIEW role (that specific role has its own dedicated, stricter
    view/authority gate -- see the Rule A tests below) -- FEE_SOURCE here
    proves the restriction/grain annotation still fires without blocking."""
    doc = _FakeDoc("dbo.vw_filtered", retrieval_tier=1, object_type="VIEW")
    authority = _FakeAuthority("RESTRICTED_VIEW", restriction_status="FILTERED", grain_class="AGGREGATED")
    raw = {
        "concept_id": "STUDENT", "object_fqn": "dbo.vw_filtered", "columns": [],
        "mapping_role": "FEE_SOURCE", "mapping_kind": "CONDITIONAL", "confidence": 0.8,
        "evidence": ["matches concept"],
    }
    verified = _verify_proposal(
        raw, known_concept_ids={"STUDENT"},
        documents_by_fqn_lower={"dbo.vw_filtered": doc},
        authority_by_fqn_lower={"dbo.vw_filtered": authority},
        min_verified_confidence=0.6,
        concepts_by_id=_NO_CONCEPTS, program_family_by_concept_id=_NO_FAMILIES, package_id=1,
    )
    assert verified.status == "VERIFIED"  # not blindly rejected just for being restricted/aggregated
    assert any("restriction_status=FILTERED" in e for e in verified.evidence)
    assert any("grain_class=AGGREGATED" in e for e in verified.evidence)


# ── Step 3J2, Part 3/5 -- generic verification hardening ──────────────────


def test_preferred_business_view_rejects_a_physical_table():
    doc = _FakeDoc("dbo.some_table", retrieval_tier=1, object_type="TABLE")
    authority = _FakeAuthority(AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW)  # even with strong authority
    raw = {
        "concept_id": "STUDENT", "object_fqn": "dbo.some_table", "columns": [],
        "mapping_role": "PREFERRED_BUSINESS_VIEW", "mapping_kind": "DIRECT", "confidence": 0.95,
        "evidence": [],
    }
    verified = _verify_proposal(
        raw, known_concept_ids={"STUDENT"},
        documents_by_fqn_lower={"dbo.some_table": doc},
        authority_by_fqn_lower={"dbo.some_table": authority},
        min_verified_confidence=0.6,
        concepts_by_id=_NO_CONCEPTS, program_family_by_concept_id=_NO_FAMILIES, package_id=1,
    )
    assert verified.status == "REJECTED"
    assert verified.rejection_reason == "preferred_view_requires_view_object"


def test_preferred_business_view_rejects_unproven_view_regardless_of_ai_confidence():
    doc = _FakeDoc("dbo.some_view", retrieval_tier=1, object_type="VIEW")
    authority = _FakeAuthority("UNKNOWN")  # a real view, but no structural authority proof
    raw = {
        "concept_id": "STUDENT", "object_fqn": "dbo.some_view", "columns": [],
        "mapping_role": "PREFERRED_BUSINESS_VIEW", "mapping_kind": "DIRECT", "confidence": 0.99,
        "evidence": [],
    }
    verified = _verify_proposal(
        raw, known_concept_ids={"STUDENT"},
        documents_by_fqn_lower={"dbo.some_view": doc},
        authority_by_fqn_lower={"dbo.some_view": authority},
        min_verified_confidence=0.6,
        concepts_by_id=_NO_CONCEPTS, program_family_by_concept_id=_NO_FAMILIES, package_id=1,
    )
    assert verified.status == "REJECTED"
    assert verified.rejection_reason == "preferred_view_requires_declared_or_proven_authority"


def test_preferred_business_view_accepts_declared_preferred_or_proven_equivalent_view():
    for authority_class in (AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW, AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW):
        doc = _FakeDoc("dbo.some_view", retrieval_tier=1, object_type="VIEW")
        authority = _FakeAuthority(authority_class)
        raw = {
            "concept_id": "STUDENT", "object_fqn": "dbo.some_view", "columns": [],
            "mapping_role": "PREFERRED_BUSINESS_VIEW", "mapping_kind": "DIRECT", "confidence": 0.8,
            "evidence": [],
        }
        verified = _verify_proposal(
            raw, known_concept_ids={"STUDENT"},
            documents_by_fqn_lower={"dbo.some_view": doc},
            authority_by_fqn_lower={"dbo.some_view": authority},
            min_verified_confidence=0.6,
            concepts_by_id=_NO_CONCEPTS, program_family_by_concept_id=_NO_FAMILIES, package_id=1,
        )
        assert verified.status == "VERIFIED", authority_class


def test_force_concept_ids_reprocesses_unchanged_concept_while_skipping_others(db_path):
    """Step 3J1: a concept whose content and schema are both unchanged is
    normally skipped -- force_concept_ids overrides that for exactly the
    named concept_ids, while every other unchanged concept still costs zero
    provider calls."""
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.upsert_concept(pkg["id"], "MENTOR", "ENTITY", "Mentor", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    first_response = {
        "mappings": [{
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": [],
        }, {
            "concept_id": "MENTOR", "object_fqn": "dbo.archive_students_2019", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.5,
            "evidence": [],
        }],
    }
    first_report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([first_response]),
    )
    assert first_report.provider_calls_made == 1

    # Rerun with force_concept_ids={"MENTOR"} and no content/schema change at
    # all -- MENTOR must be re-proposed (one provider call for its own
    # batch), STUDENT must NOT (its content and schema are unchanged).
    second_response = {"mappings": [{
        "concept_id": "MENTOR", "object_fqn": "dbo.students", "columns": [],
        "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
        "evidence": [],
    }]}
    second_provider = FakeMappingProvider([second_response])
    second_report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, second_provider,
        batch_size=1, force_concept_ids=frozenset({"MENTOR"}),
    )
    assert second_report.reused is False
    assert second_report.provider_calls_made == 1
    assert second_provider.calls == 1

    mentor_mappings = mapping_store_module.list_mappings(pkg["id"])
    mentor_rows = [m for m in mentor_mappings if m["concept_id"] == "MENTOR"]
    # The first run's MENTOR->dbo.archive_students_2019 proposal (Tier 3)
    # was correctly REJECTED and remains as its own row (different
    # object_fqn -- not overwritten); the forced rerun's MENTOR->
    # dbo.students proposal (Tier 1) is a distinct, newly-VERIFIED row.
    verified_mentor_rows = [m for m in mentor_rows if m["status"] == "VERIFIED"]
    assert len(verified_mentor_rows) == 1
    assert verified_mentor_rows[0]["object_fqn"] == "dbo.students"


def test_storage_integrity_error_is_caught_and_counted_not_fatal(db_path, monkeypatch):
    """A verified/unresolved proposal that the underlying table's own CHECK
    constraint refuses (e.g. a role an already-deployed table doesn't yet
    accept) must never crash the run -- it is dropped, counted in
    storage_rejected_count, and every other proposal in the same batch
    still persists normally."""
    import sqlite3
    import data.business_knowledge_mapping_store as mapping_store_module_ref
    import core.sqlservice.business_knowledge_mapper as mapper_module

    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    real_upsert = mapping_store_module_ref.upsert_mapping

    def _flaky_upsert(*args, **kwargs):
        if kwargs.get("mapping_role") == "DETAIL_ACTIVITY_SOURCE" or (len(args) > 3 and args[3] == "DETAIL_ACTIVITY_SOURCE"):
            raise sqlite3.IntegrityError("CHECK constraint failed: mapping_role")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(mapper_module.mapping_store, "upsert_mapping", _flaky_upsert)

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    response = {"mappings": [
        {
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "CANONICAL_ENTITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": [],
        },
        {
            "concept_id": "STUDENT", "object_fqn": "dbo.students", "columns": [],
            "mapping_role": "DETAIL_ACTIVITY_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9,
            "evidence": [],
        },
    ]}
    report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([response]),
    )
    assert report.error is None
    assert report.storage_rejected_count == 1
    assert report.verified_count == 1  # the CANONICAL_ENTITY_SOURCE row still persisted


def test_payment_family_conflict_rejects_cross_program_fee_mapping(db_path):
    """A payment-plan object already VERIFIED for PROGRAM_A's fee concept
    must not also become VERIFIED for PROGRAM_B's fee concept when the
    package's own relationships place the two under disjoint program
    families -- purely from concept_type/relationships, no object names."""
    build = _standard_build(db_path)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "PROGRAM_A", "PROGRAM", "Program A", origin="AUTHORITATIVE")
    package_service_module.upsert_concept(pkg["id"], "PROGRAM_B", "PROGRAM", "Program B", origin="AUTHORITATIVE")
    package_service_module.upsert_concept(pkg["id"], "FEE_A", "PAYMENT_CONCEPT", "Fee A", origin="AUTHORITATIVE")
    package_service_module.upsert_concept(pkg["id"], "FEE_B", "PAYMENT_CONCEPT", "Fee B", origin="AUTHORITATIVE")
    package_service_module.upsert_relationship(pkg["id"], "PROGRAM_A", "HAS_FEE", "FEE_A", origin="AUTHORITATIVE")
    package_service_module.upsert_relationship(pkg["id"], "PROGRAM_B", "HAS_FEE", "FEE_B", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    embedding_provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    first_response = {"mappings": [{
        "concept_id": "FEE_A", "object_fqn": "dbo.students", "columns": [],
        "mapping_role": "FEE_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9, "evidence": [],
    }]}
    first_report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([first_response]),
    )
    assert first_report.verified_count == 1

    second_response = {"mappings": [{
        "concept_id": "FEE_B", "object_fqn": "dbo.students", "columns": [],
        "mapping_role": "FEE_SOURCE", "mapping_kind": "DIRECT", "confidence": 0.9, "evidence": [],
    }]}
    second_report = run_business_knowledge_mapping(
        SOURCE_ID, USER_ID, build.id, embedding_provider, FakeMappingProvider([second_response]),
        force_concept_ids=frozenset({"FEE_B"}),
    )
    assert second_report.rejected_reasons.get("payment_family_conflict") == 1

    mappings = mapping_store_module.list_mappings(pkg["id"])
    fee_a = next(m for m in mappings if m["concept_id"] == "FEE_A")
    assert fee_a["status"] == "VERIFIED"  # historical evidence never touched/deleted
    fee_b = next(m for m in mappings if m["concept_id"] == "FEE_B")
    assert fee_b["status"] == "REJECTED"
    assert fee_b["rejection_reason"] == "payment_family_conflict"


def test_compute_program_families_reaches_program_through_multiple_hops():
    from core.sqlservice.business_knowledge_mapper import _compute_program_families

    concepts = [
        {"concept_id": "PROGRAM_A", "concept_type": "PROGRAM"},
        {"concept_id": "ENROLLED_A", "concept_type": "LIFECYCLE_STAGE"},
        {"concept_id": "FEE_PAID_A", "concept_type": "PAYMENT_CONCEPT"},
        {"concept_id": "UNRELATED", "concept_type": "PAYMENT_CONCEPT"},
    ]
    relationships = [
        {"from_concept_id": "ENROLLED_A", "relationship_type": "HAS_PAYMENT", "to_concept_id": "FEE_PAID_A"},
        {"from_concept_id": "PROGRAM_A", "relationship_type": "HAS_ENROLLMENT", "to_concept_id": "ENROLLED_A"},
    ]
    families = _compute_program_families(concepts, relationships)
    assert families["FEE_PAID_A"] == frozenset({"PROGRAM_A"})  # 2 hops away
    assert families["UNRELATED"] == frozenset()  # no path to any PROGRAM concept


def test_compute_package_content_hash_changes_when_a_concept_is_added(db_path):
    _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID)
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    before = compute_package_content_hash(pkg["id"])

    package_service_module.upsert_concept(pkg["id"], "SQL_PROGRAM", "PROGRAM", "SQL Program", origin="AUTHORITATIVE")
    after = compute_package_content_hash(pkg["id"])
    assert before != after
