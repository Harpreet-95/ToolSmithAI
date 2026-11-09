"""
Focused tests — SQL rebuild Phase 5, Step 8: canonical-object scoring as a
bounded relevance prior in core/sqlservice/schema_context_retrieval.py's
hybrid ranking.

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server. Every test injects a FakeEmbeddingProvider
defined in this file — never a real OpenAI call anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_step8_canonical_prior_retrieval.py -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step8-canonical-prior-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-step8-canonical-prior-salt-long-enough12")

import data.models as models_module
import core.sqlservice.canonical_scoring as canonical_scoring_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.embedding_providers import (
    EmbeddingProvider,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_HYBRID_ANCHOR,
    SELECTION_REASON_RELATIONSHIP_BRIDGE,
    retrieve_schema_context,
)
from core.sqlservice.vector_index import clear_vector_index_cache


# ── Fixture plumbing (mirrors tests/test_sqlservice_schema_context_retrieval.py) ──

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
    clear_canonical_scoring_cache()
    yield
    clear_vector_index_cache()
    clear_lexical_index_cache()
    clear_canonical_scoring_cache()


def _insert_source(db_path, source_id, user_id="alice"):
    conn = sqlite3.connect(db_path)
    now = "2026-09-11T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, snapshot_id, source_id, version=1):
    conn = sqlite3.connect(db_path)
    now = "2026-09-11T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
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
    base_meta = {"column_count": 1}
    if metadata:
        base_meta.update(metadata)
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type=object_type,
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash,
        metadata=MappingProxyType(base_meta),
    )


def _batch(source_id, snapshot_id, snapshot_version, documents, status=STATUS_READY):
    return RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=snapshot_version,
        status=status, documents=tuple(documents), counts=MappingProxyType({}), warnings=(),
    )


def _fingerprint(*, version=1, fingerprint_hash=None, table_set_hash=None):
    if fingerprint_hash is None:
        fingerprint_hash = hashlib.sha256(b"step8-fp").hexdigest()
    if table_set_hash is None:
        table_set_hash = hashlib.sha256(b"step8-ts").hexdigest()
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


def _rich_meta(**overrides):
    meta = {
        "has_primary_key": True, "primary_key_columns": ("id",),
        "foreign_key_targets": (), "confirmed_relationship_targets": (), "confirmed_relationship_sources": (),
        "confirmed_relationship_edges": (),
        "business_name": "Business Name", "has_business_name": True,
        "description": "A description.", "has_description": True,
        "synonyms": ("alpha", "beta"), "synonym_count": 2,
        "status_columns": ("Status",), "date_columns": ("CreatedDate",), "measure_columns": ("Amount",),
    }
    meta.update(overrides)
    return meta


def _bare_meta(**overrides):
    meta = {
        "has_primary_key": False, "primary_key_columns": (),
        "foreign_key_targets": (), "confirmed_relationship_targets": (), "confirmed_relationship_sources": (),
        "confirmed_relationship_edges": (),
        "has_business_name": False, "has_description": False, "synonym_count": 0,
        "status_columns": (), "date_columns": (), "measure_columns": (),
    }
    meta.update(overrides)
    return meta


# ── Authoritative object beats a narrow similarly named object ───────────

def test_authoritative_object_outranks_narrow_similarly_named_object_when_tied_on_relevance(db_path):
    # narrow and rich get an IDENTICAL embedding (a true relevance tie), so
    # core.sqlservice.vector_index's own fqn-ascending tie-break gives
    # narrow (alphabetically first) the SLIGHTLY better semantic_rank
    # (1 vs 2) -- the adversarial case. Three "moderate" filler objects
    # (semantically irrelevant -- a distinct, orthogonal embedding, so they
    # never enter the small candidate pool) exist in the same build with
    # canonical scores between narrow's and rich's, pushing narrow's GLOBAL
    # canonical rank down far enough that the bounded canonical prior can
    # still outweigh its one-rank relevance edge -- proving real causal
    # effect, not an alphabetical coincidence.
    moderate_meta = _bare_meta(has_primary_key=True, primary_key_columns=("id",), has_business_name=True, business_name="Moderate")
    docs = [
        _doc("dbo.ZZZRichStudent", metadata=_rich_meta(business_name="Student")),
        _doc("dbo.AAANarrowTracker", metadata=_bare_meta()),
        _doc("dbo.Moderate1", metadata=moderate_meta),
        _doc("dbo.Moderate2", metadata=moderate_meta),
        _doc("dbo.Moderate3", metadata=moderate_meta),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.ZZZRichStudent": [1.0, 0.0, 0.0, 0.0],
        "dbo.AAANarrowTracker": [1.0, 0.0, 0.0, 0.0],  # identical vector -- true relevance tie
        "dbo.Moderate1": [0.0, 1.0, 0.0, 0.0],
        "dbo.Moderate2": [0.0, 1.0, 0.0, 0.0],
        "dbo.Moderate3": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2)

    anchors = [d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR]
    assert len(anchors) == 1
    assert anchors[0].object_fqn == "dbo.ZZZRichStudent"
    assert anchors[0].canonical_changed_anchor_rank is True


def test_analytical_view_can_beat_weak_base_table_when_metadata_supports_it(db_path):
    # Same adversarial construction as the authoritative-object test above:
    # a true relevance tie where fqn tie-break favors the weak table, offset
    # by moderate filler objects widening the view's global canonical-rank
    # advantage enough for the bounded prior to still decide it.
    moderate_meta = _bare_meta(has_primary_key=True, primary_key_columns=("id",), has_business_name=True, business_name="Moderate")
    docs = [
        _doc("dbo.AAARawStaging", object_type="TABLE", metadata=_bare_meta()),
        _doc(
            "dbo.ZZZActiveSummary", object_type="VIEW",
            metadata=_rich_meta(business_name="Active Summary", synonyms=("summary", "active"), synonym_count=2),
        ),
        _doc("dbo.Moderate1", metadata=moderate_meta),
        _doc("dbo.Moderate2", metadata=moderate_meta),
        _doc("dbo.Moderate3", metadata=moderate_meta),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.AAARawStaging": [1.0, 0.0, 0.0, 0.0],
        "dbo.ZZZActiveSummary": [1.0, 0.0, 0.0, 0.0],
        "dbo.Moderate1": [0.0, 1.0, 0.0, 0.0],
        "dbo.Moderate2": [0.0, 1.0, 0.0, 0.0],
        "dbo.Moderate3": [0.0, 1.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2)

    anchors = [d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR]
    assert anchors[0].object_fqn == "dbo.ZZZActiveSummary"


# ── Exact query relevance still dominates unrelated canonical objects ────

def test_query_relevance_dominates_an_unrelated_high_canonical_object(db_path):
    # dbo.Authoritative has an excellent canonical score but is ORTHOGONAL
    # to the question (opposite embedding direction, zero lexical overlap)
    # -- it must never become the anchor purely on canonical strength.
    docs = [
        _doc("dbo.Relevant", metadata=_bare_meta()),
        _doc("dbo.Authoritative", metadata=_rich_meta(business_name="Authoritative Central Table")),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Relevant": [1.0, 0.0, 0.0, 0.0],
        "dbo.Authoritative": [0.0, 1.0, 0.0, 0.0],  # orthogonal -- zero cosine similarity
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2)

    anchors = [d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR]
    assert anchors[0].object_fqn == "dbo.Relevant"


def test_bounded_prior_cannot_cause_unrelated_leakage_outside_the_pool(db_path):
    # A small pool (candidate_pool_size=1) admits only the one truly
    # relevant object; a highly-canonical but semantically distant object
    # must never appear in the final context at all, regardless of how
    # authoritative it is build-wide.
    docs = [
        _doc("dbo.Relevant", metadata=_bare_meta()),
        _doc("dbo.VeryAuthoritativeButUnrelated", metadata=_rich_meta(business_name="Very Authoritative")),
        _doc("dbo.AlsoUnrelated", metadata=_bare_meta()),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Relevant": [1.0, 0.0, 0.0, 0.0],
        "dbo.VeryAuthoritativeButUnrelated": [0.0, 1.0, 0.0, 0.0],
        "dbo.AlsoUnrelated": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1, relationship_limit=0,
    )

    fqns = {d.object_fqn for d in result.documents}
    assert fqns == {"dbo.Relevant"}
    assert "dbo.VeryAuthoritativeButUnrelated" not in fqns


# ── Stale/archive/system objects remain excluded ──────────────────────────

def test_tier34_objects_never_enter_via_canonical_prior(db_path):
    docs = [
        _doc("dbo.Relevant", tier=1, metadata=_bare_meta()),
        _doc("dbo.ArchiveCopy", tier=4, metadata=_rich_meta(business_name="Archive Copy")),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Relevant": [1.0, 0.0, 0.0, 0.0],
        "dbo.ArchiveCopy": [1.0, 0.0, 0.0, 0.0],  # even with a tied embedding
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=2, candidate_pool_size=2)

    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.ArchiveCopy" not in fqns  # default allowed_tiers={1,2} excludes it before canonical ever runs


# ── Bridge table remains eligible ─────────────────────────────────────────

def test_bridge_table_remains_eligible_with_canonical_prior_enabled(db_path):
    docs = [
        _doc("dbo.A", metadata=_bare_meta(confirmed_relationship_targets=("dbo.Bridge.id",))),
        _doc("dbo.B", metadata=_bare_meta(confirmed_relationship_sources=("dbo.Bridge.a_id",))),
        _doc("dbo.Bridge", metadata=_bare_meta(
            confirmed_relationship_sources=("dbo.A.id",), confirmed_relationship_targets=("dbo.B.id",),
        )),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.A": [1.0, 0.0, 0.0, 0.0],
        "dbo.B": [0.9, 0.1, 0.0, 0.0],
        "dbo.Bridge": [0.0, 0.0, 1.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=2, candidate_pool_size=2, relationship_limit=5,
    )

    bridges = [d for d in result.documents if d.selection_reason == SELECTION_REASON_RELATIONSHIP_BRIDGE]
    assert any(d.object_fqn == "dbo.Bridge" for d in bridges)


# ── Observability and immutability ─────────────────────────────────────────

def test_canonical_observability_fields_populated_for_anchors(db_path):
    docs = [
        _doc("dbo.Student", metadata=_rich_meta(business_name="Student")),
        _doc("dbo.Filler", metadata=_bare_meta()),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Student": [1.0, 0.0, 0.0, 0.0], "dbo.Filler": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context("student", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2)

    anchor = next(d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR)
    assert anchor.canonical_total_score is not None
    assert 0.0 <= anchor.canonical_total_score <= 1.0
    assert anchor.canonical_components is not None
    assert set(anchor.canonical_components.keys()) == set(canonical_scoring_module.COMPONENT_WEIGHTS.keys())
    assert anchor.canonical_rank is not None
    assert anchor.canonical_family_key is not None
    assert anchor.canonical_family_size is not None
    assert anchor.canonical_family_rank is not None
    assert anchor.canonical_competing_fqns is not None
    assert anchor.canonical_changed_anchor_rank in (True, False)

    with pytest.raises(Exception):
        anchor.canonical_total_score = 0.0  # type: ignore[misc]


def test_canonical_fields_are_none_when_disabled(db_path):
    docs = [_doc("dbo.Student", metadata=_rich_meta()), _doc("dbo.Filler", metadata=_bare_meta())]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Student": [1.0, 0.0, 0.0, 0.0], "dbo.Filler": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "student", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2, enable_canonical_prior=False,
    )
    anchor = next(d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR)
    assert anchor.canonical_total_score is None
    assert anchor.canonical_changed_anchor_rank is None


# ── Ablation: disabling canonical reproduces pre-Step-8 ordering ─────────

def test_disabling_canonical_prior_reproduces_pure_relevance_ordering(db_path):
    # With canonical DISABLED, a tie between a rich and a bare document
    # must fall back to deterministic FQN tie-break alone (pre-Step-8
    # behavior) -- "StudentOutreachTracking1" would lose to "Student" only
    # because canonical scoring is what breaks the tie; without it, plain
    # alphabetical fqn tie-break decides, which also happens to favor
    # "Student" here (S-t-u-d-e-n-t < S-t-u-d-e-n-t-O...) -- so instead we
    # verify canonical fields are entirely absent, proving the prior did
    # not run at all.
    docs = [_doc("dbo.Zebra", metadata=_rich_meta()), _doc("dbo.Alpha", metadata=_bare_meta())]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Zebra": [1.0, 0.0, 0.0, 0.0], "dbo.Alpha": [1.0, 0.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2, enable_canonical_prior=False,
    )
    anchor = next(d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR)
    # Pure relevance tie -> deterministic fqn ascending tie-break: "dbo.Alpha" < "dbo.Zebra".
    assert anchor.object_fqn == "dbo.Alpha"
    assert anchor.canonical_total_score is None


def test_zero_weight_is_equivalent_to_disabled(db_path):
    docs = [_doc("dbo.Zebra", metadata=_rich_meta()), _doc("dbo.Alpha", metadata=_bare_meta())]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Zebra": [1.0, 0.0, 0.0, 0.0], "dbo.Alpha": [1.0, 0.0, 0.0, 0.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2, canonical_prior_weight=0.0,
    )
    anchor = next(d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR)
    assert anchor.object_fqn == "dbo.Alpha"
    assert anchor.canonical_total_score is None


# ── Validation ──────────────────────────────────────────────────────────

def test_invalid_canonical_prior_weight_rejected(db_path):
    docs = [_doc("dbo.A")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.A": [1.0, 0.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    with pytest.raises(ValueError):
        retrieve_schema_context("q", build.id, 1, "alice", provider, canonical_prior_weight=1.5)
    with pytest.raises(ValueError):
        retrieve_schema_context("q", build.id, 1, "alice", provider, canonical_prior_weight=-0.1)
    with pytest.raises(ValueError):
        retrieve_schema_context("q", build.id, 1, "alice", provider, enable_canonical_prior="yes")  # type: ignore[arg-type]


# ── Cache isolation from question content, wired end-to-end ──────────────

def test_canonical_cache_reused_across_different_questions_same_build(db_path):
    docs = [_doc("dbo.Student", metadata=_rich_meta()), _doc("dbo.Filler", metadata=_bare_meta())]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Student": [1.0, 0.0, 0.0, 0.0], "dbo.Filler": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))

    retrieve_schema_context("first question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2)
    assert canonical_scoring_module.canonical_scoring_cache_size() == 1
    retrieve_schema_context("a totally different second question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=2)
    assert canonical_scoring_module.canonical_scoring_cache_size() == 1  # still one entry -- same build identity


def test_deterministic_output_across_repeated_calls(db_path):
    docs = [
        _doc("dbo.Student", metadata=_rich_meta()),
        _doc("dbo.StudentTracking", metadata=_bare_meta()),
        _doc("dbo.Filler", metadata=_bare_meta()),
    ]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Student": [1.0, 0.0, 0.0, 0.0],
        "dbo.StudentTracking": [1.0, 0.0, 0.0, 0.0],
        "dbo.Filler": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)

    def _run():
        provider = FakeEmbeddingProvider(fixed_vector=(1.0, 0.0, 0.0, 0.0))
        return retrieve_schema_context("student", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=3)

    a = _run()
    b = _run()
    assert [d.object_fqn for d in a.documents] == [d.object_fqn for d in b.documents]
    assert [d.canonical_total_score for d in a.documents] == [d.canonical_total_score for d in b.documents]
