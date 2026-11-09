"""
Focused unit tests — SQL rebuild Phase 2, Step 3: immutable in-memory
vector index and exact cosine-similarity search
(core/sqlservice/vector_index.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server, never OpenAI. Every vector in this
file is a small hand-written or seeded-random list of floats — no real
embedding is ever generated or requested.

Run:
    venv/Scripts/pytest tests/test_sqlservice_vector_index.py -v
"""

import hashlib
import math
import os
import random
import shutil
import sqlite3
import time
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
from core.sqlservice.embedding_store import (
    STATUS_BUILD_NOT_FOUND,
    STATUS_BUILD_NOT_READY,
    STATUS_OK as EMBEDDING_STATUS_OK,
    STATUS_UNOWNED,
    StoredEmbedding,
    insert_embeddings,
)
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.vector_index import (
    STATUS_INCOMPLETE,
    STATUS_INVALID,
    IndexedSchemaDocument,
    VectorIndex,
    VectorIndexIdentity,
    clear_vector_index_cache,
    load_vector_index,
    search_vector_index,
    vector_index_cache_size,
)


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


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    """
    The vector-index cache is a module-level global, and every test's temp
    DB reuses the same small build_id numbering (1, 2, ...) — without this,
    a cache entry from one test's database could be served to a later
    test's completely different database under the same identity. Clearing
    before AND after every test keeps each test's cache state isolated.
    """
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


PROVIDER = "openai"
MODEL = "text-embedding-3-small"
DIMS = 4


def _unit(*components) -> tuple:
    norm = math.sqrt(sum(c * c for c in components))
    return tuple(c / norm for c in components)


def _embed_all(db_path, build, vectors_by_fqn, *, docs=None, provider=PROVIDER, model=MODEL, dims=DIMS, source_id=1, user_id="alice"):
    """vectors_by_fqn: {object_fqn: [raw floats]} — inserted via the real
    insert_embeddings() API, keyed by each document's real hash. `docs` (the
    actual RetrievalDocument list the build was created from) is required
    whenever a document's hash depends on more than its object_fqn and
    default tier=1/object_type="TABLE" (document_text — and therefore
    document_hash — also encodes tier and type); defaults to recomputing
    via _doc(fqn) for the common tier=1 case."""
    if docs is not None:
        hash_by_fqn = {d.object_fqn: d.document_hash for d in docs}
    else:
        hash_by_fqn = {fqn: _doc(fqn).document_hash for fqn in vectors_by_fqn}
    vectors_by_hash = {hash_by_fqn[fqn]: vec for fqn, vec in vectors_by_fqn.items()}
    result = insert_embeddings(build.id, source_id, user_id, provider, model, dims, vectors_by_hash)
    assert result.status == EMBEDDING_STATUS_OK
    return result


# ── 1. Complete READY build loads successfully ─────────────────────────────

def test_complete_build_loads_successfully(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    _embed_all(db_path, build, {
        "dbo.learners": [1.0, 0.0, 0.0, 0.0],
        "dbo.tracks": [0.0, 1.0, 0.0, 0.0],
    })

    result = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == EMBEDDING_STATUS_OK
    assert result.index is not None
    assert result.document_count == 2
    assert [d.object_fqn for d in result.index.documents] == ["dbo.learners", "dbo.tracks"]  # deterministic order
    assert result.index.identity == VectorIndexIdentity(build.id, PROVIDER, MODEL, DIMS)


# ── 2. Incomplete embeddings are rejected ──────────────────────────────────

def test_incomplete_embeddings_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0]})  # tracks missing

    result = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_INCOMPLETE
    assert result.index is None
    assert result.document_count == 2
    assert result.error is not None


# ── 3. Ownership / build-status failures ───────────────────────────────────

def test_unowned_source_rejected(db_path):
    build = _make_ready_build(db_path, user_id="alice")
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.tracks": [0.0, 1.0, 0.0, 0.0]})

    result = load_vector_index(build.id, 1, "mallory", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_UNOWNED
    assert result.index is None


def test_build_not_found_for_source_rejected(db_path):
    build = _make_ready_build(db_path, source_id=1, snapshot_id=1)
    _insert_source(db_path, 2)

    result = load_vector_index(build.id, 2, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_BUILD_NOT_FOUND
    assert result.index is None


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

    result = load_vector_index(1, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_BUILD_NOT_READY
    assert result.index is None


# ── 4. Provider/model/dimension isolation ──────────────────────────────────

def test_different_identity_produces_separate_indices(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    _embed_all(db_path, build, {"dbo.learners": [1.0, 0.0, 0.0, 0.0], "dbo.tracks": [0.0, 1.0, 0.0, 0.0]},
               provider="openai", model="text-embedding-3-small", dims=4)
    _embed_all(db_path, build, {
        "dbo.learners": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "dbo.tracks": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
    }, provider="openai", model="text-embedding-3-large", dims=6)

    small = load_vector_index(build.id, 1, "alice", "openai", "text-embedding-3-small", 4)
    large = load_vector_index(build.id, 1, "alice", "openai", "text-embedding-3-large", 6)

    assert small.status == EMBEDDING_STATUS_OK and large.status == EMBEDDING_STATUS_OK
    assert small.index is not large.index
    assert all(len(d.vector) == 4 for d in small.index.documents)
    assert all(len(d.vector) == 6 for d in large.index.documents)

    # Requesting a THIRD identity for the same build (never embedded) must
    # fail, never accidentally return either of the other two.
    missing = load_vector_index(build.id, 1, "alice", "openai", "text-embedding-3-small", 8)
    assert missing.status == STATUS_INCOMPLETE


# ── 5. Exact cosine ranking ─────────────────────────────────────────────────

def test_exact_cosine_ranking(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b", "dbo.c"))
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.0, 1.0, 0.0, 0.0],
        "dbo.c": [1.0, 1.0, 0.0, 0.0],
    })
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    results = search_vector_index(index, [1.0, 0.0, 0.0, 0.0], top_k=10)

    assert [r.object_fqn for r in results] == ["dbo.a", "dbo.c", "dbo.b"]
    assert math.isclose(results[0].score, 1.0, abs_tol=1e-9)
    assert math.isclose(results[1].score, math.sqrt(2) / 2, abs_tol=1e-9)
    assert math.isclose(results[2].score, 0.0, abs_tol=1e-9)


# ── 6. Deterministic ties ───────────────────────────────────────────────────

def test_deterministic_tie_break_by_object_fqn(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.zzz", "dbo.aaa", "dbo.mmm"))
    _embed_all(db_path, build, {
        "dbo.zzz": [1.0, 0.0, 0.0, 0.0],
        "dbo.aaa": [1.0, 0.0, 0.0, 0.0],
        "dbo.mmm": [1.0, 0.0, 0.0, 0.0],
    })
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    results = search_vector_index(index, [1.0, 0.0, 0.0, 0.0], top_k=10)
    assert [r.object_fqn for r in results] == ["dbo.aaa", "dbo.mmm", "dbo.zzz"]
    assert results[0].score == results[1].score == results[2].score


# ── 7. top_k greater than available results ────────────────────────────────

def test_top_k_greater_than_available_results(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    results = search_vector_index(index, [1.0, 0.0, 0.0, 0.0], top_k=1000)
    assert len(results) == 2


# ── 8. Tier filtering ────────────────────────────────────────────────────────

def test_tier_filtering(db_path):
    docs = [
        _doc("dbo.tier1", tier=1),
        _doc("dbo.tier2", tier=2),
        _doc("dbo.tier2_b", tier=2),
        _doc("dbo.tier4", tier=4),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.tier1": [1.0, 0.0, 0.0, 0.0],
        "dbo.tier2": [1.0, 0.0, 0.0, 0.0],
        "dbo.tier2_b": [1.0, 0.0, 0.0, 0.0],
        "dbo.tier4": [1.0, 0.0, 0.0, 0.0],
    }, docs=docs)
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    results = search_vector_index(index, [1.0, 0.0, 0.0, 0.0], top_k=10, tier=2)
    assert {r.object_fqn for r in results} == {"dbo.tier2", "dbo.tier2_b"}
    assert all(r.retrieval_tier == 2 for r in results)


def test_invalid_tier_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, 0.0, 0.0, 0.0], tier=0)
    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, 0.0, 0.0, 0.0], tier=5)


def test_invalid_top_k_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, 0.0, 0.0, 0.0], top_k=0)
    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, 0.0, 0.0, 0.0], top_k=-1)


# ── 9. Invalid query vectors and dimensions / zero vectors ─────────────────

def test_empty_query_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index
    with pytest.raises(ValueError):
        search_vector_index(index, [])


def test_wrong_dimension_query_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index
    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, 0.0])  # 2, expected 4


def test_nan_query_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index
    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, float("nan"), 0.0, 0.0])


def test_infinite_query_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index
    with pytest.raises(ValueError):
        search_vector_index(index, [1.0, float("inf"), 0.0, 0.0])


def test_zero_query_vector_rejected(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index
    with pytest.raises(ValueError):
        search_vector_index(index, [0.0, 0.0, 0.0, 0.0])


# ── 10/11. Malformed stored vectors (zero-norm reachable via the real API;
#           wrong-dimension / NaN via a defensive unit-level seam test) ────

def test_zero_norm_stored_vector_rejected_at_load(db_path):
    # serialize_vector() only rejects empty/non-finite values, never a
    # zero-norm vector — so this is genuinely reachable through the real
    # insert_embeddings() API, unlike a dimension mismatch (see below).
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {
        "dbo.a": [0.0, 0.0, 0.0, 0.0],
        "dbo.b": [1.0, 0.0, 0.0, 0.0],
    })

    result = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_INVALID
    assert result.index is None
    assert "zero norm" in result.error


def test_wrong_dimension_stored_vector_rejected_at_load(db_path, monkeypatch):
    # A dimension mismatch can never actually reach storage — the DB CHECK
    # constraint (vector_byte_length = embedding_dimensions * 4) enforces
    # consistency at the row level, and get_embeddings_for_build() filters
    # by dimensions in its WHERE clause. This exercises this module's own
    # defensive check at the seam with embedding_store, as a safety net
    # rather than a currently-reachable production defect.
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})

    real_hash = _doc("dbo.a").document_hash
    fabricated = (
        StoredEmbedding(
            id=1, object_fqn="dbo.a", embedding_provider=PROVIDER, embedding_model=MODEL,
            embedding_dimensions=DIMS, document_hash=real_hash, vector=(1.0, 0.0),  # only 2, not 4
            created_at="now",
        ),
    )
    monkeypatch.setattr(vector_index_module, "get_embeddings_for_build", lambda *a, **k: fabricated)

    result = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_INVALID
    assert result.index is None


def test_nan_stored_vector_rejected_at_load(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})

    real_hash = _doc("dbo.a").document_hash
    fabricated = (
        StoredEmbedding(
            id=1, object_fqn="dbo.a", embedding_provider=PROVIDER, embedding_model=MODEL,
            embedding_dimensions=DIMS, document_hash=real_hash, vector=(1.0, float("nan"), 0.0, 0.0),
            created_at="now",
        ),
    )
    monkeypatch.setattr(vector_index_module, "get_embeddings_for_build", lambda *a, **k: fabricated)

    result = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_INVALID
    assert result.index is None


# ── 12. Document/vector identity matching ──────────────────────────────────

def test_each_document_gets_its_own_correct_vector(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b", "dbo.c"))
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.0, 0.0, 0.0],
        "dbo.b": [0.0, 1.0, 0.0, 0.0],
        "dbo.c": [0.0, 0.0, 1.0, 0.0],
    })
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    by_fqn = {d.object_fqn: d.vector for d in index.documents}
    assert by_fqn["dbo.a"] == _unit(1.0, 0.0, 0.0, 0.0)
    assert by_fqn["dbo.b"] == _unit(0.0, 1.0, 0.0, 0.0)
    assert by_fqn["dbo.c"] == _unit(0.0, 0.0, 1.0, 0.0)


def test_missing_embedding_despite_reported_complete_rejected(db_path, monkeypatch):
    # Simulates an inconsistent read between the completeness check and the
    # actual embedding fetch (e.g. a concurrent delete) — must fail closed,
    # never silently build a partial index.
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]})

    real_get_embeddings = vector_index_module.get_embeddings_for_build
    monkeypatch.setattr(
        vector_index_module, "get_embeddings_for_build",
        lambda *a, **k: tuple(e for e in real_get_embeddings(*a, **k) if e.object_fqn != "dbo.b"),
    )

    result = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert result.status == STATUS_INVALID
    assert result.index is None
    assert "dbo.b" in result.error


# ── 13. Immutable index and nested metadata ────────────────────────────────

def test_index_and_documents_are_immutable(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    assert isinstance(index.documents, tuple)
    doc = index.documents[0]

    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.object_fqn = "dbo.changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        index.documents = ()

    with pytest.raises(TypeError):
        doc.metadata["new_key"] = "nope"  # inherited deep immutability from retrieval_store


def test_index_metadata_matches_source_document(db_path):
    build = _make_ready_build_with_docs(db_path, [_doc("dbo.a", metadata={"column_count": 7, "domain": "sales"})])
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index

    assert dict(index.documents[0].metadata) == {"column_count": 7, "domain": "sales"}


# ── 14/15/16. Cache hit, identity isolation, explicit clearing ────────────

def test_cache_hit_skips_reloading_documents(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]})

    first = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert first.status == EMBEDDING_STATUS_OK
    assert vector_index_cache_size() == 1

    def _boom(*args, **kwargs):
        raise AssertionError("load_stored_retrieval_documents must not be called on a cache hit")

    monkeypatch.setattr(vector_index_module, "load_stored_retrieval_documents", _boom)

    second = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert second.status == EMBEDDING_STATUS_OK
    assert second.index is first.index  # the exact same cached object


def test_cache_identity_isolation_across_dimensions(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b"))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0], "dbo.b": [0.0, 1.0, 0.0, 0.0]},
               dims=4)
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0], "dbo.b": [0.0, 1.0]},
               provider=PROVIDER, model=MODEL, dims=2)

    r4 = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, 4)
    r2 = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, 2)

    assert r4.index is not r2.index
    assert vector_index_cache_size() == 2
    assert all(len(d.vector) == 4 for d in r4.index.documents)
    assert all(len(d.vector) == 2 for d in r2.index.documents)


def test_explicit_cache_clearing_forces_reload(db_path, monkeypatch):
    build = _make_ready_build(db_path, object_fqns=("dbo.a",))
    _embed_all(db_path, build, {"dbo.a": [1.0, 0.0, 0.0, 0.0]})

    first = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert vector_index_cache_size() == 1

    clear_vector_index_cache()
    assert vector_index_cache_size() == 0

    second = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert second.index is not first.index  # rebuilt, not reused
    assert second.index == first.index  # but structurally identical (frozen dataclass equality)


# ── 17. Deterministic repeated searches ─────────────────────────────────────

def test_repeated_searches_are_bit_identical(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.a", "dbo.b", "dbo.c"))
    _embed_all(db_path, build, {
        "dbo.a": [1.0, 0.3, -0.2, 0.7],
        "dbo.b": [0.1, 1.0, 0.4, -0.3],
        "dbo.c": [0.5, 0.5, 0.5, 0.5],
    })
    index = load_vector_index(build.id, 1, "alice", PROVIDER, MODEL, DIMS).index
    query = [0.2, 0.4, -0.1, 0.9]

    first = search_vector_index(index, query, top_k=10)
    second = search_vector_index(index, query, top_k=10)

    assert first == second  # bit-identical, including scores


# ── 18. All existing SQL-service tests remain passing ─────────────────────
# (verified by running the full sqlservice suite together — see the
# Phase 2 Step 3 report; not re-implemented as its own test here.)


# ── 19. Synthetic timing report (not a pass/fail benchmark) ───────────────

def _build_synthetic_index(num_docs=1400, dims=1536, seed=1234) -> VectorIndex:
    rng = random.Random(seed)
    documents = []
    for i in range(num_docs):
        raw = [rng.uniform(-1.0, 1.0) for _ in range(dims)]
        norm = math.sqrt(sum(v * v for v in raw))
        vector = tuple(v / norm for v in raw)
        documents.append(
            IndexedSchemaDocument(
                object_fqn=f"dbo.table_{i:05d}",
                object_type="TABLE",
                retrieval_tier=(i % 4) + 1,
                document_hash=hashlib.sha256(f"synthetic-doc-{i}".encode()).hexdigest(),
                document_text=f"OBJECT: dbo.table_{i:05d}\nTYPE: TABLE\nTIER: {(i % 4) + 1}\nCOLUMNS:\n  id INTEGER [PK]",
                metadata=MappingProxyType({"column_count": i % 20}),
                vector=vector,
            )
        )
    identity = VectorIndexIdentity(
        build_id=999999, embedding_provider="openai", embedding_model="text-embedding-3-small",
        embedding_dimensions=dims,
    )
    return VectorIndex(identity=identity, documents=tuple(documents))


def test_warm_search_timing_synthetic_1406_documents():
    """
    Timing REPORT, not a performance benchmark: machine speed varies too
    much across CI/dev hardware for a tight millisecond assertion to be
    reliable, so the only hard assertion here is a very generous upper
    bound meant to catch a true catastrophic regression (e.g. an
    accidental O(n^2) blowup or accidental I/O reintroduced into the
    scoring loop) — never a tight target. The actual number is printed for
    the report.
    """
    index = _build_synthetic_index(num_docs=1400, dims=1536)
    rng = random.Random(999)
    query = [rng.uniform(-1.0, 1.0) for _ in range(1536)]

    search_vector_index(index, query, top_k=10)  # warm-up

    runs = 20
    start = time.perf_counter()
    for _ in range(runs):
        results = search_vector_index(index, query, top_k=10)
    elapsed_seconds = time.perf_counter() - start
    per_search_ms = (elapsed_seconds / runs) * 1000

    print(
        f"\n[timing] warm cosine search over {len(index.documents)} documents "
        f"x {index.identity.embedding_dimensions} dims: "
        f"{per_search_ms:.2f} ms/search (average of {runs} runs)"
    )

    assert len(results) == 10
    assert per_search_ms < 2000  # generous sanity bound only — not a benchmark assertion
