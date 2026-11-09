"""
Business-knowledge evidence x schema-frontier integration tests -- Phase 7,
Step 3J, Part 5/6.

Covers:
  - a source with no ACTIVE package sees zero business_knowledge_mappings
    on every candidate (the generic fallback, unaffected by this feature)
  - the feature flag (core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED)
    gates evidence attachment even when a package/mapping exists
  - a VERIFIED mapping surfaces on its object's own FrontierCandidate with
    the right concept/role/status/origin/confidence fields
  - a REJECTED mapping never surfaces as evidence
  - persistent cache reuse: a second build_schema_frontier call against an
    unchanged package/schema does not grow the evidence cache
  - no domain-specific (the reference database/ExampleCo/golden-question) tokens appear in
    any of the new production modules
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

import core.config as config_module
from core.sqlservice.business_concept_index import clear_business_concept_index_cache
from core.sqlservice.business_knowledge_evidence import (
    business_knowledge_evidence_cache_size,
    clear_business_knowledge_evidence_cache,
)
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.schema_frontier import build_schema_frontier
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    SOURCE_ID, USER_ID, FakeEmbeddingProvider, _insert_source, _make_connection,
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
    clear_business_knowledge_evidence_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()
    clear_business_knowledge_evidence_cache()


def _standard_build(db_path):
    docs = [_doc("dbo.students", tier=1, column_names=("student_id", "full_name"))]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    return _build_with_docs(db_path, docs, vectors)


def _make_package_with_verified_mapping(db_path, build):
    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT", "ENTITY", "Student", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])

    schema_fingerprint = retrieval_store_module.get_retrieval_build(build.id, SOURCE_ID, USER_ID).schema_fingerprint
    mapping_store_module.upsert_mapping(
        pkg["id"], "STUDENT", "dbo.students", "CANONICAL_ENTITY_SOURCE",
        columns=["student_id"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["owner-confirmed entity"], schema_fingerprint=schema_fingerprint,
        content_hash="a" * 64,
    )
    mapping_store_module.upsert_mapping(
        pkg["id"], "STUDENT", "dbo.students", "DETAIL_ACTIVITY_SOURCE",
        columns=[], mapping_kind="UNRESOLVED", status="REJECTED", confidence=0.95,
        evidence=[], schema_fingerprint=schema_fingerprint, content_hash="b" * 64,
        rejection_reason="object_not_found",
    )
    return pkg, schema_fingerprint


def test_no_package_means_no_business_knowledge_evidence(db_path, monkeypatch):
    monkeypatch.setattr(config_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    build = _standard_build(db_path)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("students", build.id, SOURCE_ID, USER_ID, provider)
    assert all(c.business_knowledge_mappings == () for c in result.candidates)


def test_flag_disabled_suppresses_evidence_even_with_verified_mapping(db_path, monkeypatch):
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", False)

    build = _standard_build(db_path)
    _make_package_with_verified_mapping(db_path, build)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("students", build.id, SOURCE_ID, USER_ID, provider)
    assert all(c.business_knowledge_mappings == () for c in result.candidates)


def test_verified_mapping_surfaces_rejected_mapping_never_does(db_path, monkeypatch):
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    build = _standard_build(db_path)
    _make_package_with_verified_mapping(db_path, build)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("students", build.id, SOURCE_ID, USER_ID, provider)

    candidate = next(c for c in result.candidates if c.object_fqn == "dbo.students")
    roles = {m.mapping_role for m in candidate.business_knowledge_mappings}
    assert roles == {"CANONICAL_ENTITY_SOURCE"}  # DETAIL_ACTIVITY_SOURCE was REJECTED, must not appear

    evidence = candidate.business_knowledge_mappings[0]
    assert evidence.concept_id == "STUDENT"
    assert evidence.concept_type == "ENTITY"
    assert evidence.concept_origin == "AUTHORITATIVE"
    assert evidence.mapping_status == "VERIFIED"
    assert evidence.confidence == pytest.approx(0.9)

    rendered = frontier_module.render_frontier_candidate(candidate)
    assert "BUSINESS_KNOWLEDGE:" in rendered
    assert "STUDENT:CANONICAL_ENTITY_SOURCE(0.90)" in rendered


def test_mapping_from_a_stale_schema_fingerprint_never_surfaces(db_path, monkeypatch):
    """A mapping tagged with an OLD schema_fingerprint (from before a real
    schema change) must never surface as evidence once the build's current
    schema_fingerprint has moved on -- Part 6's "schema fingerprint changes
    invalidate mappings safely" requirement. The stale row is left in place
    (never deleted -- an audit trail), simply never read for a different
    fingerprint."""
    from core.sqlservice.business_knowledge_evidence import load_business_knowledge_evidence

    build = _standard_build(db_path)
    pkg, current_fingerprint = _make_package_with_verified_mapping(db_path, build)

    stale_fingerprint = "0" * 64
    assert stale_fingerprint != current_fingerprint
    mapping_store_module.upsert_mapping(
        pkg["id"], "STUDENT", "dbo.students", "PREFERRED_BUSINESS_VIEW",
        columns=[], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["from a since-superseded schema"], schema_fingerprint=stale_fingerprint,
        content_hash="c" * 64,
    )

    clear_business_knowledge_evidence_cache()
    result = load_business_knowledge_evidence(SOURCE_ID, USER_ID, current_fingerprint)
    roles = {m.mapping_role for m in result.mappings_by_object_fqn_lower.get("dbo.students", ())}
    assert "PREFERRED_BUSINESS_VIEW" not in roles  # tagged with the stale fingerprint, correctly excluded
    assert "CANONICAL_ENTITY_SOURCE" in roles  # tagged with the current fingerprint, still visible

    stale_result = load_business_knowledge_evidence(SOURCE_ID, USER_ID, stale_fingerprint)
    stale_roles = {m.mapping_role for m in stale_result.mappings_by_object_fqn_lower.get("dbo.students", ())}
    assert stale_roles == {"PREFERRED_BUSINESS_VIEW"}  # still readable under ITS OWN fingerprint, just isolated


def test_business_knowledge_evidence_survives_character_budget_for_a_wide_object(db_path, monkeypatch):
    """Regression test for a real defect found live against the reference database: a wide
    (24-column) object with several business-knowledge mappings had its
    entire business_knowledge_mappings field silently dropped by
    core.sqlservice.schema_frontier._fit_to_character_budget's own
    DEFAULT_MAX_CHARACTERS_PER_OBJECT=600 -- the verbose original render
    format (concept_type + mapping_role + mapping_status + concept_origin
    + confidence per entry) pushed a genuinely wide candidate's rendered
    text over budget before its ordinary column categories were even
    considered for trimming. Fixed by rendering each entry compactly
    (concept_id:mapping_role(confidence), with status/origin appended only
    when they differ from the common case) -- this test constructs an
    equally wide object with 3 mappings and asserts the evidence survives."""
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    wide_columns = tuple(f"column_{i}_with_a_realistic_name" for i in range(24))
    docs = [_doc("dbo.wide_table", tier=1, column_names=wide_columns)]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)

    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "ENTITY_A", "ENTITY", "Entity A", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])
    schema_fingerprint = retrieval_store_module.get_retrieval_build(build.id, SOURCE_ID, USER_ID).schema_fingerprint
    for role in ("CANONICAL_ENTITY_SOURCE", "PREFERRED_BUSINESS_VIEW", "STATUS_SOURCE"):
        mapping_store_module.upsert_mapping(
            pkg["id"], "ENTITY_A", "dbo.wide_table", role,
            columns=list(wide_columns[:3]), mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
            evidence=["some evidence text"], schema_fingerprint=schema_fingerprint, content_hash="a" * 64,
        )

    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("wide table", build.id, SOURCE_ID, USER_ID, provider)
    candidate = next(c for c in result.candidates if c.object_fqn == "dbo.wide_table")
    assert len(candidate.business_knowledge_mappings) == 3
    assert "BUSINESS_KNOWLEDGE:" in frontier_module.render_frontier_candidate(candidate)


def test_verified_mapping_force_admits_object_no_lane_would_otherwise_surface(db_path, monkeypatch):
    """Phase 7, Step 5 audit fix -- regression for the real "IPLAN installment
    payments" defect: a table whose real name/columns share essentially no
    vocabulary with how a business user phrases the question (and whose
    embedding is not the closest semantic match either) used to never reach
    the selector at all, because business-knowledge evidence was previously
    only ATTACHED to objects the semantic/lexical/business-concept lanes had
    already admitted -- never an admission signal itself. With frontier_size
    pinned to 1, only ONE object can win each lane; a strongly-matching decoy
    (aligned embedding, shares the question's literal tokens) wins all three,
    so without this fix the verified-mapped target would never be a
    candidate. It must now be force-admitted on the strength of its VERIFIED
    mapping alone, AND sorted ahead of the decoy despite a fused score of
    0.0 -- confirming the priority fix, not just bare admission."""
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    decoy = _doc("dbo.installment_summary", tier=1, text_extra="installment payments report")
    target = _doc("dbo.zzq_plan_7f", tier=1, column_names=("id", "amt"))
    vectors = {
        decoy.document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        target.document_hash: _unit(0.0, 1.0, 0.0, 0.0),  # orthogonal -- worst possible semantic match
    }
    build = _build_with_docs(db_path, [decoy, target], vectors)

    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(
        pkg["id"], "INSTALLMENT_PAYMENT", "MEASURE", "Installment Payment", origin="AUTHORITATIVE",
    )
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])
    schema_fingerprint = retrieval_store_module.get_retrieval_build(build.id, SOURCE_ID, USER_ID).schema_fingerprint
    mapping_store_module.upsert_mapping(
        pkg["id"], "INSTALLMENT_PAYMENT", "dbo.zzq_plan_7f", "CANONICAL_ENTITY_SOURCE",
        columns=["amt"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.95,
        evidence=["owner-confirmed: this table holds installment payments"],
        schema_fingerprint=schema_fingerprint, content_hash="d" * 64,
    )

    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier(
        "installment payments", build.id, SOURCE_ID, USER_ID, provider, frontier_size=1,
    )

    fqns = [c.object_fqn for c in result.candidates]
    assert "dbo.installment_summary" in fqns  # the strong decoy, as expected
    assert "dbo.zzq_plan_7f" in fqns  # force-admitted purely on the VERIFIED mapping
    # Priority fix: the verified-mapped object outranks the fused-score
    # winner despite scoring zero in every lane -- it must not be silently
    # pushed to the tail where a tight character budget would drop it first.
    assert fqns[0] == "dbo.zzq_plan_7f"

    target_candidate = next(c for c in result.candidates if c.object_fqn == "dbo.zzq_plan_7f")
    decoy_candidate = next(c for c in result.candidates if c.object_fqn == "dbo.installment_summary")
    # The target's real fused score (from its lower, non-admitted lane ranks)
    # is far weaker than the decoy's -- it only leads final_order because the
    # VERIFIED-mapping priority tier overrides fused-score ordering entirely.
    assert target_candidate.fused_score < decoy_candidate.fused_score
    assert len(target_candidate.business_knowledge_mappings) == 1
    assert target_candidate.business_knowledge_mappings[0].concept_id == "INSTALLMENT_PAYMENT"


def test_evidence_cache_reused_across_repeated_frontier_builds(db_path, monkeypatch):
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    build = _standard_build(db_path)
    _make_package_with_verified_mapping(db_path, build)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    build_schema_frontier("students", build.id, SOURCE_ID, USER_ID, provider)
    assert business_knowledge_evidence_cache_size() == 1
    build_schema_frontier("students again", build.id, SOURCE_ID, USER_ID, provider)
    assert business_knowledge_evidence_cache_size() == 1


def test_new_mapping_invalidates_evidence_cache_without_schema_or_package_version_change(db_path, monkeypatch):
    """Phase 7, Step 6E1, Part 4 fix — a human adding/editing a governance
    mapping (e.g. approving a Tier 3/4 identity source) changes neither
    package_id nor schema_fingerprint, so the pre-fix cache identity would
    keep serving pre-edit evidence for the life of the process. The cache
    identity now also carries a cheap mapping-row signature, so a second,
    DIFFERENT mapping for the SAME object must produce a genuinely fresh
    lookup (not a stale cache hit) and the newly added evidence must be
    visible immediately, not only after a restart."""
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    build = _standard_build(db_path)
    pkg, schema_fingerprint = _make_package_with_verified_mapping(db_path, build)
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))

    first = build_schema_frontier("students", build.id, SOURCE_ID, USER_ID, provider)
    first_candidate = next(c for c in first.candidates if c.object_fqn == "dbo.students")
    assert len(first_candidate.business_knowledge_mappings) == 1

    package_service_module.upsert_concept(pkg["id"], "STUDENT_STATUS", "STATUS", "Student Status", origin="AUTHORITATIVE")
    mapping_store_module.upsert_mapping(
        pkg["id"], "STUDENT_STATUS", "dbo.students", "STATUS_SOURCE",
        columns=["full_name"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["a second, later-added mapping"], schema_fingerprint=schema_fingerprint,
        content_hash="c" * 64,
    )

    second = build_schema_frontier("students", build.id, SOURCE_ID, USER_ID, provider)
    second_candidate = next(c for c in second.candidates if c.object_fqn == "dbo.students")
    assert len(second_candidate.business_knowledge_mappings) == 2  # the new mapping is visible, not cached-stale


# ── Phase 7, Step 6E1 — tier-3/4 admission requires a confirmed
#    relationship AND its own VERIFIED business-knowledge mapping; a
#    confirmed relationship alone (tests/test_sqlservice_schema_frontier.py
#    covers that fail-closed default) is never sufficient. ────────────────


def test_tier3_object_with_verified_mapping_is_force_admitted_with_restricted_columns(db_path, monkeypatch):
    """A Tier-3 framework "Users" table reachable from an admitted Tier-1
    entity by a real declared FK is force-admitted ONLY once it carries its
    own VERIFIED business-knowledge mapping (an explicit, manually
    authored authoritative decision -- see core.sqlservice.business_
    knowledge_mapper's own docstring: this can never happen via the
    automatic AI mapper for a Tier 3/4 object). The mapping's own approved
    column list further restricts what is rendered: LastName was never
    approved and must not appear, even though it exists on the real table."""
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    entity = _doc(
        "dbo.Student", column_names=("StudentID", "StudentUserID"),
        metadata_extra={"confirmed_relationship_targets": ("framework.Users.UserID",)},
    )
    identity_table = _doc(
        "framework.Users", tier=3, column_names=("UserID", "FirstName", "LastName", "IsSuperUser"),
    )
    docs = [entity, identity_table]
    vectors = {
        entity.document_hash: _unit(1.0, 0.0, 0.0, 0.0),
        identity_table.document_hash: _unit(0.0, 1.0, 0.0, 0.0),
    }
    build = _build_with_docs(db_path, docs, vectors)

    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT_NAME", "ENTITY", "Student Name", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])
    schema_fingerprint = retrieval_store_module.get_retrieval_build(build.id, SOURCE_ID, USER_ID).schema_fingerprint
    mapping_store_module.upsert_mapping(
        pkg["id"], "STUDENT_NAME", "framework.Users", "DETAIL_ACTIVITY_SOURCE",
        columns=["FirstName"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["owner-confirmed: this framework table's FirstName holds the student's first name"],
        schema_fingerprint=schema_fingerprint, content_hash="e" * 64,
    )

    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("Show student names", build.id, SOURCE_ID, USER_ID, provider)

    fqns = {c.object_fqn for c in result.candidates}
    assert "framework.Users" in fqns
    assert any("framework.Users" in w and "VERIFIED" in w for w in result.warnings)
    users_candidate = next(c for c in result.candidates if c.object_fqn == "framework.Users")
    assert users_candidate.retrieval_tier == 3
    assert "FirstName" in users_candidate.dimension_columns
    assert "LastName" not in users_candidate.dimension_columns
    assert "IsSuperUser" not in users_candidate.dimension_columns


def test_relationship_force_admit_never_chains_two_hops_even_with_verified_mapping(db_path, monkeypatch):
    """framework.B is reachable only THROUGH framework.A. A carries its own
    VERIFIED mapping (so A alone is eligible for force-admission); B does
    not. The fix must only ever look one hop out from an object admitted
    through a REAL signal (a lane or business-knowledge), never cascade
    through a just-forced neighbor -- so B must stay excluded even though a
    structural relationship path to it exists via A."""
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    entity = _doc(
        "dbo.Student", column_names=("StudentID",),
        metadata_extra={"confirmed_relationship_targets": ("framework.A.ID",)},
    )
    tier3_a = _doc(
        "framework.A", tier=3, column_names=("ID", "BID"),
        metadata_extra={"confirmed_relationship_targets": ("framework.B.ID",)},
    )
    tier3_b = _doc("framework.B", tier=3, column_names=("ID", "Value"))
    docs = [entity, tier3_a, tier3_b]
    vectors = {d.document_hash: _unit(1.0, 0.0, 0.0, 0.0) for d in docs}
    build = _build_with_docs(db_path, docs, vectors)

    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "A_CONCEPT", "ENTITY", "A", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])
    schema_fingerprint = retrieval_store_module.get_retrieval_build(build.id, SOURCE_ID, USER_ID).schema_fingerprint
    mapping_store_module.upsert_mapping(
        pkg["id"], "A_CONCEPT", "framework.A", "DETAIL_ACTIVITY_SOURCE",
        columns=["BID"], mapping_kind="DIRECT", status="VERIFIED", confidence=0.9,
        evidence=["owner-confirmed"], schema_fingerprint=schema_fingerprint, content_hash="f" * 64,
    )

    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("Show student names", build.id, SOURCE_ID, USER_ID, provider)
    fqns = {c.object_fqn for c in result.candidates}
    assert "framework.A" in fqns
    assert "framework.B" not in fqns  # never chained through the just-forced A, even though A is legitimate


def test_tier3_object_with_only_unresolved_mapping_stays_excluded(db_path, monkeypatch):
    """An UNRESOLVED (sub-threshold-confidence, never owner-confirmed)
    mapping must never satisfy the governance gate -- only VERIFIED does."""
    import core.sqlservice.schema_frontier as frontier_module
    monkeypatch.setattr(frontier_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)

    entity = _doc(
        "dbo.Student", column_names=("StudentID",),
        metadata_extra={"confirmed_relationship_targets": ("framework.Users.UserID",)},
    )
    identity_table = _doc("framework.Users", tier=3, column_names=("UserID", "FirstName"))
    docs = [entity, identity_table]
    vectors = {d.document_hash: _unit(1.0, 0.0, 0.0, 0.0) for d in docs}
    build = _build_with_docs(db_path, docs, vectors)

    pkg = package_service_module.create_or_get_draft_package(SOURCE_ID, USER_ID, USER_ID)
    package_service_module.upsert_concept(pkg["id"], "STUDENT_NAME", "ENTITY", "Student Name", origin="AUTHORITATIVE")
    package_service_module.activate_package(SOURCE_ID, USER_ID, pkg["package_version"])
    schema_fingerprint = retrieval_store_module.get_retrieval_build(build.id, SOURCE_ID, USER_ID).schema_fingerprint
    mapping_store_module.upsert_mapping(
        pkg["id"], "STUDENT_NAME", "framework.Users", "DETAIL_ACTIVITY_SOURCE",
        columns=["FirstName"], mapping_kind="UNRESOLVED", status="UNRESOLVED", confidence=0.4,
        evidence=[], schema_fingerprint=schema_fingerprint, content_hash="g" * 64,
    )

    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("Show student names", build.id, SOURCE_ID, USER_ID, provider)
    fqns = {c.object_fqn for c in result.candidates}
    assert "framework.Users" not in fqns


# ── No domain-specific tokens in any new production module ───────────────

def test_no_domain_specific_tokens_in_business_knowledge_modules():
    import inspect
    import core.sqlservice.business_knowledge_evidence as evidence_module
    import core.sqlservice.business_knowledge_mapper as mapper_module
    import core.sqlservice.business_knowledge_mapping_providers as providers_module

    forbidden = ("edu_", "exampleco", "exampleco", "iplan", "student", "classsignup", "surveyanswers", "master_list")
    for module in (evidence_module, mapper_module, providers_module,
                   package_service_module, mapping_store_module):
        source = inspect.getsource(module).lower()
        for token in forbidden:
            assert token not in source, f"found domain-specific token {token!r} in {module.__name__}"
