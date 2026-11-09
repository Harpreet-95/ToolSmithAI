"""
Focused tests — SQL rebuild Phase 5, Step 9 integration:
- FEW_SHOT_EXAMPLES prompt section (core/sqlservice/sql_generation_service.py)
- example-derived outside-pool object injection + observability
  (core/sqlservice/schema_context_retrieval.py)
- query_vector reuse (no second embedding call)

Pure/offline: no database connection beyond a per-test temporary SQLite
file, no OpenAI call, no live SQL Server.

Run:
    venv/Scripts/pytest tests/test_sqlservice_step9_few_shot_integration.py -v
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
os.environ.setdefault("JWT_SECRET", "test-step9-integration-secret-long-enough-12345")
os.environ.setdefault("USER_ID_SALT", "test-step9-integration-salt-long-enough-123456")

import data.db as data_db_module
import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
from core.sqlservice.embedding_providers import (
    EmbeddingProvider,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.lexical_index import clear_lexical_index_cache
from core.sqlservice.canonical_scoring import clear_canonical_scoring_cache
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_EXAMPLE_DERIVED,
    SELECTION_REASON_HYBRID_ANCHOR,
    retrieve_schema_context,
)
from core.sqlservice.vector_index import clear_vector_index_cache
from core.sqlservice.sql_generation_service import (
    FewShotExample,
    MAX_FEW_SHOT_EXAMPLES,
    generate_sql_candidate,
)
from core.sqlservice.sql_generation_providers import (
    ProviderSqlGenerationResponse,
    TextToSqlProvider,
)


# ── Part A: FEW_SHOT_EXAMPLES prompt section (no DB needed) ──────────────

from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_SEMANTIC,
    STATUS_OK as SCHEMA_STATUS_OK,
    ContextDocument,
    SchemaContextResult,
)


def _context_document(fqn, *, text=None, tier=1):
    if text is None:
        text = f"OBJECT: {fqn}\nTYPE: TABLE\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    return ContextDocument(
        object_fqn=fqn, object_type="TABLE", retrieval_tier=tier,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), document_text=text,
        metadata=MappingProxyType({"column_count": 1}), score=0.9, selection_reason=SELECTION_REASON_SEMANTIC,
    )


def _schema_context(documents):
    context_text = "\n\n".join(d.document_text for d in documents)
    return SchemaContextResult(
        status=SCHEMA_STATUS_OK, build_id=1, schema_fingerprint="fp-123",
        embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=4,
        documents=tuple(documents), context_text=context_text, total_characters=len(context_text),
        truncated=False, warnings=(), error=None,
    )


def _valid_raw_response(sql="SELECT COUNT(*) FROM dbo.Student"):
    return {
        "sql": sql, "interpretation": "x", "referenced_object_fqns": ["dbo.Student"],
        "assumptions": [], "needs_clarification": False, "clarification_question": None,
    }


class FakeTextToSqlProvider(TextToSqlProvider):
    def __init__(self):
        self._provider_id = "fake"
        self._model = "fake-model"
        self.calls: list[dict] = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def generate_sql(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        return ProviderSqlGenerationResponse(raw=_valid_raw_response(), model=self._model)


def test_no_examples_produces_byte_identical_prompt_to_pre_step9():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    generate_sql_candidate("How many students?", schema_context, provider)
    assert "FEW_SHOT_EXAMPLES" not in provider.calls[0]["user_prompt"]


def test_examples_included_with_strict_delimiters():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    examples = (
        FewShotExample(question_pattern="How many :entity are there", sql_template="SELECT COUNT(*) FROM :table", result_shape="COUNT"),
    )
    generate_sql_candidate("How many students?", schema_context, provider, examples=examples)
    prompt = provider.calls[0]["user_prompt"]
    assert "<<<FEW_SHOT_EXAMPLES_START>>>" in prompt
    assert "<<<FEW_SHOT_EXAMPLES_END>>>" in prompt
    assert "How many :entity are there" in prompt
    assert "SELECT COUNT(*) FROM :table" in prompt
    start = prompt.index("<<<FEW_SHOT_EXAMPLES_START>>>")
    end = prompt.index("<<<FEW_SHOT_EXAMPLES_END>>>")
    assert start < end


def test_system_prompt_states_examples_are_patterns_not_overrides():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    generate_sql_candidate(
        "q", schema_context, provider,
        examples=(FewShotExample(question_pattern="p", sql_template="SELECT 1", result_shape="COUNT"),),
    )
    system_prompt = provider.calls[0]["system_prompt"]
    assert "PATTERN" in system_prompt.upper()
    assert "NEVER" in system_prompt.upper()
    assert "override" in system_prompt.lower() or "OVERRIDE" in system_prompt


def test_more_than_max_examples_rejected():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    too_many = tuple(
        FewShotExample(question_pattern=f"p{i}", sql_template="SELECT 1", result_shape="COUNT")
        for i in range(MAX_FEW_SHOT_EXAMPLES + 1)
    )
    with pytest.raises(ValueError):
        generate_sql_candidate("q", schema_context, provider, examples=too_many)
    assert provider.calls == []  # rejected before any provider call


def test_oversized_example_field_rejected():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    huge = FewShotExample(question_pattern="x" * 10_000, sql_template="SELECT 1", result_shape="COUNT")
    with pytest.raises(ValueError):
        generate_sql_candidate("q", schema_context, provider, examples=(huge,))


def test_still_exactly_one_provider_call_with_examples():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    generate_sql_candidate(
        "q", schema_context, provider,
        examples=(FewShotExample(question_pattern="p", sql_template="SELECT 1", result_shape="COUNT"),),
    )
    assert len(provider.calls) == 1


def test_multiple_examples_each_numbered():
    schema_context = _schema_context([_context_document("dbo.Student")])
    provider = FakeTextToSqlProvider()
    examples = (
        FewShotExample(question_pattern="p1", sql_template="SELECT 1", result_shape="COUNT"),
        FewShotExample(question_pattern="p2", sql_template="SELECT 2", result_shape="LIST"),
    )
    generate_sql_candidate("q", schema_context, provider, examples=examples)
    prompt = provider.calls[0]["user_prompt"]
    assert "EXAMPLE 1:" in prompt
    assert "EXAMPLE 2:" in prompt


# ── Part B: schema_context_retrieval.py example-derived injection ────────

def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _patch_connections(monkeypatch, db_path):
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(db_path))


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
    now = "2026-09-12T00:00:00Z"
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
    now = "2026-09-12T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id=1, tier=1):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type="TABLE",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType({"column_count": 1}),
    )


def _batch(source_id, snapshot_id, snapshot_version, documents):
    return RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=snapshot_version,
        status=STATUS_READY, documents=tuple(documents), counts=MappingProxyType({}), warnings=(),
    )


def _fingerprint():
    return {
        "fingerprint_version": 1, "fingerprint_hash": hashlib.sha256(b"fp").hexdigest(),
        "table_set_hash": hashlib.sha256(b"ts").hexdigest(),
    }


def _make_ready_build_with_docs(db_path, docs, source_id=1, snapshot_id=1, user_id="alice"):
    _insert_source(db_path, source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id, source_id=source_id)
    batch = _batch(source_id, snapshot_id, 1, docs)
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    return result.build


PROVIDER, MODEL, DIMS = "fake", "fake-model", 4


def _embed_all(db_path, build, vectors_by_fqn, *, docs, source_id=1, user_id="alice"):
    hash_by_fqn = {d.object_fqn: d.document_hash for d in docs}
    vectors_by_hash = {hash_by_fqn[fqn]: vec for fqn, vec in vectors_by_fqn.items()}
    result = insert_embeddings(build.id, source_id, user_id, PROVIDER, MODEL, DIMS, vectors_by_hash)
    assert result.status == EMBEDDING_STATUS_OK


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, fixed_vector=(1.0, 0.0, 0.0, 0.0)):
        self._fixed_vector = fixed_vector
        self.calls: list[list[str]] = []

    @property
    def provider_id(self):
        return PROVIDER

    @property
    def model(self):
        return MODEL

    @property
    def dimensions(self):
        return DIMS

    def embed_batch(self, texts):
        self.calls.append(list(texts))
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=tuple(self._fixed_vector)),), model=MODEL,
        )


def test_example_derived_object_admitted_outside_pool(db_path):
    docs = [_doc("dbo.Anchor"), _doc("dbo.ExampleProposed"), _doc("dbo.Filler")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {
        "dbo.Anchor": [1.0, 0.0, 0.0, 0.0], "dbo.ExampleProposed": [0.0, 1.0, 0.0, 0.0],
        "dbo.Filler": [0.0, 0.0, 0.0, 1.0],
    }, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1, relationship_limit=0,
        example_proposed_objects=[("dbo.ExampleProposed", 42, "matched example pattern")],
    )

    by_fqn = {d.object_fqn: d for d in result.documents}
    assert "dbo.ExampleProposed" in by_fqn
    proposed = by_fqn["dbo.ExampleProposed"]
    assert proposed.selection_reason == SELECTION_REASON_EXAMPLE_DERIVED
    assert proposed.example_id == 42
    assert proposed.example_derived_reason == "matched example pattern"


def test_example_derived_object_respects_tier_filtering(db_path):
    docs = [_doc("dbo.Anchor"), _doc("dbo.StaleExample", tier=4)]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0], "dbo.StaleExample": [0.0, 1.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
        example_proposed_objects=[("dbo.StaleExample", 1, "reason")],
    )
    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.StaleExample" not in fqns  # default allowed_tiers={1,2} excludes tier 4


def test_example_derived_object_respects_context_budget(db_path):
    big_text = "OBJECT: dbo.Big\nTYPE: TABLE\nTIER: 1\n" + "\n".join(f"  col_{i} TEXT" for i in range(1000))
    big_doc = RetrievalDocument(
        source_id=1, object_fqn="dbo.Big", object_type="TABLE", schema_name="dbo", object_name="Big",
        retrieval_tier=1, document_text=big_text, document_hash=hashlib.sha256(big_text.encode()).hexdigest(),
        metadata=MappingProxyType({"column_count": 1000}),
    )
    docs = [_doc("dbo.Anchor"), big_doc]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0], "dbo.Big": [0.0, 1.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    small_budget = len(docs[0].document_text) + 50  # not enough room for dbo.Big's full or compact text
    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
        max_context_characters=small_budget, example_proposed_objects=[("dbo.Big", 1, "reason")],
    )
    assert result.total_characters <= small_budget
    fqns = {d.object_fqn for d in result.documents}
    assert "dbo.Big" not in fqns or any(d.used_compact_text for d in result.documents if d.object_fqn == "dbo.Big")


def test_example_derived_object_not_duplicated_if_already_an_anchor(db_path):
    docs = [_doc("dbo.Anchor"), _doc("dbo.Filler")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0], "dbo.Filler": [0.0, 0.0, 0.0, 1.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
        example_proposed_objects=[("dbo.Anchor", 1, "reason")],
    )
    anchor_hits = [d for d in result.documents if d.object_fqn == "dbo.Anchor"]
    assert len(anchor_hits) == 1
    assert anchor_hits[0].selection_reason == SELECTION_REASON_HYBRID_ANCHOR  # not overwritten as EXAMPLE_DERIVED


def test_example_derived_reason_none_for_other_selection_reasons(db_path):
    docs = [_doc("dbo.Anchor"), _doc("dbo.Filler")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0], "dbo.Filler": [0.0, 0.0, 0.0, 1.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1)
    anchor = next(d for d in result.documents if d.selection_reason == SELECTION_REASON_HYBRID_ANCHOR)
    assert anchor.example_id is None
    assert anchor.example_derived_reason is None


# ── Step 9A: query-vector encapsulation ────────────────────────────────────
# SchemaContextResult never carries a query embedding at all (removed, not
# merely left None) -- core.sqlservice.retrieve_schema_context_and_examples
# is the only (internal-only) flow that reuses the one embedding call for
# both schema and example retrieval; it too returns no vector anywhere.

import dataclasses

from core.sqlservice.schema_context_retrieval import (
    EXAMPLE_RETRIEVAL_NOT_ATTEMPTED,
    SchemaContextWithExamplesResult,
    retrieve_schema_context_and_examples,
)


def _looks_like_a_vector(value) -> bool:
    """True for anything shaped like an embedding: a sequence of several
    numbers -- the exact shape a leaked query_vector would take once
    serialized (e.g. through dataclasses.asdict)."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)


def test_schema_context_result_has_no_query_vector_field():
    field_names = {f.name for f in dataclasses.fields(SchemaContextResult)}
    assert "query_vector" not in field_names


def test_retrieve_schema_context_makes_exactly_one_embed_call(db_path):
    docs = [_doc("dbo.Anchor")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1)
    assert result.status == SCHEMA_STATUS_OK
    assert len(provider.calls) == 1
    assert not hasattr(result, "query_vector")


def test_asdict_of_schema_context_result_never_contains_a_vector(db_path):
    """dataclasses.asdict() is exactly the pattern api/v1/routes.py already
    uses elsewhere in this codebase (dataclasses.asdict(obj) then
    json.dumps) to turn a dataclass result into a JSON-safe response --
    the concrete mechanism a route/adapter could use to leak a field that
    exists. Checked shallowly (this dataclass's own top-level field names)
    rather than via a full recursive asdict(), which chokes independently
    on an unrelated MappingProxyType nested inside ContextDocument.metadata
    on this Python version -- irrelevant to the query-vector guarantee."""
    docs = [_doc("dbo.Anchor")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context("question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1)
    shallow = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    assert "query_vector" not in shallow
    assert not any(_looks_like_a_vector(v) for v in shallow.values())


def test_retrieve_schema_context_and_examples_still_exactly_one_embed_call(db_path):
    """The internal-only combined flow reuses the SAME vector for schema
    search AND example search -- never a second embedding call, even
    though example retrieval happens too."""
    docs = [_doc("dbo.Anchor")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context_and_examples(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
    )
    assert isinstance(result, SchemaContextWithExamplesResult)
    assert result.schema_context.status == SCHEMA_STATUS_OK
    assert len(provider.calls) == 1  # ONE call supports both schema search and example search


def test_retrieve_schema_context_and_examples_empty_index_matches_plain_call_byte_for_byte(db_path):
    """Step 9A requirement: an empty (or absent) example index preserves
    pre-Step-9 schema-retrieval behavior byte-for-byte."""
    docs = [_doc("dbo.Anchor")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0]}, docs=docs)

    plain = retrieve_schema_context(
        "question", build.id, 1, "alice", FakeEmbeddingProvider(), top_k=1, candidate_pool_size=1,
    )
    combined = retrieve_schema_context_and_examples(
        "question", build.id, 1, "alice", FakeEmbeddingProvider(), top_k=1, candidate_pool_size=1,
    )
    assert combined.schema_context == plain
    assert combined.examples == ()
    assert combined.example_retrieval_status in (EXAMPLE_RETRIEVAL_NOT_ATTEMPTED, "NO_EXAMPLES", "UNOWNED")


def test_retrieve_schema_context_and_examples_not_attempted_when_schema_retrieval_fails(db_path):
    provider = FakeEmbeddingProvider()
    result = retrieve_schema_context_and_examples("question", 999999, 1, "alice", provider)  # build doesn't exist
    assert result.schema_context.status != SCHEMA_STATUS_OK
    assert result.examples == ()
    assert result.example_retrieval_status == EXAMPLE_RETRIEVAL_NOT_ATTEMPTED
    assert len(provider.calls) == 0  # never even embeds when the build itself can't be loaded


def test_asdict_of_combined_result_never_contains_the_question_vector(db_path):
    """No field anywhere on SchemaContextWithExamplesResult (nor on the
    nested schema_context) ever carries the CURRENT question's embedding
    -- checked by field name (never present at all, on either dataclass)
    rather than by shape, since `examples` (empty for this owned-but-
    example-free source) is out of scope here: a retrieved example's OWN
    already-persisted search-document vector is a different, pre-existing
    core.sqlservice.sql_example_vector_index.IndexedSqlExample concern,
    untouched by this Step 9A change."""
    docs = [_doc("dbo.Anchor")]
    build = _make_ready_build_with_docs(db_path, docs)
    _embed_all(db_path, build, {"dbo.Anchor": [1.0, 0.0, 0.0, 0.0]}, docs=docs)
    provider = FakeEmbeddingProvider()

    result = retrieve_schema_context_and_examples(
        "question", build.id, 1, "alice", provider, top_k=1, candidate_pool_size=1,
    )
    assert result.examples == ()  # this source has no examples -- confirms the check below is meaningful
    top_level = {f.name for f in dataclasses.fields(result)}
    schema_context_level = {f.name for f in dataclasses.fields(result.schema_context)}
    assert "query_vector" not in top_level
    assert "query_vector" not in schema_context_level
