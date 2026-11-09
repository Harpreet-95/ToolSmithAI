"""
Focused unit tests — SQL rebuild Phase 2, Step 5: connection-time
schema-retrieval preparation service (core/sqlservice/retrieval_preparation.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server, never OpenAI. Every test injects a
FakeEmbeddingProvider defined in this file. Schema is created via the real
`data.models.init_db()` (monkeypatched to a temp file, then copied per test
for speed) so these tests exercise the actual production schema — the
first stage genuinely runs the full Phase 1 pipeline (schema_snapshot ->
retrieval documents -> fingerprint -> persist), unlike earlier Phase 2
test files which hand-inserted sql_retrieval_builds/documents rows
directly.

Run:
    venv/Scripts/pytest tests/test_sqlservice_retrieval_preparation.py -v
"""

import dataclasses
import json
import os
import shutil
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import data.schema_service as schema_service_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.metadata_loader as metadata_loader_module
import core.sqlservice.retrieval_preparation as retrieval_preparation_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.vector_index as vector_index_module
from core.connectors.schema import ColumnInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.sqlservice.embedding_providers import EmbeddingProvider, ProviderEmbeddingBatchResponse, ProviderEmbeddingItem
from core.sqlservice.embedding_store import STATUS_BUILD_NOT_READY as EMB_STATUS_BUILD_NOT_READY
from core.sqlservice.embedding_store import STATUS_OK as EMB_STATUS_OK
from core.sqlservice.retrieval_preparation import (
    BUILD_OUTCOME_CREATED,
    BUILD_OUTCOME_REUSED,
    STATUS_DOCUMENT_BUILD_FAILED,
    STATUS_EMBEDDING_FAILED,
    STATUS_OK,
    STATUS_VECTOR_INDEX_FAILED,
    RetrievalPreparationResult,
    prepare_source_for_retrieval,
)
from core.sqlservice.vector_index import STATUS_BUILD_NOT_READY as VI_STATUS_BUILD_NOT_READY
from core.sqlservice.vector_index import clear_vector_index_cache


# ── Fixture plumbing (mirrors tests/test_sqlservice_retrieval_coordinator.py,
#    extended with embedding_store's connection for the embedding stage) ──

def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("template") / "template.db")
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
    monkeypatch.setattr(metadata_loader_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    """The vector-index cache is a module-level global reused across every
    test's small build_id numbering — must be cleared before/after each
    test (see the identical fixture in tests/test_sqlservice_vector_index.py)."""
    clear_vector_index_cache()
    yield
    clear_vector_index_cache()


def _insert_source(db_path, source_id, user_id="alice"):
    conn = sqlite3.connect(db_path)
    now = "2026-09-04T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " created_at, updated_at) VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _col(name, data_type, *, pk=False, ordinal=1):
    return ColumnInfo(
        column_name=name, ordinal_position=ordinal, data_type=data_type,
        raw_type=data_type.lower(), is_nullable=True, is_primary_key=pk, is_identity=pk,
    )


def _table(schema_name, table_name, columns):
    return TableInfo(
        table_name=table_name, schema_name=schema_name, table_fqn=f"{schema_name}.{table_name}",
        table_type="TABLE", columns=columns,
        primary_keys=[PrimaryKeyInfo(c.column_name, 1) for c in columns if c.is_primary_key],
        foreign_keys=[],
    )


def _snapshot_json(tables, source_id=1, discovered_at="2026-09-04T00:00:00Z"):
    snapshot = SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at=discovered_at,
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )
    return json.dumps(dataclasses.asdict(snapshot))


def _insert_snapshot(db_path, snapshot_id, source_id, version, tables=None):
    if tables is None:
        tables = [_table("dbo", "learners", [_col("id", "INTEGER", pk=True)])]
    conn = sqlite3.connect(db_path)
    now = "2026-09-04T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', ?, ?, ?)",
        (snapshot_id, source_id, version, _snapshot_json(tables, source_id=source_id), now, now),
    )
    conn.commit()
    conn.close()


def _default_setup(db_path, source_id=1, user_id="alice", tables=None):
    _insert_source(db_path, source_id, user_id)
    _insert_snapshot(db_path, snapshot_id=1, source_id=source_id, version=1, tables=tables)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Records every embed_batch() call. Returns one deterministic unit
    vector per text (derived from the text's own length so distinct texts
    normally get distinct-ish vectors), always valid."""

    def __init__(self, dimensions=4, provider_id="fake", model="fake-model"):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
        self.calls: list[list[str]] = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    @property
    def dimensions(self):
        return self._dimensions

    def embed_batch(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        items = tuple(
            ProviderEmbeddingItem(
                index=i,
                vector=tuple(
                    (1.0 if j == (len(text) % self._dimensions) else 0.1) for j in range(self._dimensions)
                ),
            )
            for i, text in enumerate(texts)
        )
        return ProviderEmbeddingBatchResponse(items=items, model=self._model)


def _make_clock(values):
    it = iter(values)
    return lambda: next(it)


# ── 1. Successful new preparation ──────────────────────────────────────────

def test_successful_new_preparation(db_path):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_retrieval(1, "alice", provider)

    assert result.status == STATUS_OK
    assert result.build_outcome == BUILD_OUTCOME_CREATED
    assert result.build_id is not None
    assert result.snapshot_id == 1
    assert result.snapshot_version == 1
    assert result.schema_fingerprint is not None
    assert result.total_document_count == 1
    assert result.embeddings_generated == 1
    assert result.embeddings_reused == 0
    assert result.vector_index_document_count == 1
    assert result.error is None
    assert len(provider.calls) == 1
    assert result.embedding_provider == "fake"
    assert result.embedding_model == "fake-model"
    assert result.embedding_dimensions == 4


# ── 2. Unchanged build: zero embedding calls on the second run ────────────

def test_unchanged_schema_reuses_build_and_makes_zero_embedding_calls(db_path):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider()

    first = prepare_source_for_retrieval(1, "alice", provider)
    assert first.status == STATUS_OK
    assert first.build_outcome == BUILD_OUTCOME_CREATED
    calls_after_first = len(provider.calls)

    second = prepare_source_for_retrieval(1, "alice", provider)

    assert second.status == STATUS_OK
    assert second.build_outcome == BUILD_OUTCOME_REUSED
    assert second.build_id == first.build_id
    assert second.embeddings_generated == 0
    assert second.embeddings_reused == 1
    assert second.vector_index_document_count == 1
    assert len(provider.calls) == calls_after_first  # no NEW provider calls


# ── 3. Changed schema: partial hash reuse ──────────────────────────────────

def test_changed_schema_creates_new_build_and_reuses_unchanged_hashes(db_path):
    unchanged_table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)])
    _default_setup(db_path, tables=[unchanged_table])
    provider = FakeEmbeddingProvider()

    first = prepare_source_for_retrieval(1, "alice", provider)
    assert first.status == STATUS_OK
    assert first.embeddings_generated == 1

    # A real schema change: a new table added, the original table's shape
    # (and therefore its document_text/hash) left completely unchanged.
    new_table = _table("dbo", "tracks", [_col("id", "INTEGER", pk=True)])
    _insert_snapshot(db_path, snapshot_id=2, source_id=1, version=2, tables=[unchanged_table, new_table])
    provider.calls.clear()

    second = prepare_source_for_retrieval(1, "alice", provider)

    assert second.status == STATUS_OK
    assert second.build_outcome == BUILD_OUTCOME_CREATED
    assert second.build_id != first.build_id
    assert second.total_document_count == 2
    assert second.embeddings_generated == 1  # only the new table
    assert second.embeddings_reused == 1     # the unchanged table's hash, reused
    assert second.vector_index_document_count == 2
    assert len(provider.calls) == 1
    assert "dbo.tracks" in provider.calls[0][0]
    assert "dbo.learners" not in provider.calls[0][0]  # never re-sent to the provider


# ── 4. Metadata / retrieval-build failure ──────────────────────────────────

def test_no_snapshot_is_document_build_failure(db_path):
    _insert_source(db_path, 1)  # no schema_snapshots row at all
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_retrieval(1, "alice", provider)

    assert result.status == STATUS_DOCUMENT_BUILD_FAILED
    assert result.build_id is None
    assert result.total_document_count == 0
    assert result.embeddings_generated == 0
    assert result.vector_index_document_count == 0
    assert "NO_SNAPSHOT" in result.error or "METADATA_LOAD_FAILED" in result.error
    assert provider.calls == []


def test_unowned_source_is_document_build_failure(db_path):
    _default_setup(db_path, user_id="alice")
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_retrieval(1, "mallory", provider)

    assert result.status == STATUS_DOCUMENT_BUILD_FAILED
    assert provider.calls == []


# ── 5. Embedding failure (simulated via monkeypatch) ───────────────────────

def test_embedding_stage_failure_short_circuits_before_index_load(db_path, monkeypatch):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider()

    class _FakeEmbeddingResult:
        status = EMB_STATUS_BUILD_NOT_READY
        generated_count = 0
        reused_count = 0

    monkeypatch.setattr(
        retrieval_preparation_module, "generate_missing_embeddings",
        lambda *a, **k: _FakeEmbeddingResult(),
    )

    def _boom(*args, **kwargs):
        raise AssertionError("load_vector_index must not be called after an embedding-stage failure")

    monkeypatch.setattr(retrieval_preparation_module, "load_vector_index", _boom)

    result = prepare_source_for_retrieval(1, "alice", provider)

    assert result.status == STATUS_EMBEDDING_FAILED
    assert result.build_id is not None  # stage 1 DID succeed
    assert result.build_outcome == BUILD_OUTCOME_CREATED
    assert result.total_document_count == 1  # known from the successful build stage
    assert result.embeddings_generated == 0
    assert result.vector_index_document_count == 0
    assert EMB_STATUS_BUILD_NOT_READY in result.error


# ── 6. Vector-index failure (simulated via monkeypatch) ────────────────────

def test_vector_index_stage_failure_reported(db_path, monkeypatch):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider()

    class _FakeIndexResult:
        status = VI_STATUS_BUILD_NOT_READY
        document_count = 0
        error = "simulated race: build status changed after embedding completed"

    monkeypatch.setattr(
        retrieval_preparation_module, "load_vector_index",
        lambda *a, **k: _FakeIndexResult(),
    )

    result = prepare_source_for_retrieval(1, "alice", provider)

    assert result.status == STATUS_VECTOR_INDEX_FAILED
    assert result.embeddings_generated == 1  # stage 2 DID succeed
    assert result.vector_index_document_count == 0
    assert "simulated race" in result.error


# ── 7. Correct stage short-circuiting ──────────────────────────────────────

def test_document_build_failure_never_calls_later_stages(db_path, monkeypatch):
    _insert_source(db_path, 1)  # no snapshot -> stage 1 fails
    provider = FakeEmbeddingProvider()

    def _boom_embed(*args, **kwargs):
        raise AssertionError("generate_missing_embeddings must not be called after a document-build failure")

    def _boom_index(*args, **kwargs):
        raise AssertionError("load_vector_index must not be called after a document-build failure")

    monkeypatch.setattr(retrieval_preparation_module, "generate_missing_embeddings", _boom_embed)
    monkeypatch.setattr(retrieval_preparation_module, "load_vector_index", _boom_index)

    result = prepare_source_for_retrieval(1, "alice", provider)
    assert result.status == STATUS_DOCUMENT_BUILD_FAILED


# ── 8. Exact identity propagation ──────────────────────────────────────────

def test_identity_fields_propagate_exactly(db_path):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider(dimensions=6, provider_id="openai", model="text-embedding-3-small")

    result = prepare_source_for_retrieval(1, "alice", provider)

    assert result.source_id == 1
    assert result.embedding_provider == "openai"
    assert result.embedding_model == "text-embedding-3-small"
    assert result.embedding_dimensions == 6
    assert result.snapshot_id == 1
    assert result.snapshot_version == 1


# ── 9. Provider/model/dimension isolation ──────────────────────────────────

def test_different_providers_produce_isolated_results_for_same_build(db_path):
    _default_setup(db_path)
    provider_a = FakeEmbeddingProvider(dimensions=4, provider_id="openai", model="text-embedding-3-small")
    provider_b = FakeEmbeddingProvider(dimensions=6, provider_id="openai", model="text-embedding-3-large")

    result_a = prepare_source_for_retrieval(1, "alice", provider_a)
    result_b = prepare_source_for_retrieval(1, "alice", provider_b)

    assert result_a.build_id == result_b.build_id  # same underlying build...
    assert result_a.build_outcome == BUILD_OUTCOME_CREATED
    assert result_b.build_outcome == BUILD_OUTCOME_REUSED  # ...reused, since the schema didn't change
    assert result_a.embeddings_generated == 1  # ...but each identity embeds independently
    assert result_b.embeddings_generated == 1
    assert result_a.vector_index_document_count == 1
    assert result_b.vector_index_document_count == 1
    assert result_a.embedding_dimensions == 4
    assert result_b.embedding_dimensions == 6


# ── 10. Deterministic timing through an injected clock ────────────────────

def test_deterministic_timing_via_injected_clock(db_path):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider()
    clock = _make_clock([0.0, 0.0, 1.0, 1.0, 3.0, 3.0, 6.0, 6.0])

    result = prepare_source_for_retrieval(1, "alice", provider, clock=clock)

    assert result.document_build_duration_seconds == 1.0
    assert result.embedding_duration_seconds == 2.0
    assert result.index_load_duration_seconds == 3.0
    assert result.total_duration_seconds == 6.0


def test_deterministic_timing_on_document_build_failure(db_path):
    _insert_source(db_path, 1)  # no snapshot -> fails at stage 1
    provider = FakeEmbeddingProvider()
    clock = _make_clock([0.0, 0.0, 2.5, 2.5])  # total_start, stage1_start, stage1_end, total_end

    result = prepare_source_for_retrieval(1, "alice", provider, clock=clock)

    assert result.status == STATUS_DOCUMENT_BUILD_FAILED
    assert result.document_build_duration_seconds == 2.5
    assert result.embedding_duration_seconds == 0.0
    assert result.index_load_duration_seconds == 0.0
    assert result.total_duration_seconds == 2.5


# ── 11. Warnings propagation ────────────────────────────────────────────────

def test_warnings_propagate_from_document_build_stage(db_path, monkeypatch):
    _default_setup(db_path)
    provider = FakeEmbeddingProvider()

    real_build_and_persist = retrieval_preparation_module.build_and_persist_retrieval_documents

    def _with_warnings(*args, **kwargs):
        real_result = real_build_and_persist(*args, **kwargs)
        return dataclasses.replace(real_result, warnings=("a diagnostic warning from the loader",))

    monkeypatch.setattr(retrieval_preparation_module, "build_and_persist_retrieval_documents", _with_warnings)

    result = prepare_source_for_retrieval(1, "alice", provider)

    assert result.status == STATUS_OK
    assert result.warnings == ("a diagnostic warning from the loader",)


# ── 12. No credential leakage ───────────────────────────────────────────────

def test_result_carries_no_secret_fields():
    field_names = set(RetrievalPreparationResult.__dataclass_fields__.keys())
    assert not any(
        "key" in name.lower() or "secret" in name.lower() or "token" in name.lower() or "password" in name.lower()
        for name in field_names
    )


def test_result_never_touches_provider_private_attributes(db_path):
    _default_setup(db_path)

    class _SecretHoldingProvider(FakeEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self._api_key = "sk-should-never-appear-anywhere"

    provider = _SecretHoldingProvider()
    result = prepare_source_for_retrieval(1, "alice", provider)

    result_repr = repr(result)
    assert "sk-should-never-appear-anywhere" not in result_repr


# ── 13. Index-cache correctness across rebuilds ────────────────────────────

def test_cache_reused_across_repeated_calls_and_reloaded_after_schema_change(db_path, monkeypatch):
    unchanged_table = _table("dbo", "learners", [_col("id", "INTEGER", pk=True)])
    _default_setup(db_path, tables=[unchanged_table])
    provider = FakeEmbeddingProvider()

    first = prepare_source_for_retrieval(1, "alice", provider)
    assert first.vector_index_document_count == 1

    real_load_stored_retrieval_documents = vector_index_module.load_stored_retrieval_documents

    def _boom(*args, **kwargs):
        raise AssertionError("load_stored_retrieval_documents must not be called on a cache hit")

    monkeypatch.setattr(vector_index_module, "load_stored_retrieval_documents", _boom)

    second = prepare_source_for_retrieval(1, "alice", provider)
    assert second.status == STATUS_OK
    assert second.build_id == first.build_id
    assert second.vector_index_document_count == 1  # served from cache, not re-decoded

    # Restore (NOT monkeypatch.undo() — this test's `monkeypatch` instance is
    # shared with the db_path fixture's get_connection patches; undo() would
    # revert those too) before the schema-change branch below, which DOES
    # need a genuine reload.
    monkeypatch.setattr(vector_index_module, "load_stored_retrieval_documents", real_load_stored_retrieval_documents)

    new_table = _table("dbo", "tracks", [_col("id", "INTEGER", pk=True)])
    _insert_snapshot(db_path, snapshot_id=2, source_id=1, version=2, tables=[unchanged_table, new_table])

    third = prepare_source_for_retrieval(1, "alice", provider)
    assert third.build_id != first.build_id  # different identity key -> genuinely reloaded, not stale
    assert third.vector_index_document_count == 2


# ── 14. All existing SQL-service tests remain passing ──────────────────────
# (verified by running the full sqlservice suite together — see the
# Phase 2 Step 5 report; not re-implemented as its own test here.)
