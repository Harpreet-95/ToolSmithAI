"""
Focused tests — Phase 7, Step 3C, Part 4: confirmed relationship completion
(core/sqlservice/schema_relationship_completion.py).

Reuses the SQLite fixture plumbing already established in tests/
sqlservice_fixtures.py. No real OpenAI call anywhere in this
file -- complete_relationships() never calls an AI provider at all.
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
from core.sqlservice.schema_relationship_completion import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    complete_relationships,
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


def _doc(object_fqn, *, tier=1, confirmed_relationship_targets=()):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\n"
    doc_hash = hashlib.sha256((text + object_fqn).encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    metadata = {"column_count": 2, "column_names": ("id", "name")}
    if confirmed_relationship_targets:
        metadata["confirmed_relationship_targets"] = tuple(confirmed_relationship_targets)
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


# ── 1. Trivial single-object selection ───────────────────────────────────


def test_single_object_is_trivially_connected(db_path):
    build = _build_with_docs(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()

    result = complete_relationships(["dbo.widgets"], build.id, SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_CONNECTED
    assert result.connector_object_fqns == ()


# ── 2. Directly confirmed pair -- no connector needed ────────────────────


def test_directly_related_pair_needs_no_connector(db_path):
    docs = [
        _doc("dbo.widgets", confirmed_relationship_targets=("dbo.warehouses.id",)),
        _doc("dbo.warehouses"),
    ]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = complete_relationships(["dbo.widgets", "dbo.warehouses"], build.id, SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_CONNECTED
    assert result.connector_object_fqns == ()


# ── 3. Confirmed connector added (bridge) ────────────────────────────────


def test_bridge_object_added_as_required_connector(db_path):
    docs = [
        _doc("dbo.widgets", confirmed_relationship_targets=("dbo.bridge.id",)),
        _doc("dbo.bridge", confirmed_relationship_targets=("dbo.warehouses.id",)),
        _doc("dbo.warehouses"),
    ]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = complete_relationships(["dbo.widgets", "dbo.warehouses"], build.id, SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_CONNECTED
    assert result.connector_object_fqns == ("dbo.bridge",)
    assert "dbo.bridge" not in result.selected_object_fqns  # distinct from the AI's own selection


# ── 4. Disconnected selection ────────────────────────────────────────────


def test_unconnectable_pair_is_disconnected(db_path):
    docs = [_doc("dbo.widgets"), _doc("dbo.unrelated")]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = complete_relationships(["dbo.widgets", "dbo.unrelated"], build.id, SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_DISCONNECTED
    assert result.connector_object_fqns == ()
    assert result.error is not None


# ── 5. max_connectors bound ───────────────────────────────────────────────


def test_max_connectors_bound_is_enforced(db_path):
    docs = [
        _doc("dbo.a", confirmed_relationship_targets=("dbo.bridge.id",)),
        _doc("dbo.bridge", confirmed_relationship_targets=("dbo.b.id",)),
        _doc("dbo.b"),
    ]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = complete_relationships(
        ["dbo.a", "dbo.b"], build.id, SOURCE_ID, USER_ID, provider, max_connectors=0,
    )
    assert result.status == STATUS_DISCONNECTED


# ── 6. Tier-3 neighbor excluded from the graph ───────────────────────────


def test_tier3_bridge_object_is_not_usable_as_a_connector(db_path):
    docs = [
        _doc("dbo.a", confirmed_relationship_targets=("dbo.bridge_archive.id",)),
        _doc("dbo.bridge_archive", tier=3, confirmed_relationship_targets=("dbo.b.id",)),
        _doc("dbo.b"),
    ]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = complete_relationships(["dbo.a", "dbo.b"], build.id, SOURCE_ID, USER_ID, provider)
    assert result.status == STATUS_DISCONNECTED


# ── 7. Duplicate/blank input validation ──────────────────────────────────


def test_duplicate_selected_fqns_rejected(db_path):
    build = _build_with_docs(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()
    with pytest.raises(ValueError, match="duplicate"):
        complete_relationships(["dbo.widgets", "dbo.widgets"], build.id, SOURCE_ID, USER_ID, provider)


# ── 8. Phase 7, Step 3F, Part 4 — selected vs. connector observability ────


def test_connector_only_ever_lies_on_the_actual_confirmed_path(db_path):
    """A connector can never 'falsely satisfy' an unsupported business
    requirement: it only ever enters connector_object_fqns because it sits
    ON the shortest confirmed path complete_relationships() itself found
    between two already AI-selected objects -- never added speculatively,
    never for an object that isn't structurally necessary for
    connectivity, and always kept disjoint from the caller's own selection
    for observability."""
    docs = [
        _doc("dbo.widgets", confirmed_relationship_targets=("dbo.bridge.id",)),
        _doc("dbo.bridge", confirmed_relationship_targets=("dbo.warehouses.id",)),
        _doc("dbo.warehouses"),
        _doc("dbo.unrelated"),
    ]
    build = _build_with_docs(db_path, docs)
    provider = FakeEmbeddingProvider()

    result = complete_relationships(["dbo.widgets", "dbo.warehouses"], build.id, SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_CONNECTED
    assert result.connector_object_fqns == ("dbo.bridge",)
    assert "dbo.unrelated" not in result.connector_object_fqns  # never a speculative addition
    assert set(result.selected_object_fqns).isdisjoint(result.connector_object_fqns)
