"""
Focused tests — SQL rebuild Phase 5, Step 11B: bounded automatic
SQL-example preparation coordinator (core/sqlservice/sql_example_preparation.py).

Uses temporary on-disk SQLite databases only — never data/toolsmith.db,
never SQL Server, never OpenAI. Every embedding provider is a fake defined
in this file. Schema is created via the real `data.models.init_db()`
(monkeypatched to a temp file, then copied per test for speed), the same
pattern tests/test_sqlservice_retrieval_preparation.py already established.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_example_preparation.py -v
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step11b-preparation-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-step11b-preparation-salt-long-enough-12")

import data.db as data_db_module
import data.models as models_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_preparation as preparation_module
import core.sqlservice.sql_example_store as example_store_module
from core.sqlservice.embedding_providers import (
    EmbeddingProvider,
    EmbeddingProviderError,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.sql_example_model import TRUST_LEVEL_TRUSTED
from core.sqlservice.sql_example_preparation import (
    STATUS_EMBEDDING_FAILED,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    SqlExampleEmbeddingValidationError,
    SqlExamplePreparationResult,
    prepare_source_for_sql_examples,
)
from core.sqlservice.sql_example_store import list_examples_for_source
from core.sqlservice.sql_example_vector_index import (
    clear_sql_example_vector_index_cache,
    load_sql_example_vector_index,
)

USER_ID = "alice"
SOURCE_ID = 1
PROVIDER = "fake"
MODEL = "fake-model"
DIMS = 4


# ── Fixture plumbing (mirrors tests/test_sqlservice_step9_few_shot_integration.py) ─


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
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_example_index_cache():
    clear_sql_example_vector_index_cache()
    yield
    clear_sql_example_vector_index_cache()


def _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID):
    conn = sqlite3.connect(db_path)
    now = "2026-09-10T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " created_at, updated_at) VALUES (?, ?, 'Test Source', 'mssql', 'relational_db', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, snapshot_id=1, source_id=SOURCE_ID, version=1):
    conn = sqlite3.connect(db_path)
    now = "2026-09-10T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id=SOURCE_ID, tier=1, columns=("id", "name"), pk=("id",)):
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\nCOLUMNS:\n" + "\n".join(f"  {c}" for c in columns)
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    metadata = {"column_names": tuple(columns), "primary_key_columns": tuple(pk)}
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type="TABLE",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def _fingerprint(seed=b"fp"):
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(seed).hexdigest(),
        "table_set_hash": hashlib.sha256(seed + b"-ts").hexdigest(),
    }


def _make_ready_build(db_path, docs, *, source_id=SOURCE_ID, snapshot_id=1, user_id=USER_ID, version=1, seed=b"fp"):
    if snapshot_id == 1 and version == 1:
        _insert_source(db_path, source_id=source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id=snapshot_id, source_id=source_id, version=version)
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=version,
        status=STATUS_READY, documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(seed), user_id)
    assert result.build is not None
    return result.build


class FakeEmbeddingProvider(EmbeddingProvider):
    """Records every embed_batch() call and returns one item per input
    text (unlike a single-question-only fake) — the shape
    core.sqlservice.sql_example_preparation actually needs, since it can
    legitimately embed several search-document texts in one call."""

    def __init__(self, dimensions=DIMS, provider_id=PROVIDER, model=MODEL, fixed_vector=None, raise_after=None, raise_exc=None):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
        self._fixed_vector = fixed_vector if fixed_vector is not None else (1.0, 0.0, 0.0, 0.0)
        self._raise_after = raise_after  # raise on the (0-indexed) call number given, if set
        self._raise_exc = raise_exc
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
        call_index = len(self.calls)
        self.calls.append(texts)
        if self._raise_after is not None and call_index >= self._raise_after:
            raise (self._raise_exc or EmbeddingProviderError("simulated provider failure"))
        items = tuple(
            ProviderEmbeddingItem(index=i, vector=tuple(self._fixed_vector)) for i in range(len(texts))
        )
        return ProviderEmbeddingBatchResponse(items=items, model=self._model)


class _CorruptVectorEmbeddingProvider(FakeEmbeddingProvider):
    """Returns a non-finite vector element for every item — simulates a
    malformed provider response."""

    def embed_batch(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        items = tuple(
            ProviderEmbeddingItem(index=i, vector=(math.nan, 0.0, 0.0, 0.0)) for i in range(len(texts))
        )
        return ProviderEmbeddingBatchResponse(items=items, model=self._model)


def _make_clock(values):
    it = iter(values)
    return lambda: next(it)


# ── 1. First preparation inserts trusted generated examples ───────────────


def test_first_preparation_inserts_trusted_generated_examples(db_path):
    build = _make_ready_build(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_OK
    assert result.build_id == build.id
    assert result.schema_fingerprint == build.schema_fingerprint
    assert result.generated_count == 2  # ROW_COUNT + ORDERED_LIST for one eligible table
    assert result.examples_inserted == 2
    assert result.examples_reused == 0
    assert result.embeddings_generated == 2
    assert result.embeddings_reused == 0
    assert result.indexed_count == 2
    assert result.error is None
    assert len(provider.calls) == 1  # one bounded batch call

    stored = list_examples_for_source(SOURCE_ID, USER_ID)
    assert len(stored) == 2
    assert all(ex.trust_level == TRUST_LEVEL_TRUSTED for ex in stored)


# ── 2. Repeat preparation reuses examples and embeddings ───────────────────


def test_repeat_preparation_reuses_examples_and_embeddings(db_path):
    build = _make_ready_build(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()

    first = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)
    assert first.status == STATUS_OK
    calls_after_first = len(provider.calls)

    second = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    assert second.status == STATUS_OK
    assert second.build_id == build.id
    assert second.generated_count == 2
    assert second.examples_inserted == 0
    assert second.examples_reused == 2
    assert second.embeddings_generated == 0
    assert second.embeddings_reused == 2
    assert second.indexed_count == 2
    assert len(provider.calls) == calls_after_first  # zero NEW provider calls

    stored = list_examples_for_source(SOURCE_ID, USER_ID)
    assert len(stored) == 2  # no duplicate rows


# ── 3. Only missing embeddings are requested ───────────────────────────────


def test_only_missing_embeddings_are_requested(db_path):
    unchanged = _doc("dbo.aardvarks")
    build_1 = _make_ready_build(db_path, [unchanged], seed=b"seed-1")
    provider = FakeEmbeddingProvider()

    first = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)
    assert first.status == STATUS_OK
    assert first.embeddings_generated == 2

    new_table = _doc("dbo.bobcats")
    build_2 = _make_ready_build(db_path, [unchanged, new_table], snapshot_id=2, version=2, seed=b"seed-2")
    assert build_2.id != build_1.id
    provider.calls.clear()

    second = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    assert second.status == STATUS_OK
    assert second.build_id == build_2.id
    assert second.generated_count == 4  # 2 objects x 2 patterns
    assert second.examples_inserted == 2   # only dbo.bobcats' two candidates are new content_hash values
    assert second.examples_reused == 2     # dbo.aardvarks' candidates are byte-identical -> REUSED
    assert second.embeddings_generated == 2  # only the new table's two search documents
    assert second.embeddings_reused == 2
    assert len(provider.calls) == 1
    sent_texts = provider.calls[0]
    assert not any("aardvark" in t.lower() for t in sent_texts)  # never re-sent
    assert any("bobcat" in t.lower() for t in sent_texts)


# ── 4. Provider failure writes zero partial embeddings ─────────────────────


def test_provider_failure_writes_zero_partial_embeddings(db_path):
    docs = [_doc(f"dbo.thing_{i}") for i in range(4)]  # 4 objects x 2 patterns = 8 candidates
    build = _make_ready_build(db_path, docs)
    # max_batch_items=1 forces one provider call per search document;
    # raise on the second call so the first call's vector must never be
    # persisted either (all-or-nothing across the whole run).
    provider = FakeEmbeddingProvider(raise_after=1)

    with pytest.raises(EmbeddingProviderError):
        prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider, max_batch_items=1)

    conn = _make_connection(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM sql_example_embeddings").fetchone()[0]
    finally:
        conn.close()
    assert count == 0

    # The examples themselves (a separate, earlier stage) were still
    # inserted -- only embedding persistence is all-or-nothing per run.
    stored = list_examples_for_source(SOURCE_ID, USER_ID)
    assert len(stored) == 8


# ── 5. Ownership and non-READY build rejection ─────────────────────────────


def test_no_ready_build_is_not_prepared(db_path):
    _insert_source(db_path)  # source exists, but no retrieval build at all
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_NOT_PREPARED
    assert result.build_id is None
    assert result.generated_count == 0
    assert provider.calls == []


def test_unowned_source_is_not_prepared(db_path):
    _make_ready_build(db_path, [_doc("dbo.widgets")], user_id="alice")
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_sql_examples(SOURCE_ID, "mallory", provider)

    assert result.status == STATUS_NOT_PREPARED
    assert provider.calls == []


# ── 6. Empty generation result ──────────────────────────────────────────────


def test_empty_generation_result_is_not_a_failure(db_path):
    # Tier 4 objects are never eligible for generation (ALLOWED_GENERATION_TIERS = {1, 2}).
    build = _make_ready_build(db_path, [_doc("dbo.archived_widgets_backup", tier=4)])
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_OK
    assert result.build_id == build.id
    assert result.generated_count == 0
    assert result.examples_inserted == 0
    assert result.examples_reused == 0
    assert result.embeddings_generated == 0
    assert result.indexed_count == 0
    assert provider.calls == []


# ── 7. Duplicate/idempotent insertion ───────────────────────────────────────


def test_duplicate_insertion_across_runs_creates_no_extra_rows(db_path):
    _make_ready_build(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()

    prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)
    prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)
    prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    conn = _make_connection(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM sql_examples").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


# ── 8. Untrusted candidate structurally rejected ────────────────────────────


def test_insert_trusted_example_structurally_rejects_a_non_trusted_object(db_path):
    """The coordinator's own insertion stage calls ONLY
    core.sqlservice.sql_example_store.insert_trusted_example() on a real
    generated.candidate — this proves the gate it relies on is real, not
    merely conventional: a hand-built stand-in object is never accepted."""
    _make_ready_build(db_path, [_doc("dbo.widgets")])

    class _NotATrustedCandidate:
        trust_level = TRUST_LEVEL_TRUSTED

    with pytest.raises(ValueError):
        example_store_module.insert_trusted_example(USER_ID, _NotATrustedCandidate())


# ── 9. Trusted-only index ───────────────────────────────────────────────────


def test_prepared_index_contains_only_trusted_active_examples(db_path):
    _make_ready_build(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()

    result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)
    assert result.status == STATUS_OK

    index_result = load_sql_example_vector_index(SOURCE_ID, USER_ID, PROVIDER, MODEL, DIMS)
    assert index_result.status == "OK"
    assert len(index_result.index.examples) == result.indexed_count
    assert all(ex.trust_level == TRUST_LEVEL_TRUSTED and ex.status == "ACTIVE" for ex in index_result.index.examples)


# ── 10. Corrupt embedding failure ───────────────────────────────────────────


def test_corrupt_embedding_response_fails_closed(db_path):
    _make_ready_build(db_path, [_doc("dbo.widgets")])
    provider = _CorruptVectorEmbeddingProvider()

    with pytest.raises(SqlExampleEmbeddingValidationError):
        prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    conn = _make_connection(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM sql_example_embeddings").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# ── 11. Deterministic counts and durations ─────────────────────────────────


def test_deterministic_timing_via_injected_clock(db_path):
    _make_ready_build(db_path, [_doc("dbo.widgets")])
    provider = FakeEmbeddingProvider()
    # total_start, generation_start, generation_elapsed(=1.0), insertion_start,
    # insertion_elapsed(=1.0), embedding_start, embedding_elapsed(=2.0),
    # index_load_start, index_elapsed(=1.0), total_elapsed(=9.0)
    clock = _make_clock([0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 4.0, 4.0, 5.0, 9.0])

    result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider, clock=clock)

    assert result.status == STATUS_OK
    assert result.generation_duration_seconds == 1.0
    assert result.insertion_duration_seconds == 1.0
    assert result.embedding_duration_seconds == 2.0
    assert result.index_load_duration_seconds == 1.0
    assert result.total_duration_seconds == 9.0


def test_deterministic_timing_on_not_prepared(db_path):
    _insert_source(db_path)
    provider = FakeEmbeddingProvider()
    clock = _make_clock([0.0, 3.0])  # total_start, total_elapsed

    result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider, clock=clock)

    assert result.status == STATUS_NOT_PREPARED
    assert result.total_duration_seconds == 3.0
    assert result.generation_duration_seconds == 0.0
    assert result.embedding_duration_seconds == 0.0


# ── 12. Safe logs with injected fake credentials ────────────────────────────


def test_no_credential_or_content_leakage_in_logs_or_repr(db_path, caplog):
    _make_ready_build(db_path, [_doc("dbo.widgets")])

    class _SecretHoldingProvider(FakeEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self._api_key = "sk-fake-secret-should-never-appear-anywhere-12345"

    provider = _SecretHoldingProvider()

    with caplog.at_level("DEBUG"):
        result = prepare_source_for_sql_examples(SOURCE_ID, USER_ID, provider)

    assert result.status == STATUS_OK
    secret = "sk-fake-secret-should-never-appear-anywhere-12345"
    assert secret not in repr(result)
    assert secret not in caplog.text
    # Never logs question/example prompt content either.
    stored = list_examples_for_source(SOURCE_ID, USER_ID)
    for example in stored:
        assert example.question_pattern not in caplog.text
        assert example.sql_template not in caplog.text


def test_result_carries_no_secret_fields():
    field_names = set(SqlExamplePreparationResult.__dataclass_fields__.keys())
    assert not any(
        "key" in name.lower() or "secret" in name.lower() or "token" in name.lower() or "password" in name.lower()
        for name in field_names
    )


# ── Leakage boundary: this new module never imports golden fixtures ───────


def test_preparation_module_never_imports_golden_or_test_material():
    import ast
    from pathlib import Path

    src = Path("core/sqlservice/sql_example_preparation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    for module in imported_modules:
        assert not module.startswith("tests")
        lowered = module.lower()
        assert "golden" not in lowered
        assert "exampleco" not in lowered
