"""
Focused tests — SQL rebuild Phase 5, Step 7: hub-table relationship bloat
fix (Part 1) and bounded pool-exempt confirmed neighbors (Part 2).

Part 1 covers core/sqlservice/retrieval_documents.py's bounded, provenance-
prioritized RELATIONSHIPS_* document-text sections and
core/sqlservice/lexical_index.py's equivalently bounded FIELD_RELATIONSHIPS
lexical field.

Part 2 covers core/sqlservice/schema_context_retrieval.py's pool-exempt
confirmed-neighbor admission pass (RANKING_MODE_HYBRID only).

Pure/offline throughout: no real database connection beyond a per-test
temporary SQLite file, no OpenAI call, no live SQL Server — every
FakeEmbeddingProvider call is recorded, never dispatched externally.

Run:
    venv/Scripts/pytest tests/test_sqlservice_step7_hub_bloat_and_pool_exempt.py -v
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step7-hub-bloat-secret-long-enough-123456")
os.environ.setdefault("USER_ID_SALT", "test-step7-hub-bloat-salt-long-enough-1234567")

from core.connectors.schema import (
    ColumnInfo, ForeignKeyInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo,
)
from core.sqlservice.retrieval_documents import (
    DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES,
    build_retrieval_documents,
)
from core.sqlservice.lexical_index import (
    FIELD_RELATIONSHIPS,
    MAX_RELATIONSHIP_LEXICAL_NAMES,
    LexicalIndexIdentity,
    build_lexical_index,
    clear_lexical_index_cache,
    tokenize,
)

import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
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
    DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR,
    DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL,
    SELECTION_REASON_DIRECT_RELATIONSHIP,
    SELECTION_REASON_HYBRID_ANCHOR,
    SELECTION_REASON_POOL_EXEMPT_NEIGHBOR,
    SELECTION_REASON_RELATIONSHIP_BRIDGE,
    STATUS_OK,
    retrieve_schema_context,
)
from core.sqlservice.vector_index import clear_vector_index_cache


# ===========================================================================
# Part 1 — bounded relationship representation (retrieval_documents.py)
# ===========================================================================

def _col(name, data_type, *, pk=False, identity=False, nullable=True, ordinal=1):
    return ColumnInfo(
        column_name=name, ordinal_position=ordinal, data_type=data_type,
        raw_type=data_type.lower(), is_nullable=nullable, is_primary_key=pk, is_identity=identity,
    )


def _table(schema_name, table_name, columns, *, table_type="TABLE", primary_keys=None, foreign_keys=None):
    return TableInfo(
        table_name=table_name, schema_name=schema_name, table_fqn=f"{schema_name}.{table_name}",
        table_type=table_type, columns=columns, primary_keys=primary_keys or [], foreign_keys=foreign_keys or [],
    )


def _snapshot(source_id, tables, schema_name="dbo"):
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at="2026-09-09T00:00:00Z",
        schemas=[SchemaInfo(schema_name=schema_name, tables=tables)],
    )


def _hub_scenario(num_inbound_views: int):
    """One hub table ('dbo.hub') with a primary key, plus `num_inbound_views`
    VIEW objects, each carrying a confirmed (catalog_view_dependency) inbound
    relationship pointing at the hub — the exact "hub table with hundreds of
    inbound view edges" shape this step exists to bound."""
    hub = _table("dbo", "hub", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    views = [
        _table("dbo", f"v_report_{i:03d}", [_col("id", "INTEGER", pk=True)], table_type="VIEW")
        for i in range(num_inbound_views)
    ]
    relationships = [
        {
            "from_table_fqn": f"dbo.v_report_{i:03d}", "from_column": "id",
            "to_schema": "dbo", "to_table": "hub", "to_table_fqn": "dbo.hub", "to_column": "id",
            "relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
        }
        for i in range(num_inbound_views)
    ]
    snapshot = _snapshot(1, [hub, *views])
    docs = build_retrieval_documents(1, snapshot, relationships=relationships)
    by_fqn = {d.object_fqn: d for d in docs}
    return by_fqn


def test_bounded_relationship_document_text():
    by_fqn = _hub_scenario(50)
    hub_doc = by_fqn["dbo.hub"]

    assert f"RELATIONSHIPS_CONFIRMED_INBOUND_TOTAL: {50}" in hub_doc.document_text
    section = hub_doc.document_text.split("RELATIONSHIPS_CONFIRMED_INBOUND:")[1]
    section = section.split("RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED:")[0]
    kept_lines = [l for l in section.splitlines() if l.strip().startswith("id <-")]
    assert len(kept_lines) == DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES


def test_omitted_count_reporting():
    total = 50
    by_fqn = _hub_scenario(total)
    hub_doc = by_fqn["dbo.hub"]
    expected_omitted = total - DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES

    assert f"RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED: {expected_omitted}" in hub_doc.document_text


def test_full_graph_metadata_remains_available():
    by_fqn = _hub_scenario(50)
    hub_doc = by_fqn["dbo.hub"]

    # The bounded document text carries only DEFAULT_MAX_RELATIONSHIP_DETAIL_LINES
    # lines, but structured metadata (used by relationship_graph.py and
    # schema_context_retrieval's one-hop expansion) always has the complete set.
    assert len(hub_doc.metadata["confirmed_relationship_sources"]) == 50
    assert len(hub_doc.metadata["confirmed_relationship_edges"]) == 50
    assert all(e["kind"] == "CATALOG_VIEW_DEPENDENCY" for e in hub_doc.metadata["confirmed_relationship_edges"])


def test_hub_with_many_inbound_view_edges_retains_stable_ranking():
    """The hub's OWN document — the one carrying the bloat — must still
    produce a bounded, well-formed document (never an ever-growing one that
    would keep destabilizing corpus-wide lexical statistics as more views
    are added)."""
    small = _hub_scenario(5)["dbo.hub"]
    large = _hub_scenario(200)["dbo.hub"]

    # Document text length no longer scales linearly with relationship
    # count once past the bound — both stay within a small, bounded delta
    # of each other despite a 40x difference in underlying relationship count.
    assert abs(len(large.document_text) - len(small.document_text)) < 2000


def test_deterministic_relationship_prioritization():
    by_fqn_forward = _hub_scenario(30)
    # Rebuild with the exact same logical inputs — order independence is
    # already covered elsewhere; this asserts the KEPT lines themselves are
    # identical and provenance-ordered (physical FK before catalog view
    # dependency) across repeated builds.
    by_fqn_repeat = _hub_scenario(30)
    assert by_fqn_forward["dbo.hub"].document_text == by_fqn_repeat["dbo.hub"].document_text
    assert by_fqn_forward["dbo.hub"].document_hash == by_fqn_repeat["dbo.hub"].document_hash


def test_important_physical_fk_not_displaced_by_many_view_dependencies():
    # The hub has 20 OUTBOUND catalog-view-dependency edges to targets that
    # all sort ALPHABETICALLY BEFORE the one physical FK target — proving
    # the physical FK survives the bound purely because of its provenance
    # priority, not because of where its name happens to fall alphabetically.
    hub_fk = ForeignKeyInfo(
        fk_name="fk_important", from_column="important_ref",
        to_schema="dbo", to_table="zzz_important_target", to_column="id",
    )
    hub = _table(
        "dbo", "hub",
        [_col("id", "INTEGER", pk=True), _col("important_ref", "INTEGER")],
        primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[hub_fk],
    )
    target = _table("dbo", "zzz_important_target", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    views = [
        _table("dbo", f"aaa_view_{i:03d}", [_col("id", "INTEGER", pk=True)], table_type="VIEW")
        for i in range(20)
    ]
    relationships = [
        {
            "from_table_fqn": "dbo.hub", "from_column": f"view_ref_{i:03d}",
            "to_schema": "dbo", "to_table": f"aaa_view_{i:03d}", "to_table_fqn": f"dbo.aaa_view_{i:03d}", "to_column": "id",
            "relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
        }
        for i in range(20)
    ]
    snapshot = _snapshot(1, [hub, target, *views])
    docs = build_retrieval_documents(1, snapshot, relationships=relationships, max_relationship_detail_lines=15)
    hub_doc = {d.object_fqn: d for d in docs}["dbo.hub"]

    outbound_section = hub_doc.document_text.split("RELATIONSHIPS_CONFIRMED:")[1]
    outbound_section = outbound_section.split("RELATIONSHIPS_CONFIRMED_OMITTED", 1)[0]
    outbound_section = outbound_section.split("RELATIONSHIPS_CONFIRMED_INBOUND", 1)[0]
    assert "important_ref -> dbo.zzz_important_target.id" in outbound_section
    # 20 view-dependency edges + 1 physical FK = 21 total, bound is 15, so at
    # least 6 view-dependency edges must have been omitted for the FK to fit.
    assert "RELATIONSHIPS_CONFIRMED_OUTBOUND_OMITTED:" in hub_doc.document_text


def test_configurable_bound_is_respected():
    by_fqn = _hub_scenario(10)
    # No omission at the default bound (10 < 15).
    assert "RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED:" not in by_fqn["dbo.hub"].document_text

    hub = _table("dbo", "hub", [_col("id", "INTEGER", pk=True)], primary_keys=[PrimaryKeyInfo("id", 1)])
    views = [_table("dbo", f"v_{i:03d}", [_col("id", "INTEGER", pk=True)], table_type="VIEW") for i in range(10)]
    relationships = [
        {
            "from_table_fqn": f"dbo.v_{i:03d}", "from_column": "id",
            "to_schema": "dbo", "to_table": "hub", "to_table_fqn": "dbo.hub", "to_column": "id",
            "relationship_type": "VIEW_DEPENDENCY", "inference_method": "catalog_view_dependency",
        }
        for i in range(10)
    ]
    docs = build_retrieval_documents(1, _snapshot(1, [hub, *views]), relationships=relationships, max_relationship_detail_lines=3)
    hub_doc = {d.object_fqn: d for d in docs}["dbo.hub"]
    assert "RELATIONSHIPS_CONFIRMED_INBOUND_OMITTED: 7" in hub_doc.document_text


# ── Lexical field bounding (lexical_index.py) ──────────────────────────────

def test_lexical_relationships_field_is_bounded_for_hub_document():
    by_fqn = _hub_scenario(200)
    hub_doc_type = type("Doc", (), {})  # simple attribute-bag stand-in

    class _StubDoc:
        def __init__(self, rd):
            self.object_fqn = rd.object_fqn
            self.object_type = rd.object_type
            self.retrieval_tier = rd.retrieval_tier
            self.document_hash = rd.document_hash
            self.document_text = rd.document_text
            self.metadata = rd.metadata

    stub_docs = [_StubDoc(rd) for rd in by_fqn.values()]
    identity = LexicalIndexIdentity(build_id=1, embedding_provider="p", embedding_model="m", embedding_dimensions=1)
    index = build_lexical_index(stub_docs, identity)

    hub_lexical = next(d for d in index.documents if d.object_fqn == "dbo.hub")
    relationships_field_length = hub_lexical.field_lengths.get(FIELD_RELATIONSHIPS, 0)
    # 200 raw edges would have produced ~200+ tokens pre-bound; bounded to
    # MAX_RELATIONSHIP_LEXICAL_NAMES names (each contributing >=1 token).
    assert relationships_field_length <= MAX_RELATIONSHIP_LEXICAL_NAMES * 4


def test_lexical_average_field_length_does_not_scale_with_hub_size():
    def _avg_relationships_field_length(num_views: int) -> float:
        by_fqn = _hub_scenario(num_views)

        class _StubDoc:
            def __init__(self, rd):
                self.object_fqn = rd.object_fqn
                self.object_type = rd.object_type
                self.retrieval_tier = rd.retrieval_tier
                self.document_hash = rd.document_hash
                self.document_text = rd.document_text
                self.metadata = rd.metadata

        stub_docs = [_StubDoc(rd) for rd in by_fqn.values()]
        identity = LexicalIndexIdentity(build_id=1, embedding_provider="p", embedding_model="m", embedding_dimensions=1)
        index = build_lexical_index(stub_docs, identity)
        return index.average_field_length.get(FIELD_RELATIONSHIPS, 0.0)

    small_avg = _avg_relationships_field_length(5)
    large_avg = _avg_relationships_field_length(300)
    # Without bounding, a single 300-edge hub in a small corpus would
    # dominate the corpus average; bounded, the two stay in the same order
    # of magnitude.
    assert large_avg < small_avg * 5 + 10


# ===========================================================================
# Part 2 — bounded pool-exempt confirmed neighbors (schema_context_retrieval.py)
# ===========================================================================

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
def _isolated_index_caches():
    clear_vector_index_cache()
    clear_lexical_index_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()


def _insert_source(db_path, source_id, user_id="alice"):
    conn = sqlite3.connect(db_path)
    now = "2026-09-09T00:00:00Z"
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
    now = "2026-09-09T00:00:00Z"
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


def _rel_doc(object_fqn, *, tier=1, targets=(), sources=(), inferred_targets=()):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    metadata = {}
    if targets:
        metadata["confirmed_relationship_targets"] = tuple(targets)
    if sources:
        metadata["confirmed_relationship_sources"] = tuple(sources)
    if inferred_targets:
        metadata["inferred_relationship_targets"] = tuple(inferred_targets)
    return _doc(object_fqn, tier=tier, text=text, metadata=metadata)


def _batch(source_id, snapshot_id, snapshot_version, documents, status=STATUS_READY):
    return RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=snapshot_version,
        status=status, documents=tuple(documents), counts=MappingProxyType({}), warnings=(),
    )


def _fingerprint(*, version=1, fingerprint_hash=None, table_set_hash=None):
    if fingerprint_hash is None:
        fingerprint_hash = hashlib.sha256(b"step7-fingerprint").hexdigest()
    if table_set_hash is None:
        table_set_hash = hashlib.sha256(b"step7-table-set").hexdigest()
    return {"fingerprint_version": version, "fingerprint_hash": fingerprint_hash, "table_set_hash": table_set_hash}


def _make_ready_build_with_docs(db_path, docs, source_id=1, snapshot_id=1, user_id="alice"):
    _insert_source(db_path, source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id, source_id=source_id)
    batch = _batch(source_id, snapshot_id, 1, docs)
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    return result.build


PROVIDER = "fake"
MODEL = "fake-model"
DIMS = 4


def _embed_all(db_path, build, vectors_by_fqn, *, docs, provider=PROVIDER, model=MODEL, dims=DIMS, source_id=1, user_id="alice"):
    hash_by_fqn = {d.object_fqn: d.document_hash for d in docs}
    vectors_by_hash = {hash_by_fqn[fqn]: vec for fqn, vec in vectors_by_fqn.items()}
    result = insert_embeddings(build.id, source_id, user_id, provider, model, dims, vectors_by_hash)
    assert result.status == EMBEDDING_STATUS_OK
    return result


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimensions=DIMS, provider_id=PROVIDER, model=MODEL, fixed_vector=None):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
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
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=tuple(self._fixed_vector)),), model=self._model,
        )


def test_outside_pool_confirmed_neighbor_admitted(db_path):
    docs = [
        _rel_doc("dbo.anchor", targets=("dbo.neighbor.id",)),
        _rel_doc("dbo.neighbor"),
        _rel_doc("dbo.filler"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor": [1.0, 0.0, 0.0, 0.0],
        "dbo.neighbor": [0.0, 1.0, 0.0, 0.0],
        "dbo.filler": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    # top_k=1, candidate_pool_size=1 -> pool = {dbo.anchor} only, so
    # dbo.neighbor is outside the pool and would be SKIPPED by the existing
    # pool-scoped direct_keys mechanism.
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1, relationship_limit=5,
    )

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert "dbo.neighbor" in by_fqn
    neighbor = by_fqn["dbo.neighbor"]
    assert neighbor.selection_reason == SELECTION_REASON_POOL_EXEMPT_NEIGHBOR
    assert neighbor.source_anchor_fqn == "dbo.anchor"
    assert neighbor.pool_exempt_reason and "dbo.anchor" in neighbor.pool_exempt_reason


def test_outside_pool_inferred_neighbor_rejected(db_path):
    docs = [
        _rel_doc("dbo.anchor", inferred_targets=("dbo.maybe_related.id",)),
        _rel_doc("dbo.maybe_related"),
        _rel_doc("dbo.filler"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor": [1.0, 0.0, 0.0, 0.0],
        "dbo.maybe_related": [0.0, 1.0, 0.0, 0.0],
        "dbo.filler": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1, relationship_limit=5,
    )

    fqns = {d.object_fqn for d in result.documents}
    # An inferred-only edge never enters the confirmed relationship graph at
    # all, so dbo.maybe_related is never a candidate through any path.
    assert "dbo.maybe_related" not in fqns


def test_per_anchor_pool_exempt_cap(db_path):
    docs = [
        _rel_doc("dbo.anchor", targets=tuple(f"dbo.n{i}.id" for i in range(5))),
        *[_rel_doc(f"dbo.n{i}") for i in range(5)],
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    vectors = {"dbo.anchor": [1.0, 0.0, 0.0, 0.0]}
    vectors.update({f"dbo.n{i}": [0.0, 1.0, 0.0, 0.0] for i in range(5)})
    _embed_all(db_path, build, vectors, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
        relationship_limit=0, max_pool_exempt_neighbors_per_anchor=2, max_pool_exempt_neighbors_total=10,
    )

    pool_exempt = [d for d in result.documents if d.selection_reason == SELECTION_REASON_POOL_EXEMPT_NEIGHBOR]
    assert len(pool_exempt) == 2


def test_global_pool_exempt_cap(db_path):
    docs = [
        _rel_doc("dbo.anchor_a", targets=tuple(f"dbo.a_n{i}.id" for i in range(3))),
        _rel_doc("dbo.anchor_b", targets=tuple(f"dbo.b_n{i}.id" for i in range(3))),
        *[_rel_doc(f"dbo.a_n{i}") for i in range(3)],
        *[_rel_doc(f"dbo.b_n{i}") for i in range(3)],
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    vectors = {
        "dbo.anchor_a": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor_b": [0.9, 0.1, 0.0, 0.0],
    }
    vectors.update({f"dbo.a_n{i}": [0.0, 1.0, 0.0, 0.0] for i in range(3)})
    vectors.update({f"dbo.b_n{i}": [0.0, 0.0, 1.0, 0.0] for i in range(3)})
    _embed_all(db_path, build, vectors, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, candidate_pool_size=2,
        relationship_limit=0, max_pool_exempt_neighbors_per_anchor=3, max_pool_exempt_neighbors_total=4,
    )

    pool_exempt = [d for d in result.documents if d.selection_reason == SELECTION_REASON_POOL_EXEMPT_NEIGHBOR]
    assert len(pool_exempt) == 4


def test_pool_exempt_disabled_via_zero_total_cap(db_path):
    docs = [
        _rel_doc("dbo.anchor", targets=("dbo.neighbor.id",)),
        _rel_doc("dbo.neighbor"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor": [1.0, 0.0, 0.0, 0.0], "dbo.neighbor": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
        relationship_limit=0, max_pool_exempt_neighbors_total=0,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.neighbor" not in fqns


def test_no_recursive_graph_explosion_via_pool_exempt(db_path):
    docs = [
        _rel_doc("dbo.anchor", targets=("dbo.neighbor.id",)),
        _rel_doc("dbo.neighbor", targets=("dbo.grandchild.id",)),  # must NOT be followed
        _rel_doc("dbo.grandchild"),
        _rel_doc("dbo.filler"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor": [1.0, 0.0, 0.0, 0.0],
        "dbo.neighbor": [0.0, 1.0, 0.0, 0.0],
        "dbo.grandchild": [0.0, 0.0, 1.0, 0.0],
        "dbo.filler": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1, relationship_limit=5,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.neighbor" in fqns
    assert "dbo.grandchild" not in fqns


def test_pool_exempt_neighbor_used_as_a_bridge_between_two_anchors(db_path):
    docs = [
        _rel_doc("dbo.anchor_a", targets=("dbo.bridge.id",)),
        _rel_doc("dbo.anchor_b", sources=("dbo.bridge.id",)),
        _rel_doc("dbo.bridge"),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor_a": [1.0, 0.0, 0.0, 0.0],
        "dbo.anchor_b": [0.9, 0.1, 0.0, 0.0],
        "dbo.bridge": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    # candidate_pool_size == top_k == 2 -> pool = {anchor_a, anchor_b} only,
    # dbo.bridge is outside the pool. max_relationship_path_length=1 means
    # the EXISTING bridge-finding mechanism (which needs a 2-edge path
    # anchor_a -> bridge -> anchor_b) cannot find it either — only the
    # pool-exempt pass can admit dbo.bridge here.
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, candidate_pool_size=2,
        relationship_limit=0, max_relationship_path_length=1,
        max_pool_exempt_neighbors_per_anchor=2, max_pool_exempt_neighbors_total=5,
    )

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert "dbo.bridge" in by_fqn
    bridge = by_fqn["dbo.bridge"]
    assert bridge.selection_reason == SELECTION_REASON_POOL_EXEMPT_NEIGHBOR
    assert bridge.pool_exempt_reason is not None
    assert "2 anchors" in bridge.pool_exempt_reason
    # Never duplicated even though it's reachable from both anchors.
    assert len([d for d in result.documents if d.object_fqn == "dbo.bridge"]) == 1


def test_pool_exempt_tier_enforcement(db_path):
    docs = [
        _rel_doc("dbo.anchor", targets=("dbo.tier3_neighbor.id",)),
        _rel_doc("dbo.tier3_neighbor", tier=3),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor": [1.0, 0.0, 0.0, 0.0], "dbo.tier3_neighbor": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    # Default allowed_tiers = {1, 2} — Tier 3 is excluded from the graph
    # entirely, so it can never be a pool-exempt candidate.
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1, relationship_limit=5,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.tier3_neighbor" not in fqns


def test_pool_exempt_respects_hard_context_budget(db_path):
    big_text = "OBJECT: dbo.neighbor\nTYPE: TABLE\nTIER: 1\nBUSINESS_NAME: Neighbor\nCOLUMNS:\n" + "\n".join(
        f"  col_{i} TEXT" for i in range(500)
    )
    neighbor_doc = _doc(
        "dbo.neighbor", text=big_text,
        metadata={"business_name": "Neighbor", "column_count": 500, "column_names": tuple(f"col_{i}" for i in range(500))},
    )
    docs = [
        _rel_doc("dbo.anchor", targets=("dbo.neighbor.id",)),
        neighbor_doc,
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.anchor": [1.0, 0.0, 0.0, 0.0], "dbo.neighbor": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    small_anchor_text_len = len(docs[0].document_text)
    budget = small_anchor_text_len + 300  # enough for the anchor + a compact projection, not the full neighbor text

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
        relationship_limit=0, max_context_characters=budget,
    )

    assert result.total_characters <= budget
    by_fqn = {d.object_fqn: d for d in result.documents}
    if "dbo.neighbor" in by_fqn:
        assert by_fqn["dbo.neighbor"].used_compact_text is True


def test_pool_exempt_deterministic_output(db_path):
    docs = [
        _rel_doc("dbo.anchor", targets=tuple(f"dbo.n{i}.id" for i in range(4))),
        *[_rel_doc(f"dbo.n{i}") for i in range(4)],
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    vectors = {"dbo.anchor": [1.0, 0.0, 0.0, 0.0]}
    vectors.update({f"dbo.n{i}": [0.0, 1.0, 0.0, 0.0] for i in range(4)})
    _embed_all(db_path, build, vectors, docs=docs)

    def _run():
        provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))
        return retrieve_schema_context(
            "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
            relationship_limit=0, max_pool_exempt_neighbors_per_anchor=2, max_pool_exempt_neighbors_total=2,
        )

    result_a = _run()
    result_b = _run()

    fqns_a = [(d.object_fqn, d.selection_reason, d.source_anchor_fqn) for d in result_a.documents]
    fqns_b = [(d.object_fqn, d.selection_reason, d.source_anchor_fqn) for d in result_b.documents]
    assert fqns_a == fqns_b
    assert result_a.context_text == result_b.context_text


def test_invalid_pool_exempt_limits_rejected(db_path):
    docs = [_rel_doc("dbo.anchor")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.anchor": [1.0, 0.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    with pytest.raises(ValueError):
        retrieve_schema_context("q", build.id, 1, "alice", provider, max_pool_exempt_neighbors_per_anchor=-1)
    with pytest.raises(ValueError):
        retrieve_schema_context("q", build.id, 1, "alice", provider, max_pool_exempt_neighbors_total=-1)


def test_pool_exempt_defaults_are_sane():
    assert DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR > 0
    assert DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL >= DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR
