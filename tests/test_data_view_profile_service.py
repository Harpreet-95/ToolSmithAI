"""
Focused tests — Phase 7, Step 6E3B, Parts 2/3/9/10: persisted view-profile
storage + connect-time batch orchestration (data/view_profile_service.py)
and its cache-identity hook (core/sqlservice/ai_first_fast_path_service.py).

Reuses the same template-schema-fixture + per-module get_connection
monkeypatch idiom as tests/test_sqlservice_retrieval_store.py and
tests/test_sqlservice_schema_object_authority.py.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3

import pytest

import data.models as models_module
import data.view_profile_service as view_profile_service_module
import core.sqlservice.schema_object_authority as authority_module
import core.sqlservice.retrieval_store as retrieval_store_module
import data.db as data_db_module
from data.view_profile_service import (
    run_deterministic_view_profiling,
    get_view_profile,
    get_view_profiles_by_fqns,
    get_view_profiling_status,
)
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.view_profiler import VIEW_PROFILE_CONTRACT_VERSION

SOURCE_ID = 90001
USER_ID = "alice"
NOW = "2026-09-30T00:00:00Z"


def _make_connection(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("vps_template") / "template.db")
    original = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original
    return path


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(view_profile_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_authority_cache():
    # core.sqlservice.schema_object_authority caches SchemaObjectAuthority
    # Result keyed by (build_id, source_id, schema_fingerprint, authority_
    # state, contract_version) in a process-global dict -- every test here
    # reuses the SAME SOURCE_ID/build_id, so a stale cache entry from a
    # PRIOR test's tmp database could otherwise leak into a later one if
    # their cheap authority_state signatures happened to coincide. Clearing
    # before AND after mirrors tests/test_sqlservice_schema_object_
    # authority.py's own established idiom.
    clear_schema_object_authority_cache()
    yield
    clear_schema_object_authority_cache()


def _seed_source_and_build(db_path, *, view_definition_sql):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (SOURCE_ID, USER_ID, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (1, ?, 1, 'mssql', '{}', ?, ?)",
        (SOURCE_ID, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO sql_retrieval_builds "
        "(id, source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at, completed_at) "
        "VALUES (1, ?, 1, 1, 1, 'fp-test', 'ts-hash', 'ds-hash', 'READY', 2, ?, ?)",
        (SOURCE_ID, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO sql_retrieval_documents "
        "(build_id, source_id, object_fqn, object_type, retrieval_tier, document_hash, document_text, metadata_json, created_at) "
        "VALUES (1, ?, 'dbo.Users', 'TABLE', 1, ?, 'text', ?, ?)",
        (
            SOURCE_ID, hashlib.sha256(b"users").hexdigest(),
            json.dumps({"column_names": ["Id", "Name"], "primary_key_columns": ["Id"]}), NOW,
        ),
    )
    conn.execute(
        "INSERT INTO sql_retrieval_documents "
        "(build_id, source_id, object_fqn, object_type, retrieval_tier, document_hash, document_text, metadata_json, created_at) "
        "VALUES (1, ?, 'dbo.v_test', 'VIEW', 1, ?, 'text', '{}', ?)",
        (SOURCE_ID, hashlib.sha256(b"vtest").hexdigest(), NOW),
    )
    _upsert_view_evidence(conn, view_definition_sql)
    conn.commit()
    conn.close()


def _upsert_view_evidence(conn, definition_sql):
    checksum = hashlib.sha256(definition_sql.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO view_semantic_evidence "
        "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, "
        " referenced_tables_json, restriction_json, naming_penalty_score, is_curated, discovered_at) "
        "VALUES (?, 1, 'dbo.v_test', ?, ?, ?, NULL, 0.0, 1, ?) "
        "ON CONFLICT(source_id, table_fqn) DO UPDATE SET "
        "definition_sql=excluded.definition_sql, definition_checksum=excluded.definition_checksum, "
        "discovered_at=excluded.discovered_at",
        (SOURCE_ID, definition_sql, checksum, json.dumps(["dbo.Users"]), NOW),
    )


def _add_second_view(db_path, table_fqn, definition_sql):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sql_retrieval_documents "
        "(build_id, source_id, object_fqn, object_type, retrieval_tier, document_hash, document_text, metadata_json, created_at) "
        "VALUES (1, ?, ?, 'VIEW', 1, ?, 'text', '{}', ?)",
        (SOURCE_ID, table_fqn, hashlib.sha256(table_fqn.encode()).hexdigest(), NOW),
    )
    checksum = hashlib.sha256(definition_sql.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO view_semantic_evidence "
        "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, "
        " referenced_tables_json, restriction_json, naming_penalty_score, is_curated, discovered_at) "
        "VALUES (?, 1, ?, ?, ?, ?, NULL, 0.0, 1, ?)",
        (SOURCE_ID, table_fqn, definition_sql, checksum, json.dumps(["dbo.Users"]), NOW),
    )
    conn.commit()
    conn.close()


_VIEW_SQL = "CREATE VIEW dbo.v_test AS SELECT u.Id, u.Name AS FullName FROM dbo.Users u"


def test_run_deterministic_view_profiling_completes_and_persists(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["status"] == "COMPLETE"
    assert result["total"] == 1
    assert result["completed"] == 1
    assert result["failed"] == 0
    assert result["ai_enrichment_ran"] is False

    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile is not None
    assert profile.completion_status == "COMPLETE"
    assert profile.lineage_complete is True
    assert profile.profiling_version == VIEW_PROFILE_CONTRACT_VERSION
    names = {c.output_name for c in profile.output_columns}
    assert names == {"Id", "FullName"}

    status = get_view_profiling_status(SOURCE_ID)
    assert status == {"total": 1, "complete": 1, "incomplete": 0, "failed": 0}


def test_second_run_reuses_unchanged_profile(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["reused"] == 1
    assert result["completed"] == 0


def test_definition_change_forces_recomputation(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    conn = sqlite3.connect(db_path)
    _upsert_view_evidence(conn, "CREATE VIEW dbo.v_test AS SELECT u.Id, u.Name FROM dbo.Users u")
    conn.commit()
    conn.close()

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["reused"] == 0
    assert result["completed"] == 1


def test_get_view_profiles_by_fqns_bulk_read(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    profiles = get_view_profiles_by_fqns(SOURCE_ID, ["dbo.v_test", "dbo.does_not_exist"])

    assert set(profiles.keys()) == {"dbo.v_test"}


def test_unsupported_view_definition_persists_typed_failure(db_path):
    _seed_source_and_build(db_path, view_definition_sql="THIS IS NOT VALID SQL {")

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["failed"] == 1
    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.completion_status == "FAILED"
    assert profile.failure_reason == "PARSE_ERROR"


def test_no_ready_build_is_skipped_not_fatal(db_path):
    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)
    assert result["status"] == "SKIPPED"
    assert result["total"] == 0


# ── Part 2: bootstrap-completeness and partial-run invariants ──────────────


def test_complete_run_leaves_every_view_with_a_profile_or_typed_failure(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _add_second_view(db_path, "dbo.v_broken", "THIS IS NOT VALID SQL {")

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["status"] == "COMPLETE"
    assert result["total"] == 2
    good = get_view_profile(SOURCE_ID, "dbo.v_test")
    bad = get_view_profile(SOURCE_ID, "dbo.v_broken")
    assert good is not None and good.completion_status == "COMPLETE"
    assert bad is not None and bad.completion_status == "FAILED" and bad.failure_reason is not None
    status = get_view_profiling_status(SOURCE_ID)
    assert status["total"] == 2
    assert status["complete"] + status["failed"] + status["incomplete"] == 2


def test_partial_run_leaves_remaining_views_genuinely_missing_not_silently_skipped(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _add_second_view(db_path, "dbo.v_second", "CREATE VIEW dbo.v_second AS SELECT u.Id FROM dbo.Users u")

    first = run_deterministic_view_profiling(SOURCE_ID, USER_ID, max_views_per_run=1)

    assert first["status"] == "PARTIAL"
    assert first["completed"] == 1
    profiled_fqns = {
        fqn for fqn in ("dbo.v_test", "dbo.v_second")
        if get_view_profile(SOURCE_ID, fqn) is not None
    }
    assert len(profiled_fqns) == 1  # exactly one processed, one still genuinely MISSING (no row at all)

    second = run_deterministic_view_profiling(SOURCE_ID, USER_ID)  # resumes with default, generous cap

    assert second["status"] == "COMPLETE"
    assert get_view_profile(SOURCE_ID, "dbo.v_test") is not None
    assert get_view_profile(SOURCE_ID, "dbo.v_second") is not None


def test_ai_first_fast_path_service_view_profile_cache_identity_changes_on_reprofile(db_path):
    from core.sqlservice.ai_first_fast_path_service import _view_profile_cache_identity

    before = _view_profile_cache_identity(SOURCE_ID)
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)
    after = _view_profile_cache_identity(SOURCE_ID)

    assert before != after
    assert after[0] == 1


# ── Part 4: semantic-enrichment consumption status ──────────────────────────


def _insert_enrichment(db_path, *, status="ACTIVE", updated_at=NOW, business_name="Enrolled Students",
                        description="Per-enrollment detail view", source_role="ENROLLMENT_DETAIL",
                        payload_json=None):
    conn = sqlite3.connect(db_path)
    if payload_json is None:
        payload_json = json.dumps({
            "business_domain": "Academics", "business_name": business_name, "description": description,
            "source_role": source_role, "entity_concepts": [], "status_concepts": [], "aliases": [],
            "supported_operations": [], "confidence": 0.9, "provenance": [],
        })
    conn.execute(
        "INSERT INTO sql_view_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, payload_json, "
        " status, created_at, updated_at) "
        "VALUES (?, 'dbo.v_test', ?, 'openai', 'gpt-test', 1, ?, ?, ?, ?)",
        (SOURCE_ID, "x" * 64, payload_json, status, updated_at, updated_at),
    )
    conn.commit()
    conn.close()


def test_enrichment_missing_when_no_row_exists(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.semantic_enrichment_status == "MISSING"
    assert profile.semantic_enrichment_business_name is None


def test_enrichment_unverified_and_folded_into_profile(db_path):
    # Step 6E3B2, Part 2: never AVAILABLE unless semantic_input_hash
    # identity is exactly proven (this codebase cannot do that yet --
    # see data.view_profile_service._load_semantic_enrichment's own
    # docstring). A fresh, correct-contract-version row is UNVERIFIED --
    # not provably stale, but not exactly verified either -- and its
    # business labels are still folded in (lower-confidence, not unused).
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2026-09-30T23:59:59Z")  # after view discovery (NOW)

    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.semantic_enrichment_status == "UNVERIFIED"
    assert profile.semantic_enrichment_business_name == "Enrolled Students"
    assert profile.semantic_enrichment_source_role == "ENROLLMENT_DETAIL"


def test_enrichment_unchanged_reuse_stays_unverified_across_reprofile(db_path):
    # "Unchanged reuse" -- the view definition does NOT change between two
    # profiling runs; the enrichment row's classification must stay the
    # same (UNVERIFIED) both times, not flap.
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2026-09-30T23:59:59Z")
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)
    first = get_view_profile(SOURCE_ID, "dbo.v_test").semantic_enrichment_status

    run_deterministic_view_profiling(SOURCE_ID, USER_ID)  # reused profile, re-checked enrichment
    second = get_view_profile(SOURCE_ID, "dbo.v_test").semantic_enrichment_status

    assert first == second == "UNVERIFIED"


def test_enrichment_schema_change_forces_stale_via_redefinition(db_path):
    # "Schema change" -- modeled here as the view's definition changing
    # (re-discovered, bumping discovered_at) between when the enrichment
    # was generated and now; sql_view_semantic_enrichments carries no
    # schema_fingerprint/build_id column of its own, so the view's own
    # re-discovery timestamp is the available schema-change signal. Calls
    # _load_semantic_enrichment directly (rather than through the full
    # run_deterministic_view_profiling reuse path, which would short-
    # circuit and skip recomputation entirely since the definition
    # checksum itself is unchanged here -- a separate, correct behavior
    # this test is not exercising).
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2026-09-30T23:59:59Z")

    conn = _make_connection(db_path)
    before = view_profile_service_module._load_semantic_enrichment(conn, SOURCE_ID, "dbo.v_test", "2026-09-30T00:00:00Z")
    after = view_profile_service_module._load_semantic_enrichment(conn, SOURCE_ID, "dbo.v_test", "2027-01-01T00:00:00Z")
    conn.close()

    assert before["status"] == "UNVERIFIED"
    assert after["status"] == "STALE"


def test_enrichment_stale_when_contract_version_superseded(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2026-09-30T23:59:59Z")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sql_view_semantic_enrichments SET contract_version = 999 WHERE source_id = ?", (SOURCE_ID,))
    conn.commit()
    conn.close()

    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.semantic_enrichment_status == "STALE"


def test_enrichment_stale_when_older_than_view_discovery(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2020-01-01T00:00:00Z")  # long before view discovery (NOW)

    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.semantic_enrichment_status == "STALE"


def test_enrichment_failed_on_corrupt_payload(db_path):
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2026-09-30T23:59:59Z", payload_json="not valid json {")

    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.semantic_enrichment_status == "FAILED"


def test_enrichment_generation_flag_enabled_still_never_calls_a_provider(db_path, monkeypatch):
    # Part 8 / Part 4 cross-check: toggling the flag ON must change only
    # the reported status, never trigger an actual generation call (none
    # exists in this code path at all yet).
    import core.config as config_module
    monkeypatch.setattr(config_module, "VIEW_SEMANTIC_ENRICHMENT_GENERATION_ON_CONNECT_ENABLED", True)
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["ai_enrichment_generation_enabled"] is True
    assert result["ai_enrichment_ran"] is False


def test_baseline_profiling_complete_without_any_enrichment(db_path):
    # No sql_view_semantic_enrichments row at all -- profiling must still
    # fully complete, deterministically, with zero AI calls.
    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)

    result = run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    assert result["status"] == "COMPLETE"
    assert result["ai_enrichment_ran"] is False
    assert result["ai_enrichment_generation_enabled"] is False
    profile = get_view_profile(SOURCE_ID, "dbo.v_test")
    assert profile.completion_status == "COMPLETE"
    assert profile.semantic_enrichment_status == "MISSING"


def test_business_label_rendered_in_context_only_when_available(db_path):
    from core.sqlservice.fast_path_context import _candidate_prioritized_lines
    from core.sqlservice.schema_frontier import FrontierCandidate

    _seed_source_and_build(db_path, view_definition_sql=_VIEW_SQL)
    _insert_enrichment(db_path, updated_at="2026-09-30T23:59:59Z")
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)
    profile = get_view_profile(SOURCE_ID, "dbo.v_test")

    candidate = FrontierCandidate(
        object_fqn="dbo.v_test", object_type="VIEW", retrieval_tier=1,
        business_name=None, business_purpose=None, domain=None, aliases=(),
        primary_key_columns=("Id",), foreign_key_summary=(), local_foreign_key_columns=(),
        measure_columns=(), date_columns=(), status_columns=(), dimension_columns=("Id",),
        confirmed_relationship_neighbors=(), semantic_rank=1, semantic_score=0.9,
        lexical_rank=None, lexical_score=None, business_concept_rank=None, business_concept_score=None,
        canonical_rank=None, canonical_total_score=None, fused_rank=1, fused_score=0.9,
        admitted_via_lanes=("semantic",), matched_business_concepts=(),
    )
    _base, by_priority = _candidate_prioritized_lines(candidate, profile)
    descriptive_line = by_priority[9]

    assert "enrichment=UNVERIFIED" in descriptive_line
    assert "business_name=Enrolled Students" in descriptive_line
