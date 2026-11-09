"""
Focused unit tests — SQL rebuild Phase 2, Step 4: semantic schema retrieval
for a natural-language question (core/sqlservice/schema_context_retrieval.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server. Every test injects a FakeEmbeddingProvider
defined in this file — never a real OpenAI call anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_schema_context_retrieval.py -v
"""

import dataclasses
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
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.vector_index as vector_index_module
from core.sqlservice.embedding_providers import (
    EmbeddingProvider,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_context_retrieval import (
    RANKING_MODE_HYBRID,
    RANKING_MODE_VECTOR,
    SELECTION_REASON_DIRECT_RELATIONSHIP,
    SELECTION_REASON_HYBRID_ANCHOR,
    SELECTION_REASON_RELATIONSHIP,
    SELECTION_REASON_RELATIONSHIP_BRIDGE,
    SELECTION_REASON_SEMANTIC,
    STATUS_BUILD_NOT_FOUND,
    STATUS_BUILD_NOT_READY,
    STATUS_INCOMPLETE,
    STATUS_OK,
    STATUS_UNOWNED,
    SchemaRetrievalValidationError,
    retrieve_schema_context,
)
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.vector_index import clear_vector_index_cache


# ── Fixture plumbing ─────────────────────────────────────────────────────

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


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    """The vector-index cache (used internally by load_vector_index) is a
    module-level global reused across every test's small build_id
    numbering — must be cleared before/after each test (see the identical
    fixture in tests/test_sqlservice_vector_index.py)."""
    clear_vector_index_cache()
    clear_lexical_index_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()


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


PROVIDER = "fake"
MODEL = "fake-model"
DIMS = 4


def _embed_all(db_path, build, vectors_by_fqn, *, docs=None, provider=PROVIDER, model=MODEL, dims=DIMS, source_id=1, user_id="alice"):
    if docs is not None:
        hash_by_fqn = {d.object_fqn: d.document_hash for d in docs}
    else:
        hash_by_fqn = {fqn: _doc(fqn).document_hash for fqn in vectors_by_fqn}
    vectors_by_hash = {hash_by_fqn[fqn]: vec for fqn, vec in vectors_by_fqn.items()}
    result = insert_embeddings(build.id, source_id, user_id, provider, model, dims, vectors_by_hash)
    assert result.status == EMBEDDING_STATUS_OK
    return result


def _unit(*components) -> tuple:
    norm = math.sqrt(sum(c * c for c in components))
    return tuple(c / norm for c in components)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Records every embed_batch() call. Defaults to a fixed vector for the
    question; pass `responder` to simulate a malformed/broken response."""

    def __init__(self, dimensions=DIMS, provider_id=PROVIDER, model=MODEL, responder=None, fixed_vector=None):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
        self._responder = responder
        self._fixed_vector = fixed_vector if fixed_vector is not None else (1.0,) + (0.0,) * (dimensions - 1)
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
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=tuple(self._fixed_vector)),),
            model=self._model,
        )


# ── 1/2. Successful single-question retrieval; exactly one provider call ──

def test_successful_retrieval_and_single_provider_call(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    _embed_all(db_path, build, {
        "dbo.learners": [1.0, 0.0, 0.0, 0.0],
        "dbo.tracks": [0.0, 1.0, 0.0, 0.0],
    })
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("how many learners are there", build.id, 1, "alice", provider)

    assert result.status == STATUS_OK
    assert len(provider.calls) == 1
    assert provider.calls[0] == ["how many learners are there"]
    assert [d.object_fqn for d in result.documents][0] == "dbo.learners"
    assert result.documents[0].selection_reason == SELECTION_REASON_HYBRID_ANCHOR
    assert result.documents[0].score is not None
    assert result.total_characters == len(result.context_text)
    assert result.build_id == build.id
    assert result.embedding_provider == PROVIDER
    assert result.embedding_model == MODEL
    assert result.embedding_dimensions == DIMS
    assert result.schema_fingerprint is not None


# ── 3. Invalid / blank / oversized question ────────────────────────────────

def test_blank_question_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        retrieve_schema_context("   ", build.id, 1, "alice", provider)
    assert provider.calls == []


def test_empty_question_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        retrieve_schema_context("", build.id, 1, "alice", provider)
    assert provider.calls == []


def test_oversized_question_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        retrieve_schema_context("x" * 5000, build.id, 1, "alice", provider)
    assert provider.calls == []


# ── 4/5. Malformed provider responses / wrong dimensions / wrong model ────

def test_provider_response_count_mismatch_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})

    def two_items(texts):
        items = (
            ProviderEmbeddingItem(index=0, vector=(1.0, 0.0, 0.0, 0.0)),
            ProviderEmbeddingItem(index=1, vector=(0.0, 1.0, 0.0, 0.0)),
        )
        return ProviderEmbeddingBatchResponse(items=items, model=MODEL)

    provider = FakeEmbeddingProvider(responder=two_items)
    with pytest.raises(SchemaRetrievalValidationError):
        retrieve_schema_context("question", build.id, 1, "alice", provider)


def test_provider_response_wrong_index_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})

    def wrong_index(texts):
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=1, vector=(1.0, 0.0, 0.0, 0.0)),), model=MODEL,
        )

    provider = FakeEmbeddingProvider(responder=wrong_index)
    with pytest.raises(SchemaRetrievalValidationError):
        retrieve_schema_context("question", build.id, 1, "alice", provider)


def test_provider_response_model_mismatch_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})

    def wrong_model(texts):
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=(1.0, 0.0, 0.0, 0.0)),),
            model="some-other-model",
        )

    provider = FakeEmbeddingProvider(responder=wrong_model)
    with pytest.raises(SchemaRetrievalValidationError):
        retrieve_schema_context("question", build.id, 1, "alice", provider)


def test_provider_response_wrong_dimensions_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})

    def wrong_dims(texts):
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=(1.0, 0.0)),), model=MODEL,
        )

    provider = FakeEmbeddingProvider(responder=wrong_dims)
    with pytest.raises(SchemaRetrievalValidationError):
        retrieve_schema_context("question", build.id, 1, "alice", provider)


def test_provider_response_nan_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})

    def nan_vector(texts):
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=(1.0, float("nan"), 0.0, 0.0)),), model=MODEL,
        )

    provider = FakeEmbeddingProvider(responder=nan_vector)
    with pytest.raises(SchemaRetrievalValidationError):
        retrieve_schema_context("question", build.id, 1, "alice", provider)


def test_provider_response_zero_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})

    def zero_vector(texts):
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=(0.0, 0.0, 0.0, 0.0)),), model=MODEL,
        )

    provider = FakeEmbeddingProvider(responder=zero_vector)
    with pytest.raises(SchemaRetrievalValidationError):
        retrieve_schema_context("question", build.id, 1, "alice", provider)


# ── 6/7. Tier 3/4 excluded by default; explicitly enabled ──────────────────

def test_tier_3_and_4_excluded_by_default(db_path):
    docs = [
        _doc("dbo.tier1_far", tier=1, text="OBJECT: dbo.tier1_far\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  x INTEGER"),
        _doc("dbo.tier3_close", tier=3, text="OBJECT: dbo.tier3_close\nTYPE: TABLE\nTIER: 3\nCOLUMNS:\n  x INTEGER"),
        _doc("dbo.tier4_close", tier=4, text="OBJECT: dbo.tier4_close\nTYPE: TABLE\nTIER: 4\nCOLUMNS:\n  x INTEGER"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.tier1_far": [0.0, 1.0, 0.0, 0.0],   # orthogonal to query
        "dbo.tier3_close": [1.0, 0.0, 0.0, 0.0],  # best match, but Tier 3
        "dbo.tier4_close": [0.9, 0.1, 0.0, 0.0],  # second-best match, but Tier 4
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider)

    assert {d.object_fqn for d in result.documents} == {"dbo.tier1_far"}


def test_tier_3_and_4_included_when_explicitly_allowed(db_path):
    docs = [
        _doc("dbo.tier1_far", tier=1, text="OBJECT: dbo.tier1_far\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  x INTEGER"),
        _doc("dbo.tier3_close", tier=3, text="OBJECT: dbo.tier3_close\nTYPE: TABLE\nTIER: 3\nCOLUMNS:\n  x INTEGER"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.tier1_far": [0.0, 1.0, 0.0, 0.0],
        "dbo.tier3_close": [1.0, 0.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, allowed_tiers=frozenset({1, 2, 3, 4}),
    )

    assert [d.object_fqn for d in result.documents][0] == "dbo.tier3_close"


def test_invalid_allowed_tiers_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        retrieve_schema_context("question", build.id, 1, "alice", provider, allowed_tiers=frozenset())
    with pytest.raises(ValueError):
        retrieve_schema_context("question", build.id, 1, "alice", provider, allowed_tiers=frozenset({7}))


# ── 8. Deterministic semantic ranking ──────────────────────────────────────

def test_deterministic_semantic_ranking(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b", "dbo.c"))
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.0, 1.0, 0.0, 0.0],
        "dbo.c": [1.0, 1.0, 0.0, 0.0],
    })
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider)

    assert [d.object_fqn for d in result.documents] == ["dbo.a", "dbo.c", "dbo.b"]
    assert math.isclose(result.documents[0].score, 1.0, abs_tol=1e-9)
    assert math.isclose(result.documents[1].score, math.sqrt(2) / 2, abs_tol=1e-9)
    assert math.isclose(result.documents[2].score, 0.0, abs_tol=1e-9)


# ── 9/10/11/12/13. Relationship expansion behavior ─────────────────────────

def _rel_doc(object_fqn, *, tier=1, targets=(), sources=()):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    metadata = {}
    if targets:
        metadata["confirmed_relationship_targets"] = tuple(targets)
    if sources:
        metadata["confirmed_relationship_sources"] = tuple(sources)
    return _doc(object_fqn, tier=tier, text=text, metadata=metadata)


def test_one_hop_confirmed_relationship_expansion(db_path):
    docs = [
        _rel_doc("dbo.enrollments", targets=("dbo.students.id",)),
        _rel_doc("dbo.students"),
        _rel_doc("dbo.unrelated"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.enrollments": [1.0, 0.0, 0.0, 0.0],  # the semantic match
        "dbo.students": [0.0, 1.0, 0.0, 0.0],     # orthogonal — only found via relationship
        "dbo.unrelated": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1)

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert by_fqn["dbo.enrollments"].selection_reason == SELECTION_REASON_HYBRID_ANCHOR
    assert by_fqn["dbo.students"].selection_reason == SELECTION_REASON_DIRECT_RELATIONSHIP
    assert by_fqn["dbo.students"].score is None
    assert "dbo.unrelated" not in by_fqn


def test_no_recursive_expansion_beyond_one_hop(db_path):
    docs = [
        _rel_doc("dbo.a", targets=("dbo.b.id",)),
        _rel_doc("dbo.b", targets=("dbo.c.id",)),  # b -> c, must NOT be followed
        _rel_doc("dbo.c"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.0, 1.0, 0.0, 0.0],
        "dbo.c": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1)

    fqns = {d.object_fqn for d in result.documents}
    assert fqns == {"dbo.a", "dbo.b"}
    assert "dbo.c" not in fqns


def test_case_insensitive_fqn_matching_for_expansion(db_path):
    docs = [
        _rel_doc("dbo.enrollments", targets=("DBO.STUDENTS.ID",)),  # deliberately mismatched case
        _rel_doc("dbo.students"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.enrollments": [1.0, 0.0, 0.0, 0.0],
        "dbo.students": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1)

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.students" in fqns


def test_relationship_expansion_deduplicates(db_path):
    docs = [
        _rel_doc("dbo.a", targets=("dbo.shared.id",)),
        _rel_doc("dbo.b", targets=("dbo.shared.id",)),
        _rel_doc("dbo.shared"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.9, 0.1, 0.0, 0.0],
        "dbo.shared": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5)

    shared_hits = [d for d in result.documents if d.object_fqn == "dbo.shared"]
    assert len(shared_hits) == 1


def test_relationship_expansion_never_duplicates_an_existing_semantic_match(db_path):
    docs = [
        _rel_doc("dbo.a", targets=("dbo.b.id",)),
        _rel_doc("dbo.b"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.9, 0.1, 0.0, 0.0],  # ranks highly enough to be a semantic match too
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=5)

    b_hits = [d for d in result.documents if d.object_fqn == "dbo.b"]
    assert len(b_hits) == 1
    assert b_hits[0].selection_reason == SELECTION_REASON_HYBRID_ANCHOR  # anchor wins, not re-added as relationship


def test_relationship_limit_enforced(db_path):
    docs = [
        _rel_doc("dbo.a", targets=("dbo.b.id", "dbo.c.id", "dbo.d.id")),
        _rel_doc("dbo.b"),
        _rel_doc("dbo.c"),
        _rel_doc("dbo.d"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.0, 1.0, 0.0, 0.0],
        "dbo.c": [0.0, 0.0, 1.0, 0.0],
        "dbo.d": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, relationship_limit=2)

    relationship_fqns = {d.object_fqn for d in result.documents if d.selection_reason == SELECTION_REASON_DIRECT_RELATIONSHIP}
    assert relationship_fqns == {"dbo.b", "dbo.c"}  # first 2 in sorted target order, deterministic


# ── 14/15/16. Context-budget enforcement ───────────────────────────────────

def test_exact_context_budget_enforcement(db_path):
    text_a = "OBJECT: dbo.a\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"  # ~58 chars
    text_b = "OBJECT: dbo.b\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
    docs = [_doc("dbo.a", text=text_a), _doc("dbo.b", text=text_b)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.9, 0.1, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    budget = len(text_a)  # fits exactly one document, not both (+ separator)
    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=2, max_context_characters=budget)

    assert [d.object_fqn for d in result.documents] == ["dbo.a"]
    assert result.total_characters == len(text_a)
    assert result.truncated is True
    assert len(result.warnings) == 1


def test_oversized_document_skipped_without_splitting(db_path):
    small_text = "OBJECT: dbo.small\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
    huge_text = "OBJECT: dbo.huge\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + ("x" * 2000)
    docs = [_doc("dbo.huge", text=huge_text), _doc("dbo.small", text=small_text)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.huge": [1.0, 0.0, 0.0, 0.0],   # best match, but too big for the budget
        "dbo.small": [0.9, 0.1, 0.0, 0.0],  # second-best, fits fine
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, max_context_characters=500,
    )

    fqns = [d.object_fqn for d in result.documents]
    assert fqns == ["dbo.small"]  # huge skipped, small still included (not a greedy stop)
    assert result.documents[0].document_text == small_text  # never truncated/split
    assert result.truncated is True


def test_budget_too_small_for_any_document_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    with pytest.raises(ValueError):
        retrieve_schema_context("question", build.id, 1, "alice", provider, max_context_characters=1)


def test_deterministic_truncation_and_warnings_across_repeated_calls(db_path):
    docs = [_doc(f"dbo.t{i}") for i in range(5)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {f"dbo.t{i}": [1.0 - i * 0.1, i * 0.1, 0.0, 0.0] for i in range(5)}, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    first = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=5, max_context_characters=150)
    second = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=5, max_context_characters=150)

    assert first.documents == second.documents
    assert first.warnings == second.warnings
    assert first.truncated == second.truncated


# ── 17. Immutable results and nested metadata ──────────────────────────────

def test_result_and_documents_are_immutable(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "CHANGED"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.documents[0].object_fqn = "changed"
    with pytest.raises(TypeError):
        result.documents[0].metadata["new_key"] = "nope"


# ── 18. Build/provider identity isolation ──────────────────────────────────

def test_identity_fields_match_request_and_never_cross_contaminate(db_path):
    build_a = _make_ready_build(db_path, source_id=1, snapshot_id=1, object_fqns=("dbo.a",))
    build_b = _make_ready_build(db_path, source_id=2, snapshot_id=2, object_fqns=("dbo.b",))
    _embed_all(db_path, build_a, {"dbo.a": [1.0, 0.0, 0.0, 0.0]}, source_id=1)
    _embed_all(db_path, build_b, {"dbo.b": [1.0, 0.0, 0.0, 0.0]}, source_id=2)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result_a = retrieve_schema_context("question", build_a.id, 1, "alice", provider)
    result_b = retrieve_schema_context("question", build_b.id, 2, "alice", provider)

    assert result_a.build_id == build_a.id
    assert result_b.build_id == build_b.id
    assert {d.object_fqn for d in result_a.documents} == {"dbo.a"}
    assert {d.object_fqn for d in result_b.documents} == {"dbo.b"}
    assert result_a.schema_fingerprint is not None
    assert result_b.schema_fingerprint is not None


# ── 19. Vector-index loading failure propagation ───────────────────────────

def test_unowned_source_propagates_status(db_path):
    build = _make_ready_build(db_path, user_id="alice")
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.tracks": [0.0, 1.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context("question", build.id, 1, "mallory", provider)
    assert result.status == STATUS_UNOWNED
    assert result.documents == ()
    assert provider.calls == []


def test_build_not_found_propagates_status(db_path):
    build = _make_ready_build(db_path, source_id=1, snapshot_id=1)
    _insert_source(db_path, 2)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context("question", build.id, 2, "alice", provider)
    assert result.status == STATUS_BUILD_NOT_FOUND
    assert provider.calls == []


def test_non_ready_build_propagates_status(db_path):
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

    result = retrieve_schema_context("question", 1, 1, "alice", provider)
    assert result.status == STATUS_BUILD_NOT_READY
    assert provider.calls == []


def test_incomplete_embeddings_propagates_status(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})  # tracks missing
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context("question", build.id, 1, "alice", provider)
    assert result.status == STATUS_INCOMPLETE
    assert provider.calls == []


# ── 20. Repeated retrieval uses the cached index safely ────────────────────

def test_repeated_retrieval_uses_cached_index(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    first = retrieve_schema_context("question one", build.id, 1, "alice", provider)
    assert first.status == STATUS_OK

    def _boom(*args, **kwargs):
        raise AssertionError("load_stored_retrieval_documents must not be called on a cache hit")

    monkeypatch.setattr(vector_index_module, "load_stored_retrieval_documents", _boom)

    second = retrieve_schema_context("question two", build.id, 1, "alice", provider)
    assert second.status == STATUS_OK
    assert len(provider.calls) == 2  # each question is still embedded once each
    assert [d.object_fqn for d in second.documents] == [d.object_fqn for d in first.documents]


# ── Step 4A regression tests ─────────────────────────────────────────────
#
# These lock in behavior already verified present in retrieve_schema_context():
# allowed_tiers is applied to the COMPLETE ranked result (search_vector_index
# is called with top_k=len(index.documents), never the caller's top_k) before
# the semantic top_k cutoff — see the `ranked = search_vector_index(...)` /
# `tier_filtered = [...]` / `semantic_selected = tier_filtered[:top_k]`
# sequence. No source change was needed for Step 4A; these tests exist to
# catch a future regression (e.g. someone "optimizing" by passing top_k
# directly to search_vector_index instead of the full document count).

def test_step4a_excluded_tiers_occupy_global_top_but_allowed_tiers_still_returned(db_path):
    docs = []
    vectors = {}
    # 10 Tier-3/4 documents with the highest cosine scores (closest to the
    # query vector) — these must NEVER crowd out the allowed-tier matches
    # below them, even though they occupy every top-10 global rank.
    for i in range(10):
        fqn = f"dbo.junk_{i:02d}"
        tier = 3 if i % 2 == 0 else 4
        docs.append(_doc(fqn, tier=tier))
        vectors[fqn] = [1.0 - i * 0.001, i * 0.001, 0.0, 0.0]
    # 3 Tier-1/2 documents ranked below all 10 excluded-tier documents.
    for i in range(3):
        fqn = f"dbo.real_{i:02d}"
        tier = 1 if i % 2 == 0 else 2
        docs.append(_doc(fqn, tier=tier))
        vectors[fqn] = [0.5 - i * 0.05, 0.5 + i * 0.05, 0.0, 0.0]

    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, vectors, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=2)

    assert [d.object_fqn for d in result.documents] == ["dbo.real_00", "dbo.real_01"]
    assert all(d.retrieval_tier in (1, 2) for d in result.documents)
    assert all(math.isclose(d.score, {
        "dbo.real_00": _unit(0.5, 0.5, 0.0, 0.0),
        "dbo.real_01": _unit(0.45, 0.55, 0.0, 0.0),
    }[d.object_fqn][0], abs_tol=1e-6) for d in result.documents)


def test_step4a_top_k_still_honored_when_more_than_enough_allowed_tier_matches_exist(db_path):
    docs = []
    vectors = {}
    for i in range(10):
        fqn = f"dbo.junk_{i:02d}"
        docs.append(_doc(fqn, tier=4))
        vectors[fqn] = [1.0 - i * 0.001, i * 0.001, 0.0, 0.0]
    for i in range(5):
        fqn = f"dbo.real_{i:02d}"
        docs.append(_doc(fqn, tier=1))
        vectors[fqn] = [0.5 - i * 0.05, 0.5 + i * 0.05, 0.0, 0.0]

    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, vectors, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=3)

    # Exactly top_k=3 returned (not fewer, not all 5 available) — the
    # highest-scoring 3 among the 5 allowed-tier candidates.
    assert len(result.documents) == 3
    assert [d.object_fqn for d in result.documents] == ["dbo.real_00", "dbo.real_01", "dbo.real_02"]


def test_step4a_mixed_allowed_tiers_compete_globally_by_score(db_path):
    # allowed_tiers spans every tier — ranking must be purely by score,
    # never grouped/clustered by tier.
    docs = [
        _doc("dbo.t1_low", tier=1),
        _doc("dbo.t3_high", tier=3),
        _doc("dbo.t2_mid", tier=2),
        _doc("dbo.t4_highest", tier=4),
    ]
    vectors = {
        "dbo.t1_low": [0.1, 0.9, 0.0, 0.0],
        "dbo.t3_high": [0.9, 0.1, 0.0, 0.0],
        "dbo.t2_mid": [0.5, 0.5, 0.0, 0.0],
        "dbo.t4_highest": [1.0, 0.0, 0.0, 0.0],
    }
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, vectors, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=4, allowed_tiers=frozenset({1, 2, 3, 4}),
    )

    # Expected order is purely score-descending: t4_highest (1.0) >
    # t3_high (~0.99) > t2_mid (~0.707) > t1_low (~0.11) — interleaved
    # tiers 4,3,2,1, never grouped by tier.
    assert [d.object_fqn for d in result.documents] == [
        "dbo.t4_highest", "dbo.t3_high", "dbo.t2_mid", "dbo.t1_low",
    ]
    assert [d.retrieval_tier for d in result.documents] == [4, 3, 2, 1]


def test_step4a_oversized_first_candidate_skipped_smaller_later_candidate_accepted(db_path):
    huge_text = "OBJECT: dbo.huge\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + ("x" * 3000)
    small_text = "OBJECT: dbo.small\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
    docs = [_doc("dbo.huge", text=huge_text), _doc("dbo.small", text=small_text)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.huge": [1.0, 0.0, 0.0, 0.0],    # ranked first (best score) but oversized for the budget
        "dbo.small": [0.8, 0.2, 0.0, 0.0],   # ranked second, fits comfortably
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, max_context_characters=200,
    )

    assert [d.object_fqn for d in result.documents] == ["dbo.small"]
    assert result.documents[0].document_text == small_text
    assert result.truncated is True
    assert any("dbo.huge" in w for w in result.warnings)


def test_step4a_budget_error_only_when_no_candidate_at_all_can_fit(db_path):
    # Multiple candidates, ALL oversized — the documented error must still
    # fire (not just for the single-candidate case already covered
    # elsewhere), confirming it is never a false negative when a first
    # candidate happens to be small.
    huge_text_1 = "OBJECT: dbo.huge1\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + ("x" * 500)
    huge_text_2 = "OBJECT: dbo.huge2\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  " + ("y" * 500)
    docs = [_doc("dbo.huge1", text=huge_text_1), _doc("dbo.huge2", text=huge_text_2)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.huge1": [1.0, 0.0, 0.0, 0.0],
        "dbo.huge2": [0.9, 0.1, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    with pytest.raises(ValueError):
        retrieve_schema_context(
            "question", build.id, 1, "alice", provider, top_k=2, max_context_characters=50,
        )


# ── Step 4: deterministic hybrid semantic + lexical ranking ────────────────
#
# Phase 5, Step 4. HYBRID (RANKING_MODE_HYBRID) is now the default; every
# test above this section passes with zero changes because their fixtures'
# question text ("question", or a bare relationship-topology probe) never
# lexically overlaps any test document's object name/metadata — fusion
# reduces to the semantic term alone in every one of those cases, which is
# a strictly rank-order-preserving transform of the original ranking. The
# tests below exercise the NEW behavior directly.


def test_hybrid_lexical_signal_outranks_stronger_semantic_alternative(db_path):
    docs = [
        _doc("dbo.wrong_semantic", metadata={}),
        _doc("dbo.right_lexical", metadata={"synonyms": ("widget",)}),
        _doc("dbo.irrelevant", metadata={}),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.wrong_semantic": [1.0, 0.0, 0.0, 0.0],   # best cosine, but zero lexical overlap
        "dbo.right_lexical": [0.9, 0.1, 0.0, 0.0],    # second-best cosine, exact synonym match
        "dbo.irrelevant": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("widget", build.id, 1, "alice", provider, top_k=1)

    assert [d.object_fqn for d in result.documents] == ["dbo.right_lexical"]
    assert result.documents[0].lexical_rank == 1
    assert result.documents[0].semantic_rank == 2
    assert result.documents[0].fused_score is not None


def test_pure_vector_mode_reproduces_original_ranking_on_the_same_fixture(db_path):
    # Identical fixture to the hybrid test above — proves RANKING_MODE_VECTOR
    # is a genuinely preserved, independently selectable comparison mode
    # that still picks the pure-cosine winner even where hybrid would not.
    docs = [
        _doc("dbo.wrong_semantic", metadata={}),
        _doc("dbo.right_lexical", metadata={"synonyms": ("widget",)}),
        _doc("dbo.irrelevant", metadata={}),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.wrong_semantic": [1.0, 0.0, 0.0, 0.0],
        "dbo.right_lexical": [0.9, 0.1, 0.0, 0.0],
        "dbo.irrelevant": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "widget", build.id, 1, "alice", provider, top_k=1, ranking_mode=RANKING_MODE_VECTOR,
    )

    assert [d.object_fqn for d in result.documents] == ["dbo.wrong_semantic"]
    assert result.ranking_mode == RANKING_MODE_VECTOR
    assert math.isclose(result.documents[0].score, 1.0, abs_tol=1e-9)
    assert result.documents[0].lexical_rank is None
    assert result.documents[0].lexical_score is None


def test_hybrid_is_the_default_ranking_mode(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider)
    assert result.ranking_mode == RANKING_MODE_HYBRID


def test_invalid_ranking_mode_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        retrieve_schema_context("question", build.id, 1, "alice", provider, ranking_mode="bogus")


def test_hybrid_semantic_only_match_remains_discoverable(db_path):
    # A document with strong semantic similarity but ZERO lexical overlap
    # must still be selected — omission from the lexical ranking must
    # never suppress a genuinely strong semantic match.
    docs = [_doc("dbo.pure_semantic_hit", metadata={}), _doc("dbo.far", metadata={})]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.pure_semantic_hit": [1.0, 0.0, 0.0, 0.0],
        "dbo.far": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("completely unrelated words here", build.id, 1, "alice", provider, top_k=1)

    assert [d.object_fqn for d in result.documents] == ["dbo.pure_semantic_hit"]
    assert result.documents[0].lexical_rank is None
    assert result.documents[0].semantic_rank == 1


def test_hybrid_tier_filtering_applied_before_fused_top_k(db_path):
    # Tier-3/4 documents with BOTH the best cosine score AND a strong exact
    # lexical match must still never leak into the result — tier policy is
    # applied before fusion/top-k, exactly as for pure semantic ranking
    # (mirrors test_step4a_excluded_tiers_occupy_global_top_but_allowed_tiers_still_returned).
    docs = []
    vectors = {}
    for i in range(6):
        fqn = f"dbo.junk_{i:02d}"
        tier = 3 if i % 2 == 0 else 4
        docs.append(_doc(fqn, tier=tier, metadata={"synonyms": ("target",)}))
        vectors[fqn] = [1.0 - i * 0.001, i * 0.001, 0.0, 0.0]
    for i in range(2):
        fqn = f"dbo.real_{i:02d}"
        docs.append(_doc(fqn, tier=1, metadata={}))
        vectors[fqn] = [0.3 - i * 0.05, 0.7 + i * 0.05, 0.0, 0.0]

    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, vectors, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("target", build.id, 1, "alice", provider, top_k=2)

    assert {d.object_fqn for d in result.documents} == {"dbo.real_00", "dbo.real_01"}
    assert all(d.retrieval_tier in (1, 2) for d in result.documents)


def test_relationship_expansion_operates_from_hybrid_seeds_not_pure_semantic_seeds(db_path):
    docs = [
        _doc("dbo.wrong_semantic", metadata={}),  # best cosine, no relationship data at all
        _doc("dbo.right_lexical", metadata={
            "synonyms": ("widget",), "confirmed_relationship_targets": ("dbo.related_to_lexical.id",),
        }),
        _doc("dbo.related_to_lexical", metadata={}),
        _doc("dbo.irrelevant", metadata={}),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.wrong_semantic": [1.0, 0.0, 0.0, 0.0],
        "dbo.right_lexical": [0.9, 0.1, 0.0, 0.0],
        "dbo.related_to_lexical": [0.0, 0.0, 1.0, 0.0],   # orthogonal — only reachable via relationship
        "dbo.irrelevant": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    hybrid = retrieve_schema_context("widget", build.id, 1, "alice", provider, top_k=1, relationship_limit=5)
    by_fqn = {d.object_fqn: d for d in hybrid.documents}
    assert set(by_fqn) == {"dbo.right_lexical", "dbo.related_to_lexical"}
    assert by_fqn["dbo.related_to_lexical"].selection_reason == SELECTION_REASON_DIRECT_RELATIONSHIP

    vector_only = retrieve_schema_context(
        "widget", build.id, 1, "alice", provider, top_k=1, relationship_limit=5, ranking_mode=RANKING_MODE_VECTOR,
    )
    assert {d.object_fqn for d in vector_only.documents} == {"dbo.wrong_semantic"}


def test_hybrid_matched_lexical_fields_reported_on_context_document(db_path):
    docs = [_doc("dbo.a", metadata={"business_name": "Widget Registry"}), _doc("dbo.b", metadata={})]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("widget registry", build.id, 1, "alice", provider, top_k=2)

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert "BUSINESS_NAME" in by_fqn["dbo.a"].matched_lexical_fields
    assert by_fqn["dbo.b"].matched_lexical_fields == ()


def test_relationship_expansion_document_has_no_rank_components(db_path):
    docs = [
        _doc("dbo.a", metadata={"synonyms": ("widget",), "confirmed_relationship_targets": ("dbo.b.id",)}),
        _doc("dbo.b", metadata={}),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("widget", build.id, 1, "alice", provider, top_k=1, relationship_limit=5)
    by_fqn = {d.object_fqn: d for d in result.documents}

    rel_doc = by_fqn["dbo.b"]
    assert rel_doc.selection_reason == SELECTION_REASON_DIRECT_RELATIONSHIP
    assert rel_doc.score is None
    assert rel_doc.semantic_rank is None
    assert rel_doc.lexical_rank is None
    assert rel_doc.lexical_score is None
    assert rel_doc.fused_score is None
    assert rel_doc.matched_lexical_fields == ()


def test_hybrid_ranking_is_deterministic_across_repeated_calls(db_path):
    docs = [
        _doc("dbo.a", metadata={"synonyms": ("widget", "gadget")}),
        _doc("dbo.b", metadata={"synonyms": ("widget",)}),
        _doc("dbo.c", metadata={}),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.a": [0.6, 0.4, 0.0, 0.0],
        "dbo.b": [0.5, 0.5, 0.0, 0.0],
        "dbo.c": [1.0, 0.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    first = retrieve_schema_context("widget gadget", build.id, 1, "alice", provider, top_k=3)
    second = retrieve_schema_context("widget gadget", build.id, 1, "alice", provider, top_k=3)

    assert first.documents == second.documents


def test_hybrid_deterministic_tie_break_by_object_fqn(db_path):
    # Three documents with identical semantic AND lexical standing must
    # break ties by object_fqn ascending, exactly like pure vector mode.
    docs = [
        _doc("dbo.zzz", metadata={"synonyms": ("widget",)}),
        _doc("dbo.aaa", metadata={"synonyms": ("widget",)}),
        _doc("dbo.mmm", metadata={"synonyms": ("widget",)}),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.zzz": [1.0, 0.0, 0.0, 0.0],
        "dbo.aaa": [1.0, 0.0, 0.0, 0.0],
        "dbo.mmm": [1.0, 0.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("widget", build.id, 1, "alice", provider, top_k=3)
    assert [d.object_fqn for d in result.documents] == ["dbo.aaa", "dbo.mmm", "dbo.zzz"]


def test_hybrid_no_sqlite_calls_during_lexical_ranking(db_path, monkeypatch):
    # Lexical index construction reuses the already-loaded vector-index
    # documents in memory — it must never issue its own SQLite read.
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    def _boom(*args, **kwargs):
        raise AssertionError("load_stored_retrieval_documents must not be called by lexical ranking")

    # First call is allowed to load (and cache) the vector index normally.
    first = retrieve_schema_context("question about widgets", build.id, 1, "alice", provider)
    assert first.status == STATUS_OK

    # Force a fresh lexical-index build (cache cleared) on the SECOND call,
    # while the vector index itself stays cached — proves lexical-index
    # construction alone (from already-loaded index.documents) needs no DB.
    clear_lexical_index_cache()
    monkeypatch.setattr(vector_index_module, "load_stored_retrieval_documents", _boom)
    second = retrieve_schema_context("question about widgets", build.id, 1, "alice", provider)
    assert second.status == STATUS_OK


# ── Step 5: candidate pool, relationship graph/bridging, size-aware packing ──


def test_step5_multi_table_anchor_coverage(db_path):
    docs = [_doc(f"dbo.t{i}") for i in range(4)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.t0": [1.0, 0.0, 0.0, 0.0], "dbo.t1": [0.9, 0.1, 0.0, 0.0],
        "dbo.t2": [0.8, 0.2, 0.0, 0.0], "dbo.t3": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=3)

    assert {d.object_fqn for d in result.documents} == {"dbo.t0", "dbo.t1", "dbo.t2"}
    assert all(d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR for d in result.documents)


def test_step5_bridge_table_included_via_two_edge_path(db_path):
    docs = [
        _rel_doc("dbo.anchor1", targets=("dbo.bridge.id",)),
        _rel_doc("dbo.anchor2"),
        _rel_doc("dbo.bridge", targets=("dbo.anchor2.id",)),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor2": [0.9, 0.1, 0.0, 0.0],
        "dbo.bridge": [0.0, 0.0, 1.0, 0.0],   # orthogonal — never a semantic/lexical anchor on its own
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5, max_relationship_path_length=2,
    )

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert {"dbo.anchor1", "dbo.anchor2", "dbo.bridge"}.issubset(by_fqn)
    assert by_fqn["dbo.bridge"].selection_reason == SELECTION_REASON_RELATIONSHIP_BRIDGE


def test_step5_three_edge_path_bridge_inclusion(db_path):
    docs = [
        _rel_doc("dbo.anchor1", targets=("dbo.b1.id",)),
        _rel_doc("dbo.b1", targets=("dbo.b2.id",)),
        _rel_doc("dbo.b2", targets=("dbo.anchor2.id",)),
        _rel_doc("dbo.anchor2"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor2": [0.9, 0.1, 0.0, 0.0],
        "dbo.b1": [0.0, 0.0, 1.0, 0.0],
        "dbo.b2": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5, max_relationship_path_length=3,
    )

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert {"dbo.anchor1", "dbo.anchor2", "dbo.b1", "dbo.b2"}.issubset(by_fqn)
    assert by_fqn["dbo.b1"].selection_reason == SELECTION_REASON_RELATIONSHIP_BRIDGE
    assert by_fqn["dbo.b2"].selection_reason == SELECTION_REASON_RELATIONSHIP_BRIDGE


def test_step5_path_length_limit_excludes_bridge_beyond_the_limit(db_path):
    # A 4-edge chain anchor1-x1-mid-x2-anchor2. x1/x2 are each one hop from
    # an anchor (so they may ALSO be found by the separate "direct
    # relationship" mechanism regardless of path length — that is correct,
    # expected behavior, not what this test is isolating). "mid" is TWO
    # hops from both anchors — reachable ONLY via multi-hop bridging — so
    # it is the node that proves the path-length limit actually bites.
    docs = [
        _rel_doc("dbo.anchor1", targets=("dbo.x1.id",)),
        _rel_doc("dbo.x1", targets=("dbo.mid.id",)),
        _rel_doc("dbo.mid", targets=("dbo.x2.id",)),
        _rel_doc("dbo.x2", targets=("dbo.anchor2.id",)),
        _rel_doc("dbo.anchor2"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor2": [0.9, 0.1, 0.0, 0.0],
        "dbo.x1": [0.0, 0.0, 1.0, 0.0],
        "dbo.mid": [0.0, 0.0, 0.0, 1.0],
        "dbo.x2": [0.0, -1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    # The real anchor1<->anchor2 distance is 4 edges; capping the bridge
    # search at 2 must fail to find that path at all, so "mid" (reachable
    # only through it) is never added.
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5, max_relationship_path_length=2,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.mid" not in fqns
    assert {"dbo.anchor1", "dbo.anchor2"}.issubset(fqns)


def test_step5_max_relationship_path_length_hard_cap_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()
    with pytest.raises(ValueError):
        retrieve_schema_context("question", build.id, 1, "alice", provider, max_relationship_path_length=4)


def test_step5_relationship_limit_caps_combined_bridges_and_directs(db_path):
    docs = [
        _rel_doc("dbo.anchor1", targets=("dbo.bridge.id", "dbo.direct1.id", "dbo.direct2.id")),
        _rel_doc("dbo.anchor2"),
        _rel_doc("dbo.bridge", targets=("dbo.anchor2.id",)),
        _rel_doc("dbo.direct1"),
        _rel_doc("dbo.direct2"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0], "dbo.anchor2": [0.9, 0.1, 0.0, 0.0],
        "dbo.bridge": [0.0, 0.0, 1.0, 0.0], "dbo.direct1": [0.0, 0.0, 0.0, 1.0],
        "dbo.direct2": [0.0, -1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, relationship_limit=1, max_relationship_path_length=2,
    )

    relationship_additions = [
        d for d in result.documents
        if d.selection_reason in (SELECTION_REASON_RELATIONSHIP_BRIDGE, SELECTION_REASON_DIRECT_RELATIONSHIP)
    ]
    assert len(relationship_additions) == 1  # capped, bridge prioritized over directs
    assert relationship_additions[0].selection_reason == SELECTION_REASON_RELATIONSHIP_BRIDGE


def test_step5_no_inferred_edge_expansion(db_path):
    docs = [
        _doc("dbo.anchor1", metadata={"inferred_relationship_targets": ("dbo.only_inferred.id",)}),
        _doc("dbo.only_inferred"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0],
        "dbo.only_inferred": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, relationship_limit=5)

    assert "dbo.only_inferred" not in {d.object_fqn for d in result.documents}


def test_step5_allowed_tier_enforcement_blocks_bridge_through_disallowed_tier(db_path):
    docs = [
        _rel_doc("dbo.anchor1", targets=("dbo.archived_bridge.id",), tier=1),
        _rel_doc("dbo.archived_bridge", targets=("dbo.anchor2.id",), tier=4),
        _rel_doc("dbo.anchor2", tier=1),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor2": [0.9, 0.1, 0.0, 0.0],
        "dbo.archived_bridge": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5, max_relationship_path_length=2,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.archived_bridge" not in fqns  # Tier 4 — never a graph member, path can't use it
    assert {"dbo.anchor1", "dbo.anchor2"}.issubset(fqns)  # both still individually included as anchors


def test_step5_disconnected_anchors_handled_safely(db_path):
    docs = [_doc("dbo.anchor1"), _doc("dbo.anchor2")]  # zero relationship metadata anywhere
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor2": [0.9, 0.1, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5, max_relationship_path_length=3,
    )

    assert result.status == STATUS_OK
    assert {d.object_fqn for d in result.documents} == {"dbo.anchor1", "dbo.anchor2"}


def test_step5_large_essential_anchor_compacted_in_full_pipeline(db_path):
    metadata = {"business_name": "Big Essential Table", "primary_key_columns": ("Id",)}
    huge_text = "OBJECT: dbo.huge\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n" + ("  col INTEGER\n" * 400)
    small_text = "OBJECT: dbo.small\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
    docs = [_doc("dbo.huge", text=huge_text, metadata=metadata), _doc("dbo.small", text=small_text)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.huge": [1.0, 0.0, 0.0, 0.0],
        "dbo.small": [0.9, 0.1, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, max_context_characters=len(small_text) + 300,
    )

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert "dbo.huge" in by_fqn  # essential anchor retained, not dropped
    assert by_fqn["dbo.huge"].used_compact_text is True
    assert by_fqn["dbo.huge"].document_text == huge_text  # original preserved for identity/auditing
    assert "dbo.small" in by_fqn
    assert by_fqn["dbo.small"].used_compact_text is False


def test_step5_deterministic_ties_across_repeated_calls_full_pipeline(db_path):
    docs = [
        _rel_doc("dbo.anchor1", targets=("dbo.bridge.id",)),
        _rel_doc("dbo.anchor2"),
        _rel_doc("dbo.bridge", targets=("dbo.anchor2.id",)),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor1": [1.0, 0.0, 0.0, 0.0], "dbo.anchor2": [0.9, 0.1, 0.0, 0.0], "dbo.bridge": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    first = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5)
    second = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=2, relationship_limit=5)
    assert first.documents == second.documents
    assert first.warnings == second.warnings


def test_step5_candidate_pool_size_must_be_at_least_top_k(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider()
    with pytest.raises(ValueError):
        retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=5, candidate_pool_size=2)


# ── Phase 7, Step 3B, Part 2 — automatic business-concept retrieval fusion ──
#
# core.sqlservice.business_concept_index has its own dedicated unit-test
# file (tests/test_sqlservice_business_concept_index.py). These tests cover
# ONLY the FUSION wiring in this module: the business-concept lane's search
# result is injected directly (monkeypatch) so these tests are independent
# of that module's own trust/enrichment-reading correctness.

import core.sqlservice.schema_context_retrieval as schema_context_retrieval_module
from core.sqlservice.business_concept_index import BusinessConceptSearchResult, MatchedConceptEvidence


def _fake_business_concept_search(matches: dict):
    """matches: {object_fqn: (score, [MatchedConceptEvidence, ...])} — returns
    a callable matching search_business_concept_index's own signature."""
    def _search(index, query_tokens, *, top_k):
        results = [
            BusinessConceptSearchResult(
                object_fqn=fqn, score=score, matched_fields=("SYNONYMS",), matched_evidence=tuple(evidence),
            )
            for fqn, (score, evidence) in matches.items()
        ]
        results.sort(key=lambda r: (-r.score, r.object_fqn))
        return tuple(results)[:top_k]
    return _search


def test_business_concept_lane_populates_observability_fields(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.zz_widget"))
    _embed_all(db_path, build, {
        "dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.zz_widget": [0.0, 0.0, 0.0, 1.0],
    })
    evidence = [MatchedConceptEvidence(concept="widget", source="ENTITY_ASSIGNMENT", matched_field="SYNONYMS")]
    monkeypatch.setattr(
        schema_context_retrieval_module, "search_business_concept_index",
        _fake_business_concept_search({"dbo.zz_widget": (5.0, evidence)}),
    )
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("show me widgets", build.id, 1, "alice", provider)

    assert result.status == STATUS_OK
    by_fqn = {d.object_fqn: d for d in result.documents}
    assert "dbo.zz_widget" in by_fqn
    widget_doc = by_fqn["dbo.zz_widget"]
    assert widget_doc.business_concept_rank == 1
    assert widget_doc.business_concept_score == 5.0
    assert widget_doc.matched_business_concepts == tuple(evidence)
    # An object with no business-concept match keeps every new field at its
    # default-safe None/() value — never a fabricated zero.
    learners_doc = by_fqn["dbo.learners"]
    assert learners_doc.business_concept_rank is None
    assert learners_doc.business_concept_score is None
    assert learners_doc.matched_business_concepts == ()


def test_business_concept_lane_can_change_fused_ordering(db_path, monkeypatch):
    """A weakly-semantically-relevant object with a strong business-concept
    match can outrank a more semantically-similar object with none — proof
    the lane genuinely participates in fusion, not just observability."""
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.zz_container"))
    _embed_all(db_path, build, {
        "dbo.learners": [0.9, 0.1, 0.0, 0.0], "dbo.zz_container": [0.85, 0.15, 0.0, 0.0],
    })
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    monkeypatch.setattr(
        schema_context_retrieval_module, "search_business_concept_index",
        _fake_business_concept_search({}),
    )
    baseline = retrieve_schema_context("show me widgets", build.id, 1, "alice", provider, top_k=1)
    baseline_top = baseline.documents[0].object_fqn

    evidence = [MatchedConceptEvidence(concept="widget", source="ENTITY_ASSIGNMENT", matched_field="SYNONYMS")]
    monkeypatch.setattr(
        schema_context_retrieval_module, "search_business_concept_index",
        _fake_business_concept_search({"dbo.zz_container": (10.0, evidence)}),
    )
    boosted = retrieve_schema_context("show me widgets", build.id, 1, "alice", provider, top_k=1)
    boosted_top = boosted.documents[0].object_fqn

    assert baseline_top == "dbo.learners"
    assert boosted_top == "dbo.zz_container"


def test_business_concept_lane_disabled_flag_never_calls_search(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners",))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return ()

    monkeypatch.setattr(schema_context_retrieval_module, "search_business_concept_index", _spy)
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, enable_business_concept_retrieval=False,
    )
    assert result.status == STATUS_OK
    assert calls == []
    assert result.documents[0].business_concept_rank is None


def test_business_concept_lane_empty_result_leaves_output_unchanged(db_path, monkeypatch):
    """'vector/lexical behavior unchanged when no semantic index exists' —
    an empty business-concept search result must produce byte-identical
    output to the lane being disabled outright."""
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.tracks": [0.0, 1.0, 0.0, 0.0]})
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    monkeypatch.setattr(
        schema_context_retrieval_module, "search_business_concept_index", _fake_business_concept_search({}),
    )
    with_empty_lane = retrieve_schema_context("question", build.id, 1, "alice", provider)
    disabled = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, enable_business_concept_retrieval=False,
    )
    assert with_empty_lane.documents == disabled.documents
    assert with_empty_lane.context_text == disabled.context_text


def test_business_concept_lane_still_exactly_one_embed_call(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.zz_widget"))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.zz_widget": [0.0, 1.0, 0.0, 0.0]})
    evidence = [MatchedConceptEvidence(concept="widget", source="ENTITY_ASSIGNMENT", matched_field="SYNONYMS")]
    monkeypatch.setattr(
        schema_context_retrieval_module, "search_business_concept_index",
        _fake_business_concept_search({"dbo.zz_widget": (5.0, evidence)}),
    )
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))
    result = retrieve_schema_context("show me widgets", build.id, 1, "alice", provider)
    assert result.status == STATUS_OK
    assert len(provider.calls) == 1


def test_business_concept_lane_deterministic_across_repeated_calls(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.zz_widget"))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.zz_widget": [0.0, 1.0, 0.0, 0.0]})
    evidence = [MatchedConceptEvidence(concept="widget", source="ENTITY_ASSIGNMENT", matched_field="SYNONYMS")]
    monkeypatch.setattr(
        schema_context_retrieval_module, "search_business_concept_index",
        _fake_business_concept_search({"dbo.zz_widget": (5.0, evidence)}),
    )
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))
    first = retrieve_schema_context("show me widgets", build.id, 1, "alice", provider)
    second = retrieve_schema_context("show me widgets", build.id, 1, "alice", provider)
    assert first.documents == second.documents
