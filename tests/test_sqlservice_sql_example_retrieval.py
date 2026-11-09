"""
Focused tests — SQL rebuild Phase 5, Step 9: SQL-example vector indexing
and dynamic few-shot retrieval (core/sqlservice/sql_example_vector_index.py,
core/sqlservice/sql_example_retrieval.py).

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db, never a real embedding provider.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_example_retrieval.py -v
"""

from __future__ import annotations

import hashlib
import inspect
import os
import shutil
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step9-retrieval-secret-long-enough-1234567")
os.environ.setdefault("USER_ID_SALT", "test-step9-retrieval-salt-long-enough-12345678")

import data.db as data_db_module
import data.models as models_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
from core.sqlservice.sql_example_candidate import build_trusted_example_candidate
from core.sqlservice.sql_example_embedding_store import insert_example_embeddings
from core.sqlservice.sql_example_model import (
    PROVENANCE_DEVELOPER_VERIFICATION_SET,
    RESULT_SHAPE_COUNT,
    RESULT_SHAPE_LIST,
    STATUS_STALE,
    TRUST_LEVEL_TRUSTED,
    TRUST_LEVEL_UNTRUSTED,
    build_example_search_document,
    compute_content_hash,
)
from core.sqlservice.sql_example_retrieval import (
    STATUS_NO_EXAMPLES,
    STATUS_OK as RETRIEVAL_STATUS_OK,
    retrieve_sql_examples,
)
from core.sqlservice.sql_example_store import insert_trusted_example, insert_untrusted_observation, update_example_status
from core.sqlservice.sql_example_vector_index import (
    STATUS_EMPTY,
    STATUS_OK as INDEX_STATUS_OK,
    clear_sql_example_vector_index_cache,
    load_sql_example_vector_index,
    sql_example_vector_index_cache_size,
)
from core.sqlservice.sql_validation import STATUS_OK as VALIDATION_STATUS_OK
from core.sqlservice.sql_validation import SqlValidationResult


# ── Fixture plumbing ────────────────────────────────────────────────────────

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
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_cache():
    clear_sql_example_vector_index_cache()
    yield
    clear_sql_example_vector_index_cache()


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


def _add_example(
    source_id, question, objects, *, vector, trust_level=TRUST_LEVEL_TRUSTED,
    result_shape=RESULT_SHAPE_COUNT, join_path=(), user_id="alice",
):
    """Step 9B: there is no raw-field insert path any more. A TRUSTED
    example goes through the real gate (build_trusted_example_candidate,
    requiring a real, OK SqlValidationResult) and insert_trusted_example();
    an UNTRUSTED one uses the separate, explicit
    insert_untrusted_observation() API instead — exactly the two-API split
    core/sqlservice/sql_example_store.py now enforces."""
    if trust_level == TRUST_LEVEL_TRUSTED:
        validation_result = SqlValidationResult(
            status=VALIDATION_STATUS_OK, sql="SELECT 1", canonical_referenced_object_fqns=tuple(objects),
            statement_type="Select", build_id=1, schema_fingerprint="fp1", warnings=(), error=None,
        )
        candidate = build_trusted_example_candidate(
            source_id=source_id, provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET, validation_result=validation_result,
            question_pattern=question, result_shape=result_shape, join_path=join_path,
        )
        result = insert_trusted_example(user_id, candidate)
        doc_hash = candidate.search_document_hash
    else:
        search_doc = build_example_search_document(
            question_pattern=question, canonical_object_fqns=objects, join_path=join_path, result_shape=result_shape,
        )
        doc_hash = hashlib.sha256(search_doc.encode()).hexdigest()
        content_hash = compute_content_hash(
            source_id=source_id, question_pattern=question, sql_template="SELECT 1", canonical_object_fqns=objects,
        )
        result = insert_untrusted_observation(
            user_id, source_id=source_id, schema_fingerprint="fp1", question_pattern=question,
            sql_template="SELECT 1", canonical_object_fqns=objects, join_path=join_path, result_shape=result_shape,
            search_document_text=search_doc, search_document_hash=doc_hash, content_hash=content_hash,
        )

    example = result.example
    insert_example_embeddings(source_id, user_id, "openai", "m", len(vector), {doc_hash: vector})
    return example


class _FakeSchemaDoc:
    def __init__(self, object_fqn, tier=1):
        self.object_fqn = object_fqn
        self.retrieval_tier = tier


# ── Vector index: trust/status enforcement at index-build time ────────────

def test_untrusted_example_never_enters_the_index(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "q1", ["dbo.A"], vector=[1.0, 0.0], trust_level=TRUST_LEVEL_TRUSTED)
    _add_example(1, "q2", ["dbo.B"], vector=[0.0, 1.0], trust_level=TRUST_LEVEL_UNTRUSTED)

    result = load_sql_example_vector_index(1, "alice", "openai", "m", 2)
    assert result.status == INDEX_STATUS_OK
    fqns = {tuple(ex.canonical_object_fqns) for ex in result.index.examples}
    assert ("dbo.A",) in fqns
    assert ("dbo.B",) not in fqns


def test_stale_example_never_enters_the_index(db_path):
    _insert_source(db_path, 1)
    ex = _add_example(1, "q1", ["dbo.A"], vector=[1.0, 0.0])
    update_example_status(ex.id, 1, "alice", status=STATUS_STALE)

    result = load_sql_example_vector_index(1, "alice", "openai", "m", 2)
    assert result.status == STATUS_EMPTY


def test_index_empty_when_no_trusted_examples(db_path):
    _insert_source(db_path, 1)
    result = load_sql_example_vector_index(1, "alice", "openai", "m", 2)
    assert result.status == STATUS_EMPTY


def test_index_cache_hit(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "q1", ["dbo.A"], vector=[1.0, 0.0])
    load_sql_example_vector_index(1, "alice", "openai", "m", 2)
    assert sql_example_vector_index_cache_size() == 1
    load_sql_example_vector_index(1, "alice", "openai", "m", 2)
    assert sql_example_vector_index_cache_size() == 1


# ── Retrieval: deterministic top-k, similarity threshold, compatibility ───

def test_deterministic_top_k_ranking(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "closest", ["dbo.A"], vector=[1.0, 0.0, 0.0])
    _add_example(1, "middle", ["dbo.B"], vector=[0.9, 0.1, 0.0])
    _add_example(1, "far", ["dbo.C"], vector=[0.0, 0.0, 1.0])
    schema_docs = {"dbo.a": _FakeSchemaDoc("dbo.A"), "dbo.b": _FakeSchemaDoc("dbo.B"), "dbo.c": _FakeSchemaDoc("dbo.C")}

    result = retrieve_sql_examples(
        [1.0, 0.0, 0.0], 1, "alice", "openai", "m", 3, schema_docs,
        allowed_tiers=frozenset({1, 2}), max_examples=3, min_similarity=0.0,
    )
    assert result.status == RETRIEVAL_STATUS_OK
    ordered = [r.example.question_pattern for r in result.examples]
    assert ordered == ["closest", "middle", "far"]


def test_min_similarity_threshold_excludes_weak_matches(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "orthogonal", ["dbo.A"], vector=[0.0, 1.0])
    schema_docs = {"dbo.a": _FakeSchemaDoc("dbo.A")}

    result = retrieve_sql_examples(
        [1.0, 0.0], 1, "alice", "openai", "m", 2, schema_docs,
        allowed_tiers=frozenset({1, 2}), min_similarity=0.5,
    )
    assert result.status == STATUS_NO_EXAMPLES


def test_at_most_three_examples_returned(db_path):
    _insert_source(db_path, 1)
    for i in range(5):
        _add_example(1, f"q{i}", [f"dbo.T{i}"], vector=[1.0, float(i) * 0.001])
    schema_docs = {f"dbo.t{i}": _FakeSchemaDoc(f"dbo.T{i}") for i in range(5)}

    result = retrieve_sql_examples(
        [1.0, 0.0], 1, "alice", "openai", "m", 2, schema_docs,
        allowed_tiers=frozenset({1, 2}), min_similarity=0.0,
    )
    assert len(result.examples) <= 3


def test_max_examples_above_three_rejected(db_path):
    _insert_source(db_path, 1)
    with pytest.raises(ValueError):
        retrieve_sql_examples(
            [1.0, 0.0], 1, "alice", "openai", "m", 2, {}, allowed_tiers=frozenset({1, 2}), max_examples=4,
        )


def test_example_object_no_longer_existing_is_incompatible(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "q1", ["dbo.RenamedOrDropped"], vector=[1.0, 0.0])
    schema_docs = {}  # nothing exists in the current schema

    result = retrieve_sql_examples(
        [1.0, 0.0], 1, "alice", "openai", "m", 2, schema_docs, allowed_tiers=frozenset({1, 2}), min_similarity=0.0,
    )
    assert result.status == STATUS_NO_EXAMPLES


def test_tier_enforcement_excludes_tier34_objects_from_proposal(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "q1", ["dbo.Archived"], vector=[1.0, 0.0])
    schema_docs = {"dbo.archived": _FakeSchemaDoc("dbo.Archived", tier=4)}

    result = retrieve_sql_examples(
        [1.0, 0.0], 1, "alice", "openai", "m", 2, schema_docs, allowed_tiers=frozenset({1, 2}), min_similarity=0.0,
    )
    assert result.status == STATUS_NO_EXAMPLES  # its only object is tier-excluded -> zero surviving proposed objects


def test_partial_object_survival_still_compatible(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "q1", ["dbo.Still", "dbo.Gone"], vector=[1.0, 0.0])
    schema_docs = {"dbo.still": _FakeSchemaDoc("dbo.Still", tier=1)}  # dbo.Gone absent

    result = retrieve_sql_examples(
        [1.0, 0.0], 1, "alice", "openai", "m", 2, schema_docs, allowed_tiers=frozenset({1, 2}), min_similarity=0.0,
    )
    assert result.status == RETRIEVAL_STATUS_OK
    assert result.examples[0].proposed_object_fqns == ("dbo.Still",)
    assert result.examples[0].unavailable_object_fqns == ("dbo.Gone",)


def test_result_shape_filter(db_path):
    _insert_source(db_path, 1)
    _add_example(1, "count_q", ["dbo.A"], vector=[1.0, 0.0], result_shape=RESULT_SHAPE_COUNT)
    _add_example(1, "list_q", ["dbo.B"], vector=[1.0, 0.0], result_shape=RESULT_SHAPE_LIST)
    schema_docs = {"dbo.a": _FakeSchemaDoc("dbo.A"), "dbo.b": _FakeSchemaDoc("dbo.B")}

    result = retrieve_sql_examples(
        [1.0, 0.0], 1, "alice", "openai", "m", 2, schema_docs, allowed_tiers=frozenset({1, 2}),
        min_similarity=0.0, expected_result_shape=RESULT_SHAPE_LIST,
    )
    assert all(r.example.result_shape == RESULT_SHAPE_LIST for r in result.examples)


# ── Query-vector reuse: zero additional embedding call ─────────────────────

def test_retrieve_sql_examples_has_no_embedding_provider_parameter():
    """Structural guard: this function cannot possibly make a second
    embedding call because it accepts no question text and no live
    EmbeddingProvider object at all — `embedding_provider`/`embedding_model`
    are plain identity STRINGS (cache/lookup keys, same shape as every
    other *_provider/*_model string param in this codebase), never
    something with an embed_batch() method to call."""
    sig = inspect.signature(retrieve_sql_examples)
    names = set(sig.parameters)
    assert "question" not in names
    assert {"embedding_provider", "embedding_model", "embedding_dimensions"}.issubset(names)
    for name in names:
        assert "question" not in name.lower()


def test_retrieve_sql_examples_never_imports_an_embedding_provider_module():
    import core.sqlservice.sql_example_retrieval as retrieval_module
    source = inspect.getsource(retrieval_module)
    assert "embedding_providers" not in source
    assert "embed_batch" not in source
