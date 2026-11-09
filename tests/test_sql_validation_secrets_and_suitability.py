"""
Focused tests — Phase 7, Step 6E3B, Parts 6/8: secret/credential column
rejection and deterministic view-suitability validation
(core/sqlservice/sql_validation.py's new additive checks).

No database of any kind — mirrors tests/test_sqlservice_sql_validation.py's
own "construct SchemaContextResult/SqlGenerationResult directly" style.
data.view_profile_service.get_view_profiles_by_fqns is monkeypatched
directly (it is imported locally, inside the function, on every call —
see sql_validation._validate_view_suitability's own docstring) rather
than exercised against a real database.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_SEMANTIC,
    STATUS_OK as SCHEMA_STATUS_OK,
    ContextDocument,
    SchemaContextResult,
)
from core.sqlservice.sql_generation_service import (
    STATUS_OK as GEN_STATUS_OK,
    SqlGenerationCandidate,
    SqlGenerationResult,
)
from core.sqlservice.sql_validation import STATUS_OK, STATUS_REJECTED, validate_sql_candidate
import data.view_profile_service as view_profile_service_module
from core.sqlservice.view_profiler import ViewProfile, ViewSuitability
from core.sqlservice import schema_object_authority as authority_module

SOURCE_ID = 90001


def _doc(fqn, *, tier=1, object_type="TABLE", columns=("id",), metadata_extra=None):
    text = f"OBJECT: {fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    metadata = {"column_names": tuple(columns)}
    metadata.update(metadata_extra or {})
    return ContextDocument(
        object_fqn=fqn, object_type=object_type, retrieval_tier=tier,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), document_text=text,
        metadata=MappingProxyType(metadata), score=0.9, selection_reason=SELECTION_REASON_SEMANTIC,
    )


def _context(documents, *, build_id=1, schema_fingerprint="fp-123"):
    return SchemaContextResult(
        status=SCHEMA_STATUS_OK, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=1536,
        documents=tuple(documents), context_text="context", total_characters=7,
        truncated=False, warnings=(), error=None,
    )


def _generation_result(sql, referenced, *, build_id=1, schema_fingerprint="fp-123"):
    candidate = SqlGenerationCandidate(
        sql=sql, interpretation="An interpretation.", referenced_object_fqns=tuple(referenced),
        assumptions=(), needs_clarification=False, clarification_question=None,
    )
    return SqlGenerationResult(
        status=GEN_STATUS_OK, candidate=candidate, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id="fake", model="fake-model", warnings=(), error=None,
    )


# ── Part 6/8: secret/credential column rejection ────────────────────────────


def test_secret_column_rejected_regardless_of_tier():
    ctx = _context([_doc("dbo.users", tier=1, columns=("id", "passwordhash"))])
    gen = _generation_result("SELECT id, passwordhash FROM dbo.users", ["dbo.users"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED
    assert "restricted" in (result.error or "").lower()


def test_ordinary_pii_column_not_rejected():
    ctx = _context([_doc("dbo.users", tier=1, columns=("id", "email", "ssn"))])
    # SSN is sensitive PII but not an authentication secret -- stays selectable.
    gen = _generation_result("SELECT id, email, ssn FROM dbo.users", ["dbo.users"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_OK


# ── Part 8: deterministic view suitability ──────────────────────────────────


def _view_profile(
    *, suitable_for_detail=True, suitable_for_count=True, suitable_for_aggregation=True,
    suitable_for_join=True, required_distinct_key=None, duplication_risk="NONE",
    secret_columns=(), population_restrictions=(), secret_policy_version=None,
):
    from core.dictionary.pii_detector import SECRET_POLICY_VERSION
    return ViewProfile(
        object_fqn="dbo.v_enrollment", source_id=SOURCE_ID, build_id=1, schema_fingerprint="fp-123",
        definition_hash="hash", profiling_version=1, completion_status="COMPLETE", failure_reason=None,
        required_distinct_key=required_distinct_key, duplication_risk=duplication_risk,
        secret_columns=secret_columns, population_restrictions=population_restrictions,
        secret_policy_version=(secret_policy_version if secret_policy_version is not None else SECRET_POLICY_VERSION),
        suitability=ViewSuitability(
            suitable_for_detail=suitable_for_detail, suitable_for_count=suitable_for_count,
            suitable_for_aggregation=suitable_for_aggregation, suitable_for_join=suitable_for_join,
            unsuitable_reasons=(), required_distinct_key=required_distinct_key,
            population_restriction=(population_restrictions[0] if population_restrictions else None), confidence="HIGH",
        ),
        created_at="2026-09-30T00:00:00Z", updated_at="2026-09-30T00:00:00Z",
    )


def _patch_profiles(monkeypatch, profile):
    monkeypatch.setattr(
        view_profile_service_module, "get_view_profiles_by_fqns",
        lambda source_id, fqns: {"dbo.v_enrollment": profile} if profile is not None else {},
    )


def test_detail_query_accepted_for_detail_suitable_view(monkeypatch):
    _patch_profiles(monkeypatch, _view_profile(suitable_for_detail=True))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname", "classname"))])
    gen = _generation_result("SELECT studentname, classname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_OK


def test_detail_query_rejected_for_aggregate_grain_view(monkeypatch):
    _patch_profiles(monkeypatch, _view_profile(suitable_for_detail=False))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname", "classname"))])
    gen = _generation_result("SELECT studentname, classname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED


def test_naive_count_star_rejected_when_fanout_risk_and_key_required(monkeypatch):
    _patch_profiles(
        monkeypatch,
        _view_profile(required_distinct_key="StudentID", duplication_risk="POSSIBLE_FANOUT"),
    )
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result("SELECT COUNT(*) FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED


def test_count_distinct_required_key_accepted(monkeypatch):
    _patch_profiles(
        monkeypatch,
        _view_profile(required_distinct_key="StudentID", duplication_risk="POSSIBLE_FANOUT"),
    )
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result(
        "SELECT COUNT(DISTINCT StudentID) FROM dbo.v_enrollment", ["dbo.v_enrollment"],
    )

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_OK


def test_count_distinct_accepted_even_when_suitable_for_aggregation_is_false_from_fanout(monkeypatch):
    # Step 6E3B2, Part 5 real-data regression (found via the pre-backfill
    # simulation against actual source-90004 cached metadata, not a
    # synthetic fixture): a genuinely LIKELY_FANOUT view's own compute_
    # view_profile output ALSO sets suitable_for_aggregation=False with
    # unsuitable_reasons=["AGGREGATION:row_preservation_or_fanout_risk"].
    # Without the fix, COUNT(DISTINCT required_key) -- the view's own
    # recommended, correct way to count -- was rejected right alongside
    # the naive COUNT(*) it exists to replace, making the view
    # uncountable under ANY phrasing. The two prior tests in this file
    # used a hand-built fixture that left suitable_for_aggregation=True
    # by default and never caught this.
    profile = _view_profile(
        required_distinct_key="StudentID", duplication_risk="LIKELY_FANOUT", suitable_for_aggregation=False,
    )
    profile = dataclasses.replace(
        profile,
        suitability=dataclasses.replace(
            profile.suitability, unsuitable_reasons=("AGGREGATION:row_preservation_or_fanout_risk",),
        ),
    )
    _patch_profiles(monkeypatch, profile)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])

    naive = _generation_result("SELECT COUNT(*) FROM dbo.v_enrollment", ["dbo.v_enrollment"])
    assert validate_sql_candidate(naive, ctx, source_id=SOURCE_ID).status == STATUS_REJECTED

    correct = _generation_result("SELECT COUNT(DISTINCT StudentID) FROM dbo.v_enrollment", ["dbo.v_enrollment"])
    assert validate_sql_candidate(correct, ctx, source_id=SOURCE_ID).status == STATUS_OK


def test_aggregation_still_rejected_when_unsuitable_for_a_different_reason_than_fanout(monkeypatch):
    # The carve-out must NOT swallow a genuinely different aggregation
    # incompatibility (e.g. incompatible grain) just because a distinct
    # key happens to also be set.
    profile = _view_profile(
        required_distinct_key="StudentID", duplication_risk="LIKELY_FANOUT", suitable_for_aggregation=False,
    )
    profile = dataclasses.replace(
        profile,
        suitability=dataclasses.replace(
            profile.suitability,
            unsuitable_reasons=("AGGREGATION:row_preservation_or_fanout_risk", "AGGREGATION:incompatible_grain"),
        ),
    )
    _patch_profiles(monkeypatch, profile)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result("SELECT COUNT(DISTINCT StudentID) FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    assert validate_sql_candidate(gen, ctx, source_id=SOURCE_ID).status == STATUS_REJECTED


def test_aggregation_rejected_when_view_unsuitable_for_aggregation(monkeypatch):
    _patch_profiles(monkeypatch, _view_profile(suitable_for_aggregation=False))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result(
        "SELECT COUNT(*) AS c FROM dbo.v_enrollment GROUP BY StudentID", ["dbo.v_enrollment"],
    )

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED


def test_unprofiled_view_is_rejected_for_a_plain_detail_query_too(monkeypatch):
    # Phase 7, Step 6E3B2, Part 1 -- supersedes the old 6E3B1 Part 2
    # "detail queries may still proceed against an unprofiled view" rule.
    # A view may be referenced by generated SQL ONLY when its profile is
    # COMPLETE; a plain row-detail query is no longer exempt just because
    # it has no aggregation.
    _patch_profiles(monkeypatch, None)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED
    assert "state=MISSING" in (result.error or "")


def test_view_with_secret_columns_rejected_via_suitability_evidence_too(monkeypatch):
    _patch_profiles(monkeypatch, _view_profile(secret_columns=("PasswordHash",)))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED


# ── Part 2: fail-closed on missing/stale/incomplete profiles ───────────────


def test_naive_count_star_rejected_for_completely_unprofiled_view(monkeypatch):
    _patch_profiles(monkeypatch, None)  # MISSING -- no row at all
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result("SELECT COUNT(*) FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED
    assert "no currently-trusted profile" in (result.error or "")


def test_group_by_aggregate_rejected_for_completely_unprofiled_view(monkeypatch):
    _patch_profiles(monkeypatch, None)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result(
        "SELECT StudentID, COUNT(*) AS c FROM dbo.v_enrollment GROUP BY StudentID", ["dbo.v_enrollment"],
    )

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED


def test_plain_detail_query_now_also_rejected_against_unprofiled_view(monkeypatch):
    # Step 6E3B2, Part 1 tightening -- see test_unprofiled_view_is_rejected_
    # for_a_plain_detail_query_too's own docstring for the full rationale.
    # The view stays "discoverable" (still rendered in retrieval/diagnostic
    # evidence by schema_frontier/fast_path_context) -- only EXECUTION is
    # blocked here, unconditionally, regardless of query shape.
    _patch_profiles(monkeypatch, None)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED


def test_naive_count_rejected_for_stale_schema_fingerprint_profile(monkeypatch):
    stale = _view_profile(required_distinct_key="StudentID", duplication_risk="POSSIBLE_FANOUT")
    stale = dataclasses.replace(stale, schema_fingerprint="a-different-stale-fingerprint")
    _patch_profiles(monkeypatch, stale)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))], schema_fingerprint="fp-123")
    gen = _generation_result("SELECT COUNT(*) FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED
    assert "state=STALE" in (result.error or "")


def test_naive_count_rejected_for_lineage_incomplete_profile(monkeypatch):
    incomplete = dataclasses.replace(
        _view_profile(required_distinct_key="StudentID", duplication_risk="POSSIBLE_FANOUT"),
        completion_status="INCOMPLETE",
    )
    _patch_profiles(monkeypatch, incomplete)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentid",))])
    gen = _generation_result("SELECT COUNT(*) FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED
    assert "state=LINEAGE_INCOMPLETE" in (result.error or "")


# ── Part 6: deterministic restricted-population enforcement ────────────────
#
# Generic by construction -- keyed only on the view's own persisted
# population_restrictions evidence and the requester's own declared
# `filters`, never a view name or a hardcoded question list.


def test_unrestricted_all_question_rejected_against_population_restricted_view(monkeypatch):
    _patch_profiles(monkeypatch, _view_profile(population_restrictions=("WHERE Status = 'ACTIVE'",)))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    # No declared filters -- an "all students" style unrestricted request.
    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID, requested_population_filters=())

    assert result.status == STATUS_REJECTED
    assert "population restriction" in (result.error or "")


def test_explicitly_scoped_question_accepted_against_population_restricted_view(monkeypatch):
    _patch_profiles(monkeypatch, _view_profile(population_restrictions=("WHERE Status = 'ACTIVE'",)))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    # The requester declared a scope/filter -- matches this module's own
    # pre-existing standard for "may use a RESTRICTED_VIEW" (schema_
    # selection_service._validate_authority_facts' unrestricted_request).
    result = validate_sql_candidate(
        gen, ctx, source_id=SOURCE_ID, requested_population_filters=("status = active",),
    )

    assert result.status == STATUS_OK


def test_unrestricted_question_against_a_genuinely_unrestricted_view_is_fine(monkeypatch):
    # Ambiguous/no-evidence case: the profile is COMPLETE but carries NO
    # population restriction at all -- no claim to violate, so an
    # unrestricted question is never rejected just because the view
    # happens to be a VIEW.
    _patch_profiles(monkeypatch, _view_profile(population_restrictions=()))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID, requested_population_filters=())

    assert result.status == STATUS_OK


def test_model_confidence_cannot_override_restricted_population_check(monkeypatch):
    # confidence is never a parameter of validate_sql_candidate at all --
    # this test documents that fact: the function signature has no
    # confidence-shaped input, so there is nothing a high-confidence
    # candidate could pass to bypass the check above.
    import inspect

    sig = inspect.signature(validate_sql_candidate)
    assert "confidence" not in sig.parameters


# ── Step 6E3B2, Part 1: every profile state x every operation type ─────────


def _profile_for_state(state):
    base = _view_profile()
    if state == "MISSING":
        return None
    if state == "STALE":
        return dataclasses.replace(base, schema_fingerprint="a-stale-fingerprint")
    if state == "TYPED_FAILURE":
        return dataclasses.replace(base, completion_status="FAILED")
    if state == "LINEAGE_INCOMPLETE":
        return dataclasses.replace(base, completion_status="INCOMPLETE")
    if state == "UNSAFE":
        return dataclasses.replace(
            base, completion_status="INCOMPLETE",
            limited_complete_reasons=("unresolved_dependency_secret_exposure_risk",),
        )
    if state == "LIMITED_COMPLETE":
        return dataclasses.replace(
            base, completion_status="INCOMPLETE", limited_complete_eligible=True,
        )
    if state == "COMPLETE":
        return base
    raise ValueError(state)


_OPERATIONS = {
    "detail": "SELECT studentname FROM dbo.v_enrollment",
    "count": "SELECT COUNT(*) FROM dbo.v_enrollment",
    "aggregate": "SELECT StudentID, COUNT(*) AS c FROM dbo.v_enrollment GROUP BY StudentID",
}


@pytest.mark.parametrize("state", ["MISSING", "STALE", "TYPED_FAILURE", "LINEAGE_INCOMPLETE", "UNSAFE"])
@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_every_non_complete_non_limited_state_blocks_every_operation_type(monkeypatch, state, operation):
    _patch_profiles(monkeypatch, _profile_for_state(state))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname", "studentid"))])
    gen = _generation_result(_OPERATIONS[operation], ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_REJECTED, f"state={state} operation={operation} should be rejected"
    assert f"state={state}" in (result.error or "")


@pytest.mark.parametrize("state", ["COMPLETE", "LIMITED_COMPLETE"])
@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_complete_and_limited_complete_states_permit_every_compatible_operation_type(monkeypatch, state, operation):
    # Phase 7, Step 6E3B3, Part 5 -- LIMITED_COMPLETE gets the SAME
    # enforcement path and the SAME suitability-derived permissions as
    # COMPLETE here (the fixture's own suitable_for_* flags are identical
    # either way); only the underlying EVIDENCE basis differs, not the
    # restrictions actually applied.
    _patch_profiles(monkeypatch, _profile_for_state(state))
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname", "studentid"))])
    gen = _generation_result(_OPERATIONS[operation], ["dbo.v_enrollment"])

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)

    assert result.status == STATUS_OK, f"state={state} operation={operation} should be accepted"


def test_limited_complete_still_blocked_by_secret_columns(monkeypatch):
    profile = dataclasses.replace(
        _profile_for_state("LIMITED_COMPLETE"), secret_columns=("PasswordHash",),
    )
    _patch_profiles(monkeypatch, profile)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])
    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)
    assert result.status == STATUS_REJECTED
    assert "restricted" in (result.error or "").lower()


def test_limited_complete_still_blocked_for_incompatible_suitability(monkeypatch):
    limited = _profile_for_state("LIMITED_COMPLETE")
    profile = dataclasses.replace(
        limited, suitability=dataclasses.replace(limited.suitability, suitable_for_detail=False),
    )
    _patch_profiles(monkeypatch, profile)
    ctx = _context([_doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",))])
    gen = _generation_result("SELECT studentname FROM dbo.v_enrollment", ["dbo.v_enrollment"])
    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID)
    assert result.status == STATUS_REJECTED


def test_blocked_view_does_not_silently_permit_direct_framework_table_fallback(monkeypatch):
    # Part 1: "Do not silently fall back to direct framework/authentication
    # tables." A blocked view must not create a path where the caller
    # instead selects the view's own underlying Tier-3/4 framework table
    # directly -- that remains independently blocked by the pre-existing
    # Tier-3/4 governance re-check, unconditionally, regardless of this
    # view-suitability check's own outcome.
    import data.business_knowledge_package_service as bkp_service_module
    monkeypatch.setattr(bkp_service_module, "get_active_package", lambda source_id, user_id: None)

    _patch_profiles(monkeypatch, None)  # the view itself is MISSING/blocked
    ctx = _context([
        _doc("dbo.v_enrollment", object_type="VIEW", columns=("studentname",)),
        _doc("dnnuser.users", object_type="TABLE", tier=3, columns=("id", "username")),
    ])
    gen = _generation_result("SELECT username FROM dnnuser.users", ["dnnuser.users"], )

    result = validate_sql_candidate(gen, ctx, source_id=SOURCE_ID, user_id="alice")

    # Rejected by the pre-existing Tier-3/4 governance re-check (no
    # VERIFIED business-knowledge mapping exists for dnnuser.users here) --
    # proving there is no silent fallback path to the raw framework table.
    assert result.status == STATUS_REJECTED
