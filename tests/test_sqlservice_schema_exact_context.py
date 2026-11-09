"""
Focused tests — Phase 7, Step 3C, Part 5: exact schema-context construction
(core/sqlservice/schema_exact_context.py).

Reuses the SQLite fixture plumbing already established in tests/
sqlservice_fixtures.py. No real OpenAI call anywhere in this
file -- build_exact_schema_context() never calls an AI provider at all.
"""

import hashlib
from types import MappingProxyType

import pytest

import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import data.db as data_db_module
from core.sqlservice.business_concept_index import clear_business_concept_index_cache
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_context_retrieval import STATUS_OK as CONTEXT_STATUS_OK
from core.sqlservice.schema_exact_context import (
    SELECTION_ROLE_AI_SELECTED,
    SELECTION_ROLE_CONNECTOR,
    STATUS_INCOMPLETE,
    build_exact_schema_context,
)
from core.sqlservice.vector_index import clear_vector_index_cache

from tests.sqlservice_fixtures import (
    DIMS,
    MODEL,
    PROVIDER,
    SOURCE_ID,
    USER_ID,
    FakeEmbeddingProvider,
    _insert_snapshot,
    _insert_source,
    _make_connection,
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
    return path


@pytest.fixture(autouse=True)
def _isolated_caches():
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_business_concept_index_cache()
    clear_canonical_scoring_cache()


def _fingerprint():
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(b"fingerprint-a").hexdigest(),
        "table_set_hash": hashlib.sha256(b"table-set-a").hexdigest(),
    }


def _doc(object_fqn, *, tier=1, text_len=60):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\n" + ("COLUMN: id INTEGER [PK]\n" * max(1, text_len // 24))
    doc_hash = hashlib.sha256((text + object_fqn).encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    metadata = {"column_count": 1, "column_names": ("id",), "primary_key_columns": ("id",)}
    return RetrievalDocument(
        source_id=SOURCE_ID, object_fqn=object_fqn, object_type="TABLE",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def _build_with_docs(db_path, docs, *, source_id=SOURCE_ID, user_id=USER_ID):
    _insert_source(db_path, source_id=source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id=source_id, source_id=source_id)
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=source_id, snapshot_version=1,
        status=STATUS_READY, documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    build = result.build
    vectors_by_hash = {d.document_hash: [1.0, 0.0, 0.0, 0.0] for d in docs}
    embed_result = insert_embeddings(build.id, source_id, user_id, PROVIDER, MODEL, DIMS, vectors_by_hash)
    assert embed_result.status == EMBEDDING_STATUS_OK
    return build


# ── 1. Full context built for selected + connector ───────────────────────


def test_full_context_includes_selected_and_connector_with_distinct_roles(db_path):
    docs = [_doc("dbo.widgets"), _doc("dbo.bridge"), _doc("dbo.warehouses")]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = build_exact_schema_context(
        ["dbo.widgets", "dbo.warehouses"], ["dbo.bridge"], build.id, SOURCE_ID, USER_ID, provider,
    )

    assert result.status == CONTEXT_STATUS_OK
    fqns_by_role = {d.object_fqn: d.selection_reason for d in result.documents}
    assert fqns_by_role["dbo.widgets"] == SELECTION_ROLE_AI_SELECTED
    assert fqns_by_role["dbo.warehouses"] == SELECTION_ROLE_AI_SELECTED
    assert fqns_by_role["dbo.bridge"] == SELECTION_ROLE_CONNECTOR
    assert "dbo.widgets" in result.context_text
    assert "dbo.bridge" in result.context_text


def test_no_connectors_is_valid(db_path):
    docs = [_doc("dbo.widgets")]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = build_exact_schema_context(["dbo.widgets"], [], build.id, SOURCE_ID, USER_ID, provider)
    assert result.status == CONTEXT_STATUS_OK
    assert len(result.documents) == 1


# ── 2. Missing essential object -> INCOMPLETE ─────────────────────────────


def test_missing_selected_object_is_incomplete(db_path):
    docs = [_doc("dbo.widgets")]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = build_exact_schema_context(
        ["dbo.widgets", "dbo.not_indexed"], [], build.id, SOURCE_ID, USER_ID, provider,
    )
    assert result.status == STATUS_INCOMPLETE


# ── 3. Character budget too small, even compactly -> INCOMPLETE ─────────


def test_budget_too_small_even_compactly_is_incomplete(db_path):
    docs = [_doc("dbo.widgets", text_len=5000)]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = build_exact_schema_context(
        ["dbo.widgets"], [], build.id, SOURCE_ID, USER_ID, provider, max_context_characters=5,
    )
    assert result.status == STATUS_INCOMPLETE
    assert result.documents == ()


# ── 4. Selected + connector must not overlap ─────────────────────────────


def test_overlapping_selected_and_connector_rejected(db_path):
    docs = [_doc("dbo.widgets")]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    with pytest.raises(ValueError, match="must not overlap"):
        build_exact_schema_context(
            ["dbo.widgets"], ["dbo.widgets"], build.id, SOURCE_ID, USER_ID, provider,
        )
