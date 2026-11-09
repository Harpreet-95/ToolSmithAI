"""
Focused tests — Phase 7, Step 6C, Part 2: the fast-path context builder
(core/sqlservice/fast_path_context.py).

Reuses tests/sqlservice_fixtures.py's own SQLite fixture
plumbing exactly like tests/test_sqlservice_ai_first_question_answering.py
does, since core.sqlservice.fast_path_context.build_fast_path_context()
calls core.sqlservice.schema_frontier.build_schema_frontier() unchanged --
the same real vector/lexical/business-concept/authority/snapshot machinery
the existing schema-selection stage already exercises.
"""

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as schema_object_authority_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
import data.business_knowledge_package_service as business_knowledge_package_service_module
import data.db as data_db_module
from core.sqlservice.fast_path_context import (
    STATUS_OK,
    _CATEGORY_LABELS,
    _PRIORITY_DESCRIPTIVE,
    _PRIORITY_JOIN_KEYS,
    _assemble_bounded_context,
    _candidate_prioritized_lines,
    build_fast_path_context,
    render_fast_path_candidate,
)
from core.sqlservice.view_profiler import ViewProfile, ViewSuitability
from core.sqlservice.schema_frontier import FrontierCandidate
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache
from core.sqlservice.sql_example_vector_index import clear_sql_example_vector_index_cache
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    FakeEmbeddingProvider,
    _make_connection,
    _prepare_source,
    template_db_path,  # noqa: F401
)


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    import shutil

    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_object_authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(business_knowledge_package_service_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()
    yield
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    clear_schema_object_authority_cache()


# ── 1. Basic shape: OK status, object_count/characters/tokens recorded ──


def test_build_fast_path_context_records_observability_fields(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = FakeEmbeddingProvider()

    context = build_fast_path_context("How many widgets are there?", build.id, build.source_id, "alice", provider)

    assert context.status == STATUS_OK
    assert context.object_count == len(context.frontier.candidates)
    assert context.object_count >= 1
    assert context.total_characters == len(context.context_text)
    assert context.estimated_tokens > 0
    assert context.build_id == build.id
    assert context.schema_fingerprint == build.schema_fingerprint
    assert len(provider.calls) == 1


# ── 2. render_fast_path_candidate adds JOIN_KEYS only when present ─────


def _bare_candidate(fqn="dbo.widgets", **overrides):
    base = dict(
        object_fqn=fqn, object_type="TABLE", retrieval_tier=1,
        business_name=None, business_purpose=None, domain=None, aliases=(),
        primary_key_columns=("id",), foreign_key_summary=(), local_foreign_key_columns=(),
        measure_columns=(), date_columns=(), status_columns=(), dimension_columns=("id", "name"),
        confirmed_relationship_neighbors=(), semantic_rank=1, semantic_score=0.9,
        lexical_rank=None, lexical_score=None, business_concept_rank=None,
        business_concept_score=None, canonical_rank=None, canonical_total_score=None,
        fused_rank=1, fused_score=0.05, admitted_via_lanes=("semantic",),
        matched_business_concepts=(),
    )
    base.update(overrides)
    return FrontierCandidate(**base)


def test_render_fast_path_candidate_omits_join_keys_when_absent():
    candidate = _bare_candidate()
    rendered = render_fast_path_candidate(candidate)
    assert "JOIN_KEYS" not in rendered


def test_render_fast_path_candidate_includes_join_keys_when_present():
    candidate = _bare_candidate(confirmed_relationship_column_pairs=("id -> dbo.orders.widget_id",))
    rendered = render_fast_path_candidate(candidate)
    assert "JOIN_KEYS: id -> dbo.orders.widget_id" in rendered
    # Never a truncation of the base render -- every base line still present.
    assert "OBJECT: dbo.widgets" in rendered
    assert "PRIMARY_KEY: id" in rendered


# ── 3. Empty/non-OK frontier propagates as a usable, zero-object result ─


def test_no_ready_build_propagates_non_ok_status(db_path, monkeypatch):
    provider = FakeEmbeddingProvider()

    context = build_fast_path_context("How many widgets?", 999999, 1, "alice", provider)

    assert context.status != STATUS_OK
    assert context.object_count == 0
    assert context.context_text == ""
    assert len(provider.calls) == 0


# ── 4. Part 5: one final bounded assembly, never unlimited appends ────────


def _full_view_profile(fqn="dbo.v_many"):
    return ViewProfile(
        object_fqn=fqn, source_id=1, build_id=1, schema_fingerprint="fp", definition_hash="h",
        profiling_version=1, completion_status="COMPLETE", failure_reason=None,
        grain_class="BASE_ROW", authority_class="UNKNOWN", lineage_complete=True, is_framework_backed=False,
        entity_keys=("Id",), required_distinct_key="Id", duplication_risk="POSSIBLE_FANOUT",
        row_preservation_risk="PRESERVED", population_restrictions=("WHERE Status = 'ACTIVE'",),
        secret_columns=("PasswordHash",), pii_categories=("Email:TIER1",),
        suitability=ViewSuitability(
            suitable_for_detail=True, suitable_for_count=False, suitable_for_aggregation=True,
            suitable_for_join=True, unsuitable_reasons=("COUNT:fanout_risk",), required_distinct_key="Id",
            population_restriction="WHERE Status = 'ACTIVE'", confidence="HIGH",
        ),
        created_at="2026-10-01T00:00:00Z", updated_at="2026-10-01T00:00:00Z",
    )


def test_assemble_bounded_context_never_exceeds_configured_maximum_with_many_views():
    candidates = [_bare_candidate(fqn=f"dbo.v_{i}", object_type="VIEW") for i in range(40)]
    per_candidate = [_candidate_prioritized_lines(c, _full_view_profile(c.object_fqn)) for c in candidates]

    for budget in (200, 500, 2000, 10_000, 100_000):
        text, total_characters, warnings = _assemble_bounded_context(per_candidate, budget)
        assert total_characters == len(text)
        assert total_characters <= budget, f"exceeded budget={budget}: got {total_characters}"


def test_assemble_bounded_context_drops_descriptive_before_join_keys():
    candidates = [
        _bare_candidate(fqn=f"dbo.v_{i}", object_type="VIEW", confirmed_relationship_column_pairs=(f"id -> dbo.t.{i}",))
        for i in range(20)
    ]
    per_candidate = [_candidate_prioritized_lines(c, _full_view_profile(c.object_fqn)) for c in candidates]

    # A budget tight enough to force trimming, but loose enough that the
    # pinned base text + join_keys for all 20 candidates still fits.
    full_text, _, _ = _assemble_bounded_context(per_candidate, 10_000_000)
    base_and_joinkeys_only = "\n\n".join(
        base + "\n" + by_priority[_PRIORITY_JOIN_KEYS] for base, by_priority in per_candidate
    )
    budget = len(base_and_joinkeys_only) + 50

    text, total_characters, warnings = _assemble_bounded_context(per_candidate, budget)

    assert total_characters <= budget
    assert "JOIN_KEYS:" in text
    assert "VIEW_PROFILE_NOTES:" not in text  # lowest-priority category dropped first
    assert any(_CATEGORY_LABELS[_PRIORITY_DESCRIPTIVE] in w for w in warnings)


def test_assemble_bounded_context_never_touches_pinned_base_text():
    candidates = [_bare_candidate(fqn="dbo.widgets")]
    per_candidate = [_candidate_prioritized_lines(c, None) for c in candidates]
    base_text = per_candidate[0][0]

    # A budget smaller than even the single pinned base block.
    text, total_characters, warnings = _assemble_bounded_context(per_candidate, len(base_text) - 10)

    assert any("hard_truncated" in w for w in warnings)
    assert total_characters <= len(base_text) - 10


def test_build_fast_path_context_respects_configured_maximum_end_to_end(db_path, monkeypatch):
    build = _prepare_source(db_path)
    provider = FakeEmbeddingProvider()

    # A generous-enough budget that the frontier itself still admits real
    # candidates (so this exercises the real wiring, not an empty-frontier
    # edge case), but small enough that it is a genuine, checkable cap.
    context = build_fast_path_context(
        "How many widgets are there?", build.id, build.source_id, "alice", provider,
        max_context_characters=2000,
    )

    assert context.status == STATUS_OK
    assert context.total_characters <= 2000
    assert context.total_characters == len(context.context_text)
    # warnings is always the frontier's own warnings PLUS any trim
    # warnings -- never fewer than the frontier alone would have produced.
    assert set(context.frontier.warnings).issubset(set(context.warnings))
