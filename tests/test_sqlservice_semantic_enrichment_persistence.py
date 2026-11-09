"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4B: semantic-
enrichment persistence (core/sqlservice/semantic_enrichment_store.py,
migrations/015_create_semantic_enrichment.sql, data/models.py's mirror).

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db. Every EnrichedObjectSemantics/EnrichedColumnSemantics
used here comes from the REAL construction gate (core.sqlservice.
semantic_enrichment_service.generate_semantic_enrichment with a fake
provider) — never hand-built, mirroring
tests/test_sqlservice_semantic_enrichment.py's own discipline.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_persistence.py -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step4b-persistence-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-step4b-persistence-salt-long-enough-1234567")

import data.models as models_module
import core.sqlservice.semantic_enrichment_store as store_module
from core.sqlservice.semantic_enrichment_service import (
    EnrichedColumnSemantics,
    EnrichedObjectSemantics,
    SemanticEnrichmentProvider,
    generate_semantic_enrichment,
)
from core.sqlservice.semantic_enrichment_providers import ProviderSemanticEnrichmentResponse
from core.sqlservice.semantic_enrichment_store import (
    COMPLETENESS_STATUS_OK,
    COMPLETENESS_STATUS_UNOWNED,
    OUTCOME_CREATED,
    OUTCOME_REPAIRED,
    OUTCOME_REUSED,
    OUTCOME_UNOWNED,
    STATUS_ACTIVE,
    STATUS_INVALID,
    count_enrichment_completeness,
    get_enrichment_by_cache_key,
    insert_semantic_enrichment,
    load_current_enrichments_for_source,
    mark_enrichment_invalid_by_id,
)


# ── Fixture plumbing (mirrors tests/test_sqlservice_sql_example_persistence.py) ─

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
    import shutil
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(store_module, "get_connection", lambda: _make_connection(path))
    return path


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-12T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _delete_source(db_path, source_id):
    conn = _make_connection(db_path)
    conn.execute("DELETE FROM data_source_connections WHERE id = ?", (source_id,))
    conn.commit()
    conn.close()


# ── Real EnrichedObjectSemantics/EnrichedColumnSemantics factory (through
#    the actual construction gate — never hand-built) ─────────────────────

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


class _Doc:
    def __init__(self, object_fqn, retrieval_tier=1, **meta):
        self.object_fqn = object_fqn
        self.object_type = "TABLE"
        self.retrieval_tier = retrieval_tier
        base = {
            "business_name": None, "description": None, "synonyms": (),
            "column_descriptors": (
                {"column_name": "StuID", "normalized_sql_type": "INTEGER", "is_primary_key": True, "is_foreign_key": False},
            ),
            "primary_key_columns": ("StuID",),
            "confirmed_relationship_edges": (),
        }
        base.update(meta)
        self.metadata = base


def _real_enrichment(object_fqn="dbo.Stu", *, provider_id="fake", model="fake-model-1", business_name="Student"):
    doc = _Doc(object_fqn)
    response = {
        "objects": [{
            "object_fqn": object_fqn, "business_name": business_name, "description": "A student",
            "object_role": "ENTITY", "entity_concepts": ["student"], "aliases": ["learner"],
            "supported_operations": ["COUNT"], "confidence": 0.8,
            "columns": [{
                "column_name": "StuID", "business_label": "Student ID", "semantic_role": "IDENTIFIER",
                "date_role": None, "measure_role": None, "dimension_role": None, "identifier_role": "PRIMARY_KEY",
                "confidence": 0.7,
            }],
        }]
    }
    result = generate_semantic_enrichment([doc], _FakeProvider(response, provider_id=provider_id, model=model))
    assert result.generated_count == 1
    obj = result.enriched_objects[0]
    cols = [c for c in result.enriched_columns if c.object_fqn == obj.object_fqn]
    input_hash = result.cache_keys_by_object_fqn[obj.object_fqn].semantic_input_hash
    return obj, cols, input_hash


# ── Migration schema / idempotency ─────────────────────────────────────────


def test_migration_creates_table_and_indexes_idempotently(db_path):
    conn = _make_connection(db_path)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sql_semantic_enrichments'"
    ).fetchall()}
    assert tables == {"sql_semantic_enrichments"}
    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sql_semantic_enrichments'"
    ).fetchall()}
    assert {"idx_sse_cache_key", "idx_sse_source_current", "idx_sse_source_object"} <= indexes
    conn.close()

    # Re-running init_db() must not fail or duplicate anything (additive/idempotent).
    original_get_connection = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(db_path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original_get_connection


def test_no_sensitive_columns_in_schema(db_path):
    conn = _make_connection(db_path)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(sql_semantic_enrichments)").fetchall()}
    conn.close()
    forbidden_substrings = ("prompt", "question", "credential", "connection_string", "raw_response", "exception")
    for column in columns:
        for forbidden in forbidden_substrings:
            assert forbidden not in column.lower()


# ── Ownership enforcement ──────────────────────────────────────────────────


def test_insert_reports_unowned_for_wrong_user(db_path):
    _insert_source(db_path, source_id=1, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    result = insert_semantic_enrichment(
        "mallory", source_id=1, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert result.outcome == OUTCOME_UNOWNED
    assert result.enrichment is None


def test_load_and_get_return_nothing_for_wrong_user(db_path):
    _insert_source(db_path, source_id=2, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    insert_semantic_enrichment(
        "alice", source_id=2, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert get_enrichment_by_cache_key(
        2, "mallory", object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    ) is None
    assert load_current_enrichments_for_source(
        2, "mallory", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.Stu": input_hash},
    ) == ()


# ── Exact cache-key reuse / idempotent insert ──────────────────────────────


def test_exact_cache_key_reuse_is_idempotent(db_path):
    _insert_source(db_path, source_id=3, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    first = insert_semantic_enrichment(
        "alice", source_id=3, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    second = insert_semantic_enrichment(
        "alice", source_id=3, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_REUSED
    assert first.enrichment.id == second.enrichment.id

    conn = _make_connection(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM sql_semantic_enrichments WHERE source_id = 3").fetchone()["c"]
    conn.close()
    assert count == 1


def test_stale_hash_is_excluded_from_current_load(db_path):
    _insert_source(db_path, source_id=4, user_id="alice")
    obj, cols, old_hash = _real_enrichment()
    insert_semantic_enrichment(
        "alice", source_id=4, object_fqn="dbo.Stu", semantic_input_hash=old_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    new_hash = "a" * 64  # simulates a schema change producing a different current hash
    current = load_current_enrichments_for_source(
        4, "alice", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.Stu": new_hash},
    )
    assert current == ()
    # ... but the OLD hash is still retrievable directly -- historical
    # versions are retained, never deleted.
    assert get_enrichment_by_cache_key(
        4, "alice", object_fqn="dbo.Stu", semantic_input_hash=old_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    ) is not None


def test_provider_model_contract_version_are_isolated_cache_dimensions(db_path):
    _insert_source(db_path, source_id=5, user_id="alice")
    obj, cols, input_hash = _real_enrichment(provider_id="fake", model="fake-model-1")

    r1 = insert_semantic_enrichment(
        "alice", source_id=5, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="openai", model="gpt-4o-mini", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    r2 = insert_semantic_enrichment(
        "alice", source_id=5, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="openai", model="gpt-4o", contract_version=1,  # different model
        object_semantics=obj, column_semantics=cols,
    )
    r3 = insert_semantic_enrichment(
        "alice", source_id=5, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="openai", model="gpt-4o-mini", contract_version=2,  # different contract version
        object_semantics=obj, column_semantics=cols,
    )
    assert {r1.outcome, r2.outcome, r3.outcome} == {OUTCOME_CREATED}
    assert len({r1.enrichment.id, r2.enrichment.id, r3.enrichment.id}) == 3


# ── Immutable decode ────────────────────────────────────────────────────────


def test_stored_enrichment_is_immutably_reconstructed(db_path):
    _insert_source(db_path, source_id=6, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    insert_semantic_enrichment(
        "alice", source_id=6, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    stored = get_enrichment_by_cache_key(
        6, "alice", object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    )
    assert isinstance(stored.entity_concepts, tuple)
    assert isinstance(stored.columns, tuple)
    assert isinstance(stored.columns[0].provenance, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stored.business_name = "hacked"
    with pytest.raises(dataclasses.FrozenInstanceError):
        stored.columns[0].confidence = 0.99


# ── Malformed/corrupt payload excluded + repair on resume ─────────────────


def test_corrupt_payload_is_excluded_and_repaired_on_resume(db_path):
    _insert_source(db_path, source_id=7, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    created = insert_semantic_enrichment(
        "alice", source_id=7, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert created.outcome == OUTCOME_CREATED

    # Simulate corruption directly at the storage layer (never through this
    # module's own API, which never writes malformed JSON).
    conn = _make_connection(db_path)
    conn.execute(
        "UPDATE sql_semantic_enrichments SET payload_json = ? WHERE id = ?",
        ("{not valid json", created.enrichment.id),
    )
    conn.commit()
    conn.close()

    assert get_enrichment_by_cache_key(
        7, "alice", object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    ) is None
    assert load_current_enrichments_for_source(
        7, "alice", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.Stu": input_hash},
    ) == ()

    # Safe resume: re-inserting at the SAME cache key repairs the row in
    # place rather than conflicting or leaving it permanently corrupt.
    repaired = insert_semantic_enrichment(
        "alice", source_id=7, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert repaired.outcome == OUTCOME_REPAIRED
    assert repaired.enrichment.id == created.enrichment.id
    assert get_enrichment_by_cache_key(
        7, "alice", object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    ) is not None


def test_mark_invalid_excludes_without_deleting_content(db_path):
    _insert_source(db_path, source_id=8, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    created = insert_semantic_enrichment(
        "alice", source_id=8, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert mark_enrichment_invalid_by_id(created.enrichment.id, 8, "alice") is True
    assert get_enrichment_by_cache_key(
        8, "alice", object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
    ) is None

    conn = _make_connection(db_path)
    row = conn.execute("SELECT status, payload_json FROM sql_semantic_enrichments WHERE id = ?", (created.enrichment.id,)).fetchone()
    conn.close()
    assert row["status"] == STATUS_INVALID
    assert json.loads(row["payload_json"])["object"]["business_name"] == "Student"  # content retained, not deleted

    # Re-inserting at the same key repairs it back to ACTIVE.
    repaired = insert_semantic_enrichment(
        "alice", source_id=8, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert repaired.outcome == OUTCOME_REPAIRED


def test_mark_invalid_reports_false_for_wrong_owner_or_unknown_id(db_path):
    _insert_source(db_path, source_id=9, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    created = insert_semantic_enrichment(
        "alice", source_id=9, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    assert mark_enrichment_invalid_by_id(created.enrichment.id, 9, "mallory") is False
    assert mark_enrichment_invalid_by_id(999999, 9, "alice") is False


# ── Cascade behavior ────────────────────────────────────────────────────────


def test_cascade_delete_removes_enrichment_rows(db_path):
    _insert_source(db_path, source_id=10, user_id="alice")
    obj, cols, input_hash = _real_enrichment()
    insert_semantic_enrichment(
        "alice", source_id=10, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    _delete_source(db_path, source_id=10)
    conn = _make_connection(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM sql_semantic_enrichments WHERE source_id = 10").fetchone()["c"]
    conn.close()
    assert count == 0


# ── Never allow raw/hand-built input ───────────────────────────────────────


def test_insert_rejects_non_gated_object_semantics(db_path):
    _insert_source(db_path, source_id=11, user_id="alice")
    with pytest.raises(ValueError):
        insert_semantic_enrichment(
            "alice", source_id=11, object_fqn="dbo.Stu", semantic_input_hash="a" * 64,
            provider_id="fake", model="fake-model-1", contract_version=1,
            object_semantics={"business_name": "hand built dict"}, column_semantics=(),
        )


def test_insert_rejects_mismatched_object_fqn(db_path):
    _insert_source(db_path, source_id=12, user_id="alice")
    obj, cols, input_hash = _real_enrichment(object_fqn="dbo.Stu")
    with pytest.raises(ValueError):
        insert_semantic_enrichment(
            "alice", source_id=12, object_fqn="dbo.SomethingElse", semantic_input_hash=input_hash,
            provider_id="fake", model="fake-model-1", contract_version=1,
            object_semantics=obj, column_semantics=cols,
        )


# ── Completeness counting ──────────────────────────────────────────────────


def test_completeness_counts_missing_and_cached(db_path):
    _insert_source(db_path, source_id=13, user_id="alice")
    obj, cols, input_hash = _real_enrichment(object_fqn="dbo.Stu")
    insert_semantic_enrichment(
        "alice", source_id=13, object_fqn="dbo.Stu", semantic_input_hash=input_hash,
        provider_id="fake", model="fake-model-1", contract_version=1,
        object_semantics=obj, column_semantics=cols,
    )
    result = count_enrichment_completeness(
        13, "alice", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.Stu": input_hash, "dbo.Course": "b" * 64},
    )
    assert result.status == COMPLETENESS_STATUS_OK
    assert result.requested_count == 2
    assert result.cached_current_count == 1
    assert result.missing_object_fqns == ("dbo.Course",)


def test_completeness_reports_unowned(db_path):
    _insert_source(db_path, source_id=14, user_id="alice")
    result = count_enrichment_completeness(
        14, "mallory", provider_id="fake", model="fake-model-1", contract_version=1,
        current_hashes_by_object_fqn={"dbo.Stu": "a" * 64},
    )
    assert result.status == COMPLETENESS_STATUS_UNOWNED
