"""
Focused unit tests — SQL rebuild Phase 2, Step 2: batched embedding
generation coordinator (core/sqlservice/embedding_coordinator.py) plus the
provider interface (core/sqlservice/embedding_providers.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server. Every test injects a FakeEmbeddingProvider
defined in this file — never OpenAIEmbeddingProvider against a real network,
never a real OpenAI API call anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_embedding_coordinator.py -v
"""

import hashlib
import math
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import core.sqlservice.embedding_coordinator as coordinator_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
from core.sqlservice.embedding_coordinator import (
    EmbeddingGenerationResult,
    EmbeddingPersistenceError,
    EmbeddingValidationError,
    generate_missing_embeddings,
)
from core.sqlservice.embedding_providers import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderError,
    OpenAIEmbeddingProvider,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.embedding_store import (
    STATUS_BUILD_NOT_FOUND,
    STATUS_BUILD_NOT_READY,
    STATUS_OK,
    STATUS_UNOWNED,
    get_embeddings_for_build,
)
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build


# ── Fixture plumbing (mirrors tests/test_sqlservice_embedding_store.py) ──

def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _patch_connections(monkeypatch, db_path):
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(db_path))


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
    _patch_connections(monkeypatch, path)
    return path


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


def _insert_snapshot(db_path, snapshot_id, source_id, version=1):
    conn = sqlite3.connect(db_path)
    now = "2026-09-04T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id=1, object_type="TABLE", tier=1, text=None, metadata=None):
    if text is None:
        text = f"OBJECT: {object_fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type=object_type,
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash,
        metadata=MappingProxyType(metadata if metadata is not None else {"column_count": 1}),
    )


def _batch(source_id, snapshot_id, snapshot_version, documents, status=STATUS_READY):
    return RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=snapshot_version,
        status=status, documents=tuple(documents), counts=MappingProxyType({}), warnings=(),
    )


def _fingerprint(*, version=1, fingerprint_hash=None, table_set_hash=None):
    if fingerprint_hash is None:
        fingerprint_hash = hashlib.sha256(b"fingerprint-a").hexdigest()
    if table_set_hash is None:
        table_set_hash = hashlib.sha256(b"table-set-a").hexdigest()
    return {
        "fingerprint_version": version,
        "fingerprint_hash": fingerprint_hash,
        "table_set_hash": table_set_hash,
    }


def _make_ready_build_with_docs(db_path, docs, source_id=1, snapshot_id=1, user_id="alice"):
    _insert_source(db_path, source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id, source_id=source_id)
    batch = _batch(source_id, snapshot_id, 1, docs)
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    return result.build


def _make_ready_build(db_path, source_id=1, snapshot_id=1, object_fqns=("dbo.learners", "dbo.tracks"), user_id="alice"):
    docs = [_doc(fqn, source_id=source_id) for fqn in object_fqns]
    return _make_ready_build_with_docs(db_path, docs, source_id=source_id, snapshot_id=snapshot_id, user_id=user_id)


DIMS = 4


def _default_vector_for_text(text, dims=DIMS):
    seed = sum(ord(c) for c in text) % 97
    return tuple(float(seed + j) for j in range(dims))


# ── Fake provider (no network, no OpenAI SDK) ──────────────────────────────


class FakeEmbeddingProvider(EmbeddingProvider):
    """
    Records every embed_batch() call (in `self.calls`, a list of the exact
    text lists it was asked to embed) and, by default, returns a
    deterministic per-text vector in the same order it was asked — so
    ordering/dedup assertions can inspect `self.calls` directly, and
    reused-content assertions can recompute the expected vector from the
    text alone. Pass `responder` to override the response (or raise) for
    every test that isn't exercising the happy path.
    """

    def __init__(self, dimensions=DIMS, provider_id="fake", model="fake-model", responder=None):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
        self._responder = responder
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
        if self._responder is not None:
            return self._responder(texts)
        items = tuple(
            ProviderEmbeddingItem(index=i, vector=_default_vector_for_text(text, self._dimensions))
            for i, text in enumerate(texts)
        )
        return ProviderEmbeddingBatchResponse(items=items, model=self._model)


# ── 1. All embeddings already cached: zero provider calls ─────────────────

def test_all_cached_makes_zero_provider_calls(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    provider = FakeEmbeddingProvider()

    first = generate_missing_embeddings(build.id, 1, "alice", provider)
    assert first.status == STATUS_OK
    assert first.generated_count == 2
    assert len(provider.calls) == 1  # one batch for the first, real generation

    provider.calls.clear()
    second = generate_missing_embeddings(build.id, 1, "alice", provider)
    assert second.status == STATUS_OK
    assert second.requested_count == 0
    assert second.generated_count == 0
    assert second.reused_count == 2
    assert provider.calls == []  # zero provider calls once everything is cached


# ── 2. Partial cache: only missing documents requested ─────────────────────

def test_partial_cache_requests_only_missing_documents(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks", "dbo.grades"))
    learners_text = _doc("dbo.learners").document_text

    warm_provider = FakeEmbeddingProvider()
    # Pre-warm the cache for exactly one document by generating once, then
    # only ask about the other two next time with a fresh provider.
    generate_missing_embeddings(build.id, 1, "alice", warm_provider)

    # Simulate "already cached" state changing by manually clearing two of
    # the three cached rows so exactly one (learners) remains cached.
    conn = sqlite3.connect(db_path)
    learners_hash = _doc("dbo.learners").document_hash
    conn.execute("DELETE FROM sql_retrieval_embeddings WHERE document_hash != ?", (learners_hash,))
    conn.commit()
    conn.close()

    provider = FakeEmbeddingProvider()
    result = generate_missing_embeddings(build.id, 1, "alice", provider)

    assert result.total_documents == 3
    assert result.requested_count == 2
    assert result.reused_count == 1
    assert len(provider.calls) == 1
    requested_texts = set(provider.calls[0])
    assert requested_texts == {_doc("dbo.tracks").document_text, _doc("dbo.grades").document_text}
    assert learners_text not in requested_texts


# ── 3. Deterministic batching and ordering ─────────────────────────────────

def test_batching_is_deterministic_and_follows_object_fqn_order(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.c_table", "dbo.a_table", "dbo.b_table"))
    provider = FakeEmbeddingProvider()

    generate_missing_embeddings(build.id, 1, "alice", provider, max_batch_items=2)

    assert len(provider.calls) == 2
    expected_order = [
        _doc("dbo.a_table").document_text,
        _doc("dbo.b_table").document_text,
        _doc("dbo.c_table").document_text,
    ]
    flattened = [text for call in provider.calls for text in call]
    assert flattened == expected_order  # object_fqn order, not insertion order


# ── 4. Duplicate document-hash deduplication ───────────────────────────────

def test_duplicate_document_hash_sent_once(db_path):
    shared_text = "OBJECT: dbo.shared\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
    docs = [
        _doc("dbo.alpha", text=shared_text),
        _doc("dbo.beta", text=shared_text),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = generate_missing_embeddings(build.id, 1, "alice", provider)

    # total_documents counts DISTINCT document_hash values (established in
    # Phase 2 Step 1's embedding_store) — two objects sharing identical
    # content collapse to one, which is exactly the point of this test.
    assert result.total_documents == 1
    assert result.requested_count == 1
    assert len(provider.calls) == 1
    assert provider.calls[0] == [shared_text]  # sent exactly once, not twice

    fetched = get_embeddings_for_build(build.id, 1, "alice", "fake", "fake-model", DIMS)
    assert {e.object_fqn for e in fetched} == {"dbo.alpha", "dbo.beta"}
    assert fetched[0].vector == fetched[1].vector  # both share the one generated vector


# ── 5. Multiple batches ─────────────────────────────────────────────────────

def test_multiple_batches_cover_every_document(db_path):
    fqns = tuple(f"dbo.table_{i:02d}" for i in range(7))
    build = _make_ready_build(db_path, object_fqns=fqns)
    provider = FakeEmbeddingProvider()

    result = generate_missing_embeddings(build.id, 1, "alice", provider, max_batch_items=3)

    assert len(provider.calls) == 3  # 3 + 3 + 1
    assert [len(c) for c in provider.calls] == [3, 3, 1]
    assert result.generated_count == 7
    assert sum(len(c) for c in provider.calls) == 7


# ── 6. Correct response-index reordering ───────────────────────────────────

def test_response_index_reordering(db_path):
    # Deliberately different-length texts, so a mis-mapped index would
    # produce a detectably wrong vector rather than one that happens to
    # look right by coincidence.
    docs = [
        _doc("dbo.aaa", text="short"),
        _doc("dbo.bbb", text="a medium length document"),
        _doc("dbo.ccc", text="a considerably longer document than the other two by far"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)

    def shuffled_responder(texts):
        # Deliberately return items in reverse index order, with a vector
        # that encodes which text (by length) it was actually asked to embed.
        items = tuple(
            ProviderEmbeddingItem(index=i, vector=tuple([float(len(texts[i]))] * DIMS))
            for i in reversed(range(len(texts)))
        )
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=shuffled_responder)
    generate_missing_embeddings(build.id, 1, "alice", provider)

    fetched = {e.object_fqn: e.vector for e in get_embeddings_for_build(build.id, 1, "alice", "fake", "fake-model", DIMS)}
    for doc in docs:
        expected_len = float(len(doc.document_text))
        assert fetched[doc.object_fqn] == (expected_len,) * DIMS


def _assert_nothing_persisted(db_path):
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_embeddings").fetchone()[0]
    conn.close()
    assert count == 0


# ── 7. Count mismatch ───────────────────────────────────────────────────────

def test_response_count_mismatch_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))

    def too_few(texts):
        items = (ProviderEmbeddingItem(index=0, vector=(1.0,) * DIMS),)  # only 1, expected 2
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=too_few)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


# ── 8. Duplicate / missing / out-of-range indices ──────────────────────────

def test_duplicate_index_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))

    def duplicate_index(texts):
        items = (
            ProviderEmbeddingItem(index=0, vector=(1.0,) * DIMS),
            ProviderEmbeddingItem(index=0, vector=(2.0,) * DIMS),  # duplicate, not 1
        )
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=duplicate_index)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


def test_missing_index_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks", "dbo.grades"))

    def missing_index(texts):
        # 3 items but indices 0 and 2 only — index 1 is missing.
        items = (
            ProviderEmbeddingItem(index=0, vector=(1.0,) * DIMS),
            ProviderEmbeddingItem(index=2, vector=(2.0,) * DIMS),
            ProviderEmbeddingItem(index=2, vector=(3.0,) * DIMS),
        )
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=missing_index)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


def test_out_of_range_index_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))

    def out_of_range(texts):
        items = (
            ProviderEmbeddingItem(index=0, vector=(1.0,) * DIMS),
            ProviderEmbeddingItem(index=5, vector=(2.0,) * DIMS),  # out of range for a batch of 2
        )
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=out_of_range)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


# ── 9. Incorrect dimensions ─────────────────────────────────────────────────

def test_wrong_dimension_count_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def wrong_dims(texts):
        items = (ProviderEmbeddingItem(index=0, vector=(1.0, 2.0)),)  # 2, expected DIMS=4
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=wrong_dims)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


# ── 10. NaN / infinite vectors ───────────────────────────────────────────────

def test_nan_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def nan_vector(texts):
        items = (ProviderEmbeddingItem(index=0, vector=(1.0, float("nan"), 3.0, 4.0)),)
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=nan_vector)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


def test_infinite_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def inf_vector(texts):
        items = (ProviderEmbeddingItem(index=0, vector=(1.0, float("inf"), 3.0, 4.0)),)
        return ProviderEmbeddingBatchResponse(items=items, model="fake-model")

    provider = FakeEmbeddingProvider(responder=inf_vector)
    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


# ── 11. Oversized single document ──────────────────────────────────────────

def test_oversized_document_rejected_before_any_provider_call(db_path):
    huge_text = "x" * 1000
    docs = [_doc("dbo.small", text="short text"), _doc("dbo.huge", text=huge_text)]
    build = _make_ready_build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    with pytest.raises(EmbeddingValidationError):
        generate_missing_embeddings(build.id, 1, "alice", provider, max_batch_chars=500)

    assert provider.calls == []  # rejected during planning, before any embed_batch() call
    _assert_nothing_persisted(db_path)


# ── 12. Provider exception ──────────────────────────────────────────────────

def test_provider_error_propagates_unchanged(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def raises_provider_error(texts):
        raise EmbeddingProviderError("simulated network failure")

    provider = FakeEmbeddingProvider(responder=raises_provider_error)
    with pytest.raises(EmbeddingProviderError, match="simulated network failure"):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


def test_unexpected_provider_exception_wrapped_as_provider_error(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def raises_generic(texts):
        raise RuntimeError("some SDK internals blew up")

    provider = FakeEmbeddingProvider(responder=raises_generic)
    with pytest.raises(EmbeddingProviderError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


# ── 13. Configuration failure ────────────────────────────────────────────────

def test_configuration_error_from_provider_propagates_unchanged(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def raises_configuration_error(texts):
        raise EmbeddingConfigurationError("OPENAI_API_KEY is not configured.")

    provider = FakeEmbeddingProvider(responder=raises_configuration_error)
    with pytest.raises(EmbeddingConfigurationError):
        generate_missing_embeddings(build.id, 1, "alice", provider)
    _assert_nothing_persisted(db_path)


def test_invalid_batch_size_configuration_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    provider = FakeEmbeddingProvider()

    with pytest.raises(EmbeddingConfigurationError):
        generate_missing_embeddings(build.id, 1, "alice", provider, max_batch_items=0)
    with pytest.raises(EmbeddingConfigurationError):
        generate_missing_embeddings(build.id, 1, "alice", provider, max_batch_chars=-1)
    assert provider.calls == []  # rejected before any DB read or provider call


# ── 14. Ownership / non-READY build failures ────────────────────────────────

def test_non_ready_build_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sql_retrieval_builds "
        "(id, source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at) "
        "VALUES (1, 1, 1, 1, 1, ?, ?, ?, 'BUILDING', 0, '2026-09-04T00:00:00Z')",
        (hashlib.sha256(b"x").hexdigest(), hashlib.sha256(b"y").hexdigest(), hashlib.sha256(b"z").hexdigest()),
    )
    conn.commit()
    conn.close()

    provider = FakeEmbeddingProvider()
    result = generate_missing_embeddings(1, 1, "alice", provider)
    assert result.status == STATUS_BUILD_NOT_READY
    assert provider.calls == []


def test_unowned_source_rejected(db_path):
    build = _make_ready_build(db_path, user_id="alice")
    provider = FakeEmbeddingProvider()

    result = generate_missing_embeddings(build.id, 1, "mallory", provider)
    assert result.status == STATUS_UNOWNED
    assert provider.calls == []


def test_build_not_found_for_source_rejected(db_path):
    build = _make_ready_build(db_path, source_id=1, snapshot_id=1)
    _insert_source(db_path, 2)

    provider = FakeEmbeddingProvider()
    result = generate_missing_embeddings(build.id, 2, "alice", provider)
    assert result.status == STATUS_BUILD_NOT_FOUND
    assert provider.calls == []


# ── 15. Persistence failure ─────────────────────────────────────────────────

def test_persistence_failure_raises_persistence_error(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))

    def failing_insert_embeddings(*args, **kwargs):
        raise sqlite3.OperationalError("injected failure for test_persistence_failure")

    monkeypatch.setattr(coordinator_module, "insert_embeddings", failing_insert_embeddings)

    provider = FakeEmbeddingProvider()
    with pytest.raises(EmbeddingPersistenceError):
        generate_missing_embeddings(build.id, 1, "alice", provider)


# ── 16. No partial insert after a later batch fails ────────────────────────

def test_no_partial_insert_when_a_later_batch_fails(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    call_count = {"n": 0}

    def first_batch_ok_second_batch_broken(texts):
        call_count["n"] += 1
        if call_count["n"] == 1:
            items = tuple(
                ProviderEmbeddingItem(index=i, vector=(1.0,) * DIMS) for i in range(len(texts))
            )
            return ProviderEmbeddingBatchResponse(items=items, model="fake-model")
        raise EmbeddingProviderError("second batch failed")

    provider = FakeEmbeddingProvider(responder=first_batch_ok_second_batch_broken)
    with pytest.raises(EmbeddingProviderError):
        # max_batch_items=1 forces two separate batches — one per document.
        generate_missing_embeddings(build.id, 1, "alice", provider, max_batch_items=1)

    assert call_count["n"] == 2
    _assert_nothing_persisted(db_path)  # the first (successful) batch was never written either


# ── 17. Successful generation then subsequent full reuse ──────────────────

def test_full_generation_then_full_reuse(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks", "dbo.grades"))
    provider = FakeEmbeddingProvider()

    first = generate_missing_embeddings(build.id, 1, "alice", provider)
    assert first.status == STATUS_OK
    assert first.generated_count == 3
    assert first.reused_count == 0
    assert len(provider.calls) == 1

    provider.calls.clear()
    second = generate_missing_embeddings(build.id, 1, "alice", provider)
    assert second.status == STATUS_OK
    assert second.generated_count == 0
    assert second.reused_count == 3
    assert second.requested_count == 0
    assert provider.calls == []


# ── 18. API keys absent from results/errors/logs ───────────────────────────

def test_generation_result_carries_no_secret_fields():
    field_names = set(EmbeddingGenerationResult.__dataclass_fields__.keys())
    assert not any("key" in name.lower() or "secret" in name.lower() or "token" in name.lower() for name in field_names)


def test_openai_provider_never_leaks_api_key_in_configuration_error():
    provider = OpenAIEmbeddingProvider(api_key="", model="text-embedding-3-small", dimensions=DIMS)
    with pytest.raises(EmbeddingConfigurationError) as excinfo:
        provider.embed_batch(["some text"])
    assert "OPENAI_API_KEY" in str(excinfo.value)  # a helpful hint, not the key itself (there is none here)


def test_openai_provider_wraps_sdk_exception_without_leaking_key(monkeypatch):
    secret_key = "sk-super-secret-value-should-never-appear-anywhere"
    provider = OpenAIEmbeddingProvider(api_key=secret_key, model="text-embedding-3-small", dimensions=DIMS)

    import core.sqlservice.embedding_providers as providers_module

    class _FakeClient:
        class embeddings:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("connection reset by peer")

    class _FakeOpenAIModule:
        @staticmethod
        def OpenAI(**kwargs):
            # Confirm the real SDK constructor call site is the only place
            # the key is ever read, and that this test's fake never echoes
            # it back into any exception message.
            assert kwargs.get("api_key") == secret_key
            return _FakeClient()

    monkeypatch.setattr(providers_module, "_openai", _FakeOpenAIModule())
    monkeypatch.setattr(providers_module, "_OPENAI_AVAILABLE", True)

    with pytest.raises(EmbeddingProviderError) as excinfo:
        provider.embed_batch(["some text"])
    assert secret_key not in str(excinfo.value)
    assert secret_key not in repr(excinfo.value)


# ── 19. All existing Phase 1 / Phase 2 Step 1 tests remain passing ────────
# (exercised by running the full sqlservice test suite together — see the
# Phase 2 Step 2 report; not re-implemented as its own test here since it
# would just re-run those files' own test functions verbatim.)
