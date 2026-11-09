"""
Focused tests -- SQL Architecture Correction, Phase 6, Step 6C1: the deep
VIEW_ENRICHED persistence contract itself
(core/sqlservice/semantic_enrichment_store.py's view-specific functions +
sql_view_semantic_enrichments, created via migrations/017_create_view_
semantic_enrichments.sql / data/models.py's ensure_sql_view_semantic_
enrichments_table). Not covered by Step 6C's own test suite -- this file
closes that gap.

Uses a temporary on-disk SQLite database only -- never the real
data/toolsmith.db. Every EnrichedViewObjectSemantics/EnrichedColumnSemantics
used here comes from the REAL construction gate
(generate_view_semantic_enrichment with a fake provider) -- never
hand-built, mirroring tests/test_sqlservice_semantic_enrichment_
persistence.py's own discipline for tables.
"""

from __future__ import annotations

import os
import shutil
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step6c1-persistence-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-step6c1-persistence-salt-long-enough-1234")

import data.models as models_module
import core.sqlservice.semantic_enrichment_store as store_module
from core.sqlservice.semantic_enrichment_providers import ProviderSemanticEnrichmentResponse
from core.sqlservice.semantic_enrichment_service import (
    ColumnSemanticInput,
    PHASE_VIEW_ENRICHED,
    SemanticEnrichmentProvider,
    ViewSemanticInput,
    compute_view_cache_key,
    generate_view_semantic_enrichment,
)
from core.sqlservice.semantic_enrichment_store import (
    COMPLETENESS_STATUS_OK,
    COMPLETENESS_STATUS_UNOWNED,
    OUTCOME_CREATED,
    OUTCOME_REPAIRED,
    OUTCOME_REUSED,
    OUTCOME_UNOWNED,
    STATUS_ACTIVE,
    STATUS_INVALID,
    count_view_enrichment_completeness,
    get_view_enrichment_by_cache_key,
    insert_view_semantic_enrichment,
    load_current_view_enrichments_for_source,
    mark_view_enrichment_invalid_by_id,
)


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
    monkeypatch.setattr(store_module, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-16T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


class _FakeProvider(SemanticEnrichmentProvider):
    def __init__(self, response, provider_id="fake", model="fake-model-1"):
        self._response = response
        self._provider_id = provider_id
        self._model = model

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def enrich_batch(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        return ProviderSemanticEnrichmentResponse(raw=self._response, model=self._model)


def _view_input(object_fqn="dbo.ActiveStudents") -> ViewSemanticInput:
    return ViewSemanticInput(
        object_fqn=object_fqn, schema_name="dbo", view_name=object_fqn.split(".")[-1], retrieval_tier=1,
        columns=(ColumnSemanticInput("StudentId", "INTEGER", True, False),),
        primary_key_columns=("StudentId",), dictionary_business_name=None, dictionary_description=None,
        dictionary_synonyms=(), definition_evidence="SELECT StudentId FROM dbo.Students", definition_truncated=False,
        dependency_edges=(), confirmed_relationship_summary=(),
    )


def _real_enrichment(object_fqn="dbo.ActiveStudents", *, provider_id="fake", model="fake-model-1"):
    view_input = _view_input(object_fqn)
    response = {
        "views": [{
            "id": "v1", "business_domain": "Enrollment", "business_name": "Active Students",
            "description": "Currently active students.", "source_role": "CURRENT_STATE_VIEW",
            "entity_concepts": ["student"], "status_concepts": [], "aliases": ["current students"],
            "operations": ["count active students"], "confidence": 0.8,
            "columns": [],
        }]
    }
    provider = _FakeProvider(response, provider_id=provider_id, model=model)
    result = generate_view_semantic_enrichment([view_input], lambda: provider)
    assert result.generated_count == 1
    obj = result.enriched_views[0]
    cols = [c for c in result.enriched_columns if c.object_fqn == obj.object_fqn]
    input_hash = result.cache_keys_by_object_fqn[obj.object_fqn].semantic_input_hash
    return obj, cols, input_hash


# ── Schema shape ────────────────────────────────────────────────────────


def test_table_and_indexes_exist(db_path):
    conn = _make_connection(db_path)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sql_view_semantic_enrichments'"
    ).fetchone() is not None
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(sql_view_semantic_enrichments)").fetchall()}
    assert {"idx_svse_cache_key", "idx_svse_source_current", "idx_svse_source_object"} <= indexes
    conn.close()


# ── insert_view_semantic_enrichment: CREATED/REUSED/REPAIRED/UNOWNED ──────


def test_insert_creates_new_row(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    result = insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert result.outcome == OUTCOME_CREATED
    assert result.enrichment.object_fqn == "dbo.ActiveStudents"
    assert result.enrichment.business_domain == "Enrollment"
    assert result.enrichment.phase == PHASE_VIEW_ENRICHED
    assert result.enrichment.status == STATUS_ACTIVE


def test_insert_same_key_twice_is_reused_not_duplicated(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    kwargs = dict(
        source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    first = insert_view_semantic_enrichment("alice", **kwargs)
    second = insert_view_semantic_enrichment("alice", **kwargs)
    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_REUSED
    assert first.enrichment.id == second.enrichment.id

    conn = _make_connection(db_path)
    count = conn.execute("SELECT COUNT(*) c FROM sql_view_semantic_enrichments").fetchone()["c"]
    assert count == 1
    conn.close()


def test_insert_repairs_invalid_row_at_same_key(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    kwargs = dict(
        source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    created = insert_view_semantic_enrichment("alice", **kwargs)
    assert mark_view_enrichment_invalid_by_id(created.enrichment.id, source_id=1, user_id="alice") is True

    repaired = insert_view_semantic_enrichment("alice", **kwargs)
    assert repaired.outcome == OUTCOME_REPAIRED
    assert repaired.enrichment.id == created.enrichment.id
    assert repaired.enrichment.status == STATUS_ACTIVE


def test_insert_unowned_source_returns_unowned_not_raise(db_path):
    _insert_source(db_path, source_id=1, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    result = insert_view_semantic_enrichment(
        "mallory", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert result.outcome == OUTCOME_UNOWNED
    assert result.enrichment is None


def test_insert_rejects_non_gated_object_semantics(db_path):
    _insert_source(db_path, source_id=1)
    with pytest.raises(ValueError):
        insert_view_semantic_enrichment(
            "alice", source_id=1, object_fqn="dbo.X", semantic_input_hash="a" * 64,
            provider_id="fake", model="fake-model-1", contract_version=1,
            object_semantics={"business_name": "hacked"},  # type: ignore[arg-type]
        )


def test_insert_rejects_object_fqn_mismatch(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment("dbo.ActiveStudents")
    with pytest.raises(ValueError):
        insert_view_semantic_enrichment(
            "alice", source_id=1, object_fqn="dbo.SomeOtherView", semantic_input_hash=input_hash,
            provider_id="fake", model="fake-model-1", contract_version=1,
            object_semantics=obj, column_semantics=cols,
        )


# ── get_view_enrichment_by_cache_key / load_current_view_enrichments_for_source ──


def test_get_by_cache_key_round_trips(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    fetched = get_view_enrichment_by_cache_key(
        1, "alice", object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert fetched is not None
    assert fetched.business_domain == "Enrollment"
    assert fetched.source_role == "CURRENT_STATE_VIEW"


def test_get_by_cache_key_wrong_hash_returns_none(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    fetched = get_view_enrichment_by_cache_key(
        1, "alice", object_fqn=obj.object_fqn, semantic_input_hash="f" * 64,
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert fetched is None


def test_load_current_excludes_stale_hash(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    current = load_current_view_enrichments_for_source(
        1, "alice", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={obj.object_fqn: "f" * 64},  # stale on purpose
    )
    assert current == ()

    current_matching = load_current_view_enrichments_for_source(
        1, "alice", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={obj.object_fqn: input_hash},
    )
    assert len(current_matching) == 1


def test_ownership_isolation_between_sources(db_path):
    _insert_source(db_path, source_id=1, user_id="alice")
    _insert_source(db_path, source_id=2, user_id="bob")
    obj, cols, input_hash = _real_enrichment()
    insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    bob_view = get_view_enrichment_by_cache_key(
        2, "bob", object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert bob_view is None  # bob's source has no such row -- correctly isolated


# ── count_view_enrichment_completeness ─────────────────────────────────────


def test_completeness_reports_missing_and_current(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment("dbo.V1")
    insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    result = count_view_enrichment_completeness(
        1, "alice", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.V1": input_hash, "dbo.V2": "b" * 64},
    )
    assert result.status == COMPLETENESS_STATUS_OK
    assert result.requested_count == 2
    assert result.cached_current_count == 1
    assert result.missing_object_fqns == ("dbo.V2",)


def test_completeness_unowned_source(db_path):
    result = count_view_enrichment_completeness(
        999, "nobody", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.V1": "a" * 64},
    )
    assert result.status == COMPLETENESS_STATUS_UNOWNED


# ── mark_view_enrichment_invalid_by_id ─────────────────────────────────────


def test_mark_invalid_excludes_from_future_reads(db_path):
    _insert_source(db_path, source_id=1)
    obj, cols, input_hash = _real_enrichment()
    created = insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert mark_view_enrichment_invalid_by_id(created.enrichment.id, source_id=1, user_id="alice") is True
    fetched = get_view_enrichment_by_cache_key(
        1, "alice", object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert fetched is None  # INVALID rows are excluded, never surfaced

    conn = _make_connection(db_path)
    row = conn.execute("SELECT status, payload_json FROM sql_view_semantic_enrichments WHERE id = ?", (created.enrichment.id,)).fetchone()
    assert row["status"] == STATUS_INVALID
    assert row["payload_json"]  # payload_json is retained, never wiped
    conn.close()


def test_mark_invalid_wrong_owner_returns_false(db_path):
    _insert_source(db_path, source_id=1, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    created = insert_view_semantic_enrichment(
        "alice", source_id=1, object_fqn=obj.object_fqn, semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert mark_view_enrichment_invalid_by_id(created.enrichment.id, source_id=1, user_id="mallory") is False


# ── Cache-key round-trip through the real compute_view_cache_key ──────────


def test_compute_view_cache_key_matches_what_generate_view_semantic_enrichment_used(db_path):
    _insert_source(db_path, source_id=1)
    view_input = _view_input()
    obj, cols, input_hash = _real_enrichment()
    recomputed = compute_view_cache_key(view_input, "fake", "fake-model-1")
    assert recomputed.semantic_input_hash == input_hash
