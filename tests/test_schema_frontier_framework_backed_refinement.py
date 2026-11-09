"""
Production-path integration tests — Phase 7, Step 6E3B1, Part 1.

Proves the FRAMEWORK_BACKED_BUSINESS_VIEW refinement (introduced in Step
6E3B's core.sqlservice.schema_object_authority.classify_framework_backed_
eligibility) actually changes behavior along the REAL active fast-path
call chain, not only in an isolated classifier unit test:

  1. core.sqlservice.schema_frontier.build_schema_frontier -- the refined
     class/cleared gate-field must appear on the real FrontierCandidate.
  2. core.sqlservice.schema_selection_service._validate_authority_facts --
     the EXISTING, UNMODIFIED guard that previously rejected any
     UNSAFE_DEPENDENCY candidate outright -- must now ACCEPT the refined
     candidate and still REJECT the raw one.

Also proves the fail-closed contract: a missing, stale-fingerprint, or
incomplete persisted profile must never apply the refinement.

Reuses the exact SQLite fixture plumbing already established in
tests/test_sqlservice_schema_frontier.py (itself built on tests/
sqlservice_fixtures.py) rather than duplicating it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as schema_object_authority_module
import data.db as data_db_module
import data.view_profile_service as view_profile_service_module
from core.sqlservice.schema_frontier import STATUS_OK, FrontierCandidate, build_schema_frontier
from core.sqlservice.schema_object_authority import (
    AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW,
    AUTHORITY_CLASS_UNSAFE_DEPENDENCY,
    clear_schema_object_authority_cache,
)
from core.sqlservice.schema_selection_service import (
    IntentSummary,
    ObjectSelectionReason,
    SchemaSelectionValidationError,
    _validate_authority_facts,
)
from core.sqlservice.view_profiler import VIEW_PROFILE_CONTRACT_VERSION
from core.sqlservice.vector_index import clear_vector_index_cache
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.business_concept_index import clear_business_concept_index_cache
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache

from tests.sqlservice_fixtures import (
    DIMS, MODEL, PROVIDER, SOURCE_ID, USER_ID, FakeEmbeddingProvider, _insert_snapshot, _insert_source,
    _make_connection, template_db_path,  # noqa: F401
)
from tests.test_sqlservice_schema_frontier import _build_with_docs, _doc, _insert_view_evidence, _unit, _view_doc


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    import shutil

    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_object_authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(view_profile_service_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_caches():
    clear_vector_index_cache(); clear_lexical_index_cache()
    clear_business_concept_index_cache(); clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()
    yield
    clear_vector_index_cache(); clear_lexical_index_cache()
    clear_business_concept_index_cache(); clear_canonical_scoring_cache()
    clear_schema_object_authority_cache()


_VIEW_FQN = "dbo.vw_StudentOverview"
_FRAMEWORK_TABLE_FQN = "dnnuser.Users"
_DEFINITION_SQL = (
    "CREATE VIEW dbo.vw_StudentOverview AS "
    "SELECT u.Id, u.Name AS StudentName FROM dnnuser.Users u"
)


def _setup_unsafe_dependency_view(db_path):
    """A Tier-1 VIEW whose own dependency closure includes a Tier-3
    object -- the exact real-world shape of dbo.vw_StudentOverview per the
    Step 6E3A/6E3B baseline (depends on dnnuser.Users)."""
    _insert_view_evidence(db_path, _VIEW_FQN, _DEFINITION_SQL, [_FRAMEWORK_TABLE_FQN])
    docs = [
        _doc(_FRAMEWORK_TABLE_FQN, tier=3, metadata_extra={"primary_key_columns": ("Id",)}),
        _view_doc(_VIEW_FQN, column_names=("Id", "StudentName")),
    ]
    vectors = {docs[0].document_hash: _unit(1.0, 0.0, 0.0, 0.0), docs[1].document_hash: _unit(0.9, 0.1, 0.0, 0.0)}
    build = _build_with_docs(db_path, docs, vectors)
    return build


def _insert_view_profile_row(
    db_path, *, build, authority_class, completion_status="COMPLETE",
    schema_fingerprint=None, profiling_version=VIEW_PROFILE_CONTRACT_VERSION,
    secret_policy_version=None, definition_hash="x",
):
    from core.dictionary.pii_detector import SECRET_POLICY_VERSION
    conn = sqlite3.connect(db_path)
    now = "2026-10-01T00:00:00Z"
    payload_json = json.dumps({
        "secret_policy_version": secret_policy_version if secret_policy_version is not None else SECRET_POLICY_VERSION,
    })
    conn.execute(
        "INSERT INTO sql_view_profiles "
        "(source_id, build_id, object_fqn, schema_fingerprint, definition_hash, profiling_version, "
        " completion_status, failure_reason, authority_class, lineage_complete, is_framework_backed, "
        " suitable_for_detail, suitable_for_count, suitable_for_aggregation, suitable_for_join, "
        " duplication_risk, row_preservation_risk, required_distinct_key, payload_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, 1, 1, 1, 1, 0, 'NONE', 'PRESERVED', NULL, ?, ?, ?)",
        (
            SOURCE_ID, build.id, _VIEW_FQN,
            schema_fingerprint if schema_fingerprint is not None else build.schema_fingerprint,
            definition_hash, profiling_version, completion_status, authority_class, payload_json, now, now,
        ),
    )
    conn.commit()
    conn.close()


def _run_frontier(db_path, build):
    provider = FakeEmbeddingProvider(fixed_vector=_unit(1.0, 0.0, 0.0, 0.0))
    result = build_schema_frontier("student overview", build.id, SOURCE_ID, USER_ID, provider)
    assert result.status == STATUS_OK
    return {c.object_fqn: c for c in result.candidates}


# ── 1. Positive: matching, complete, current-build profile refines ────────


def test_matching_complete_profile_refines_candidate_in_real_frontier(db_path):
    build = _setup_unsafe_dependency_view(db_path)
    _insert_view_profile_row(db_path, build=build, authority_class=AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW)

    by_fqn = _run_frontier(db_path, build)
    candidate = by_fqn[_VIEW_FQN]

    assert candidate.authority_class == AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW
    # The gate-consulted field is cleared...
    assert candidate.authority_unsafe_dependency_fqns == ()
    # ...but the ORIGINAL evidence is never lost.
    assert _FRAMEWORK_TABLE_FQN.lower() in candidate.authority_unsafe_dependency_evidence_fqns


# ── 2. Fail-closed: no profile, stale fingerprint, incomplete profile ─────


def test_no_persisted_profile_stays_unsafe_dependency(db_path):
    build = _setup_unsafe_dependency_view(db_path)
    by_fqn = _run_frontier(db_path, build)
    candidate = by_fqn[_VIEW_FQN]
    assert candidate.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    assert candidate.authority_unsafe_dependency_fqns == (_FRAMEWORK_TABLE_FQN.lower(),) or \
        _FRAMEWORK_TABLE_FQN.lower() in candidate.authority_unsafe_dependency_fqns


def test_stale_schema_fingerprint_profile_is_not_trusted(db_path):
    build = _setup_unsafe_dependency_view(db_path)
    _insert_view_profile_row(
        db_path, build=build, authority_class=AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW,
        schema_fingerprint="a-stale-fingerprint-from-a-previous-build",
    )

    by_fqn = _run_frontier(db_path, build)
    candidate = by_fqn[_VIEW_FQN]

    assert candidate.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    assert candidate.authority_unsafe_dependency_fqns


def test_incomplete_profile_is_not_trusted(db_path):
    build = _setup_unsafe_dependency_view(db_path)
    _insert_view_profile_row(
        db_path, build=build, authority_class=AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW,
        completion_status="INCOMPLETE",
    )

    by_fqn = _run_frontier(db_path, build)
    candidate = by_fqn[_VIEW_FQN]

    assert candidate.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    assert candidate.authority_unsafe_dependency_fqns


def test_stale_profiling_version_is_not_trusted(db_path):
    build = _setup_unsafe_dependency_view(db_path)
    _insert_view_profile_row(
        db_path, build=build, authority_class=AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW,
        profiling_version=VIEW_PROFILE_CONTRACT_VERSION + 1,
    )

    by_fqn = _run_frontier(db_path, build)
    candidate = by_fqn[_VIEW_FQN]

    assert candidate.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY


# ── 3. Direct proof against the two real, UNMODIFIED selection gates ──────


def _minimal_candidate(**overrides) -> FrontierCandidate:
    base = dict(
        object_fqn=_VIEW_FQN, object_type="VIEW", retrieval_tier=1,
        business_name=None, business_purpose=None, domain=None, aliases=(),
        primary_key_columns=(), foreign_key_summary=(), local_foreign_key_columns=(),
        measure_columns=(), date_columns=(), status_columns=(), dimension_columns=(),
        confirmed_relationship_neighbors=(), semantic_rank=1, semantic_score=0.9,
        lexical_rank=None, lexical_score=None, business_concept_rank=None, business_concept_score=None,
        canonical_rank=None, canonical_total_score=None, fused_rank=1, fused_score=0.9,
        admitted_via_lanes=("semantic",), matched_business_concepts=(),
    )
    base.update(overrides)
    return FrontierCandidate(**base)


def _selection_reason():
    return ObjectSelectionReason(object_fqn=_VIEW_FQN, supported_requirements=("enrollment detail",), supporting_columns=())


def _intent():
    return IntentSummary(operation="LIST", entities=("Student",), measures=(), filters=(), dates=(), groupings=())


def test_selection_service_gate_accepts_refined_candidate():
    candidate = _minimal_candidate(
        authority_class=AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW,
        authority_unsafe_dependency_fqns=(), authority_unsafe_dependency_evidence_fqns=(_FRAMEWORK_TABLE_FQN.lower(),),
    )
    # Must not raise.
    _validate_authority_facts(
        (_selection_reason(),), candidates_by_lower={_VIEW_FQN.lower(): candidate}, intent_summary=_intent(),
    )


def test_selection_service_gate_still_rejects_raw_unsafe_dependency():
    candidate = _minimal_candidate(
        authority_class=AUTHORITY_CLASS_UNSAFE_DEPENDENCY,
        authority_unsafe_dependency_fqns=(_FRAMEWORK_TABLE_FQN.lower(),),
    )
    with pytest.raises(SchemaSelectionValidationError):
        _validate_authority_facts(
            (_selection_reason(),), candidates_by_lower={_VIEW_FQN.lower(): candidate}, intent_summary=_intent(),
        )

