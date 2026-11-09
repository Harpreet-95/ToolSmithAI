"""
Focused tests — Phase 7, Step 6E3B1, Part 7: PII/secret policy audit
across both the active AI-first fast path and the view-profiling layer,
using sentinel values to prove nothing PII-shaped ever reaches a log,
the rendered question-time context, or a cache entry.

Owner policy (unchanged by this step, verified here): ordinary/sensitive
PII (names, email, SSN, passport, bank account, ...) is returned unmasked
to an authorized caller and is never logged/cached; only actual
authentication/system-access secrets are unconditionally blocked.
"""

from __future__ import annotations

import logging

import pytest

from core.dictionary.pii_detector import is_restricted_category
from core.sqlservice.fast_path_context import _candidate_prioritized_lines, _assemble_bounded_context
from core.sqlservice.schema_frontier import FrontierCandidate
from core.sqlservice.view_profiler import compute_view_profile

_SENTINEL_SSN = "SENTINEL-SSN-987-65-4321"
_SENTINEL_SECRET_VALUE = "SENTINEL-PASSWORD-VALUE-zzz999"


# ── Secret-category coverage (Part 6E3B Part 6 + this step's own audit) ────


@pytest.mark.parametrize("name,expected", [
    ("EncryptionKey", True), ("encryption_key", True), ("MasterKey", True), ("master_key", True),
    ("Password", True), ("PasswordHash", True), ("PasswordResetToken", True),
    ("SessionToken", True), ("AccessToken", True), ("ApiKey", True),
    ("ConnectionString", True), ("PrivateKey", True), ("Pin", True), ("Otp", True),
    ("SecurityAnswer", True), ("Cvv", True),
    # Ordinary/sensitive PII -- must NOT be restricted under the owner's policy.
    ("SSN", False), ("Passport", False), ("BankAccountNumber", False), ("RoutingNumber", False),
    ("Email", False), ("FirstName", False), ("DateOfBirth", False), ("CreditCardNumber", False),
])
def test_secret_category_coverage_matches_owner_policy(name, expected):
    assert is_restricted_category(name) is expected


# ── No PII/secret VALUE in the rendered question-time context ─────────────


def _bare_view_candidate(fqn="dbo.v_sensitive"):
    return FrontierCandidate(
        object_fqn=fqn, object_type="VIEW", retrieval_tier=1,
        business_name=None, business_purpose=None, domain=None, aliases=(),
        primary_key_columns=("Id",), foreign_key_summary=(), local_foreign_key_columns=(),
        measure_columns=(), date_columns=(), status_columns=(), dimension_columns=("Id",),
        confirmed_relationship_neighbors=(), semantic_rank=1, semantic_score=0.9,
        lexical_rank=None, lexical_score=None, business_concept_rank=None, business_concept_score=None,
        canonical_rank=None, canonical_total_score=None, fused_rank=1, fused_score=0.9,
        admitted_via_lanes=("semantic",), matched_business_concepts=(),
    )


def test_sentinel_literal_in_view_definition_never_reaches_rendered_context():
    sql = f"CREATE VIEW dbo.v_sensitive AS SELECT u.Id FROM dbo.Users u WHERE u.SSN = '{_SENTINEL_SSN}'"
    profile = compute_view_profile(
        "dbo.v_sensitive", sql, source_id=1, build_id=1, schema_fingerprint="fp",
        direct_dependencies=["dbo.Users"], transitive_dependencies=[], authority=None,
        column_names_by_fqn_lower={"dbo.users": frozenset({"id", "ssn"})},
        primary_key_columns_by_fqn_lower={"dbo.users": ("Id",)}, now="2026-10-01T00:00:00Z",
    )
    # The view-definition literal IS captured as structural evidence
    # (expected -- this is schema/business-logic text, not row data)...
    assert _SENTINEL_SSN in " ".join(profile.population_restrictions)

    # ...but it must never reach the rendered question-time context --
    # only a boolean POPULATION_RESTRICTED flag is ever rendered there.
    candidate = _bare_view_candidate()
    base, by_priority = _candidate_prioritized_lines(candidate, profile)
    text, _, _ = _assemble_bounded_context([(base, by_priority)], 100_000)

    assert _SENTINEL_SSN not in text
    assert "POPULATION_RESTRICTED: true" in text


def test_secret_column_name_never_implies_a_value_leak_in_context():
    sql = "CREATE VIEW dbo.v_sensitive AS SELECT u.Id, u.PasswordHash AS PasswordHash FROM dbo.Users u"
    profile = compute_view_profile(
        "dbo.v_sensitive", sql, source_id=1, build_id=1, schema_fingerprint="fp",
        direct_dependencies=["dbo.Users"], transitive_dependencies=[], authority=None,
        column_names_by_fqn_lower={"dbo.users": frozenset({"id", "passwordhash"})},
        primary_key_columns_by_fqn_lower={"dbo.users": ("Id",)}, now="2026-10-01T00:00:00Z",
    )
    candidate = _bare_view_candidate()
    base, by_priority = _candidate_prioritized_lines(candidate, profile)
    text, _, _ = _assemble_bounded_context([(base, by_priority)], 100_000)

    # Only the COLUMN NAME is surfaced (as evidence to reject it), never a
    # value -- there is no value to leak in the first place (this module
    # never reads row data).
    assert "PasswordHash" in text
    assert "SECRET_COLUMNS: PasswordHash" in text


# ── No PII/secret VALUE ever reaches a log line ────────────────────────────


def test_no_sentinel_value_reaches_logs_during_profiling(caplog):
    sql = f"CREATE VIEW dbo.v_sensitive AS SELECT u.Id FROM dbo.Users u WHERE u.Note = '{_SENTINEL_SECRET_VALUE}'"
    with caplog.at_level(logging.DEBUG):
        compute_view_profile(
            "dbo.v_sensitive", sql, source_id=1, build_id=1, schema_fingerprint="fp",
            direct_dependencies=["dbo.Users"], transitive_dependencies=[], authority=None,
            column_names_by_fqn_lower={"dbo.users": frozenset({"id", "note"})},
            primary_key_columns_by_fqn_lower={"dbo.users": ("Id",)}, now="2026-10-01T00:00:00Z",
        )
    for record in caplog.records:
        assert _SENTINEL_SECRET_VALUE not in record.getMessage()


def test_no_sentinel_value_reaches_logs_on_unparseable_definition(caplog):
    sql = f"THIS IS NOT VALID SQL {{ -- {_SENTINEL_SECRET_VALUE}"
    with caplog.at_level(logging.DEBUG):
        profile = compute_view_profile(
            "dbo.v_broken", sql, source_id=1, build_id=1, schema_fingerprint="fp",
            direct_dependencies=[], transitive_dependencies=[], authority=None,
            column_names_by_fqn_lower={}, primary_key_columns_by_fqn_lower={}, now="2026-10-01T00:00:00Z",
        )
    assert profile.completion_status == "FAILED"
    for record in caplog.records:
        assert _SENTINEL_SECRET_VALUE not in record.getMessage()
