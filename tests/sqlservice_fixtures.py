"""
Shared SQLite-backed fixtures and fakes for the question-time (fast path)
test suites.

Uses a temporary on-disk SQLite database (never data/toolsmith.db) for the
real retrieval-build/embedding plumbing core.sqlservice.retrieval_store and
core.sqlservice.embedding_store need. The two SQLite-backed lookups inside
core.sqlservice.sql_execution (get_retrieval_build / get_connection_config)
are monkeypatched directly (see _wire_execution_lookups). Every provider is
a fake defined here; the live SQL connection is a fake DBAPI2 connection/
cursor pair. No real OpenAI call, no real SQL Server.

Phase 8A: this module used to be tests/sqlservice_fixtures.py,
the test file of the retired non-AI-first question pipeline; only its
fixtures survive, here, under a name pytest does not collect.
"""

import dataclasses
import hashlib
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.db as data_db_module
import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.sql_example_embedding_store as example_embedding_store_module
import core.sqlservice.sql_example_store as example_store_module
import core.sqlservice.sql_execution as sql_execution_module
from core.sqlservice.answer_generation_providers import (
    AnswerGenerationProvider,
    AnswerGenerationProviderError,
    ProviderAnswerGenerationResponse,
)
from core.sqlservice.embedding_providers import (
    EmbeddingProvider,
    EmbeddingProviderError,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.embedding_store import STATUS_OK as EMBEDDING_STATUS_OK, insert_embeddings
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_context_retrieval import SchemaContextResult
from core.sqlservice.sql_example_preparation import prepare_source_for_sql_examples
from core.sqlservice.sql_example_preparation import STATUS_OK as EXAMPLE_PREP_STATUS_OK
from core.sqlservice.sql_example_vector_index import clear_sql_example_vector_index_cache
from core.sqlservice.sql_generation_providers import TextToSqlConfigurationError, TextToSqlProvider
from core.sqlservice.vector_index import clear_vector_index_cache

USER_ID = "alice"
SOURCE_ID = 1
SNAPSHOT_ID = 1
PROVIDER = "fake"
MODEL = "fake-model"
DIMS = 4


# ── SQLite fixture plumbing (mirrors tests/test_sqlservice_schema_context_retrieval.py) ──


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
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(example_embedding_store_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()
    yield
    clear_vector_index_cache()
    clear_sql_example_vector_index_cache()


@pytest.fixture(autouse=True)
def _stub_sql_execution_sqlite_lookups(monkeypatch):
    """execute_validated_sql's own two SQLite-backed dependencies are
    monkeypatched per-test (see _wire_execution_lookups) — this fixture
    just guarantees a safe default (unowned/missing) so a test that
    doesn't reach the execution stage never accidentally touches a real
    connection object."""
    monkeypatch.setattr(sql_execution_module, "get_retrieval_build", lambda *a, **k: None)
    monkeypatch.setattr(sql_execution_module, "get_connection_config", lambda *a, **k: None)
    yield


def _wire_execution_lookups(monkeypatch, build):
    monkeypatch.setattr(
        sql_execution_module, "get_retrieval_build",
        lambda build_id, source_id, user_id: build if (build_id, source_id, user_id) == (build.id, build.source_id, USER_ID) else None,
    )
    monkeypatch.setattr(
        sql_execution_module, "get_connection_config",
        lambda source_id, user_id: _connection_record() if (source_id, user_id) == (build.source_id, USER_ID) else None,
    )


def _connection_record():
    return {
        "source_type": "mssql", "source_category": "relational_db", "display_name": "Test MSSQL",
        "is_active": True, "source_status": "ACTIVE", "capabilities": ["sql_query"], "live_query_enabled": True,
        "params": {"host": "db.internal", "port": 1433, "database": "AppDb", "username": "svc", "password": "unused"},
    }


# ── Retrieval-build / embedding setup ───────────────────────────────────


def _insert_source(db_path, source_id=SOURCE_ID, user_id=USER_ID):
    conn = sqlite3.connect(db_path)
    now = "2026-09-05T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " created_at, updated_at) VALUES (?, ?, 'Test Source', 'mssql', 'relational_db', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, snapshot_id=SNAPSHOT_ID, source_id=SOURCE_ID, version=1):
    conn = sqlite3.connect(db_path)
    now = "2026-09-05T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id=SOURCE_ID, object_type="TABLE", tier=1):
    text = f"OBJECT: {object_fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]\n  name VARCHAR(50)"
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type=object_type,
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash,
        metadata=MappingProxyType({"column_count": 2, "column_names": ("id", "name")}),
    )


def _fingerprint():
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(b"fingerprint-a").hexdigest(),
        "table_set_hash": hashlib.sha256(b"table-set-a").hexdigest(),
    }


def _prepare_source(db_path, *, object_fqn="dbo.widgets", source_id=SOURCE_ID, user_id=USER_ID, snapshot_id=None):
    """Full happy-path prerequisite: an owned source, a snapshot, one READY
    retrieval build with one document, and its embedding — everything
    get_latest_ready_retrieval_build() and retrieve_schema_context() need."""
    snapshot_id = snapshot_id if snapshot_id is not None else source_id
    _insert_source(db_path, source_id=source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id=snapshot_id, source_id=source_id)
    doc = _doc(object_fqn, source_id=source_id)
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=1,
        status=STATUS_READY, documents=(doc,), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    build = result.build

    vectors_by_hash = {doc.document_hash: [1.0, 0.0, 0.0, 0.0]}
    embed_result = insert_embeddings(build.id, source_id, user_id, PROVIDER, MODEL, DIMS, vectors_by_hash)
    assert embed_result.status == EMBEDDING_STATUS_OK
    return build


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimensions=DIMS, provider_id=PROVIDER, model=MODEL, fixed_vector=None, on_call=None, raise_exc=None):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
        self._fixed_vector = fixed_vector if fixed_vector is not None else (1.0, 0.0, 0.0, 0.0)
        self._on_call = on_call
        self._raise_exc = raise_exc
        self.calls: list = []

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
        if self._on_call is not None:
            self._on_call()
        self.calls.append(list(texts))
        if self._raise_exc is not None:
            raise self._raise_exc
        return ProviderEmbeddingBatchResponse(
            items=(ProviderEmbeddingItem(index=0, vector=tuple(self._fixed_vector)),), model=self._model,
        )


def _sql_generation_raw(
    sql="SELECT id, name FROM dbo.widgets", interpretation="Lists widgets.",
    referenced=("dbo.widgets",), needs_clarification=False, clarification_question=None,
    selected_option_id="1",
):
    # Phase 6, Step 6I, Part 5 — generate_sql_candidate now REQUIRES a
    # selected_option_id whenever core.sqlservice.schema_plan_options
    # actually offered options (this pipeline calls it for real against a
    # real prepared build in these tests, unlike the mocked-planner unit
    # tests in tests/test_sqlservice_sql_generation.py). "1" — the
    # highest-ranked option — is a safe default: these fixtures always ask
    # a question whose single relevant object IS `referenced`, which is
    # the same object schema_plan_options.py's own best-ranked plan would
    # already offer as option "1". A caller needing a different id can
    # still override it explicitly.
    return {
        "sql": "" if needs_clarification else sql,
        "interpretation": interpretation,
        "referenced_object_fqns": [] if needs_clarification else list(referenced),
        "assumptions": [],
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
        "selected_option_id": None if needs_clarification else selected_option_id,
    }


class FakeTextToSqlProvider(TextToSqlProvider):
    def __init__(self, provider_id="fake-gen", model="fake-gen-model", fixed_raw=None, on_call=None, raise_exc=None):
        self._provider_id = provider_id
        self._model = model
        self._fixed_raw = fixed_raw
        self._on_call = on_call
        self._raise_exc = raise_exc
        self.calls: list = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def generate_sql(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        if self._on_call is not None:
            self._on_call()
        self.calls.append({
            "system_prompt": system_prompt, "user_prompt": user_prompt,
            "max_output_tokens": max_output_tokens, "timeout_seconds": timeout_seconds,
        })
        if self._raise_exc is not None:
            raise self._raise_exc
        raw = self._fixed_raw if self._fixed_raw is not None else _sql_generation_raw()
        from core.sqlservice.sql_generation_providers import ProviderSqlGenerationResponse
        return ProviderSqlGenerationResponse(raw=raw, model=self._model)


def _evidence_column(position, name):
    return {"position": position, "name": name}


def _answer_raw(answer="There are 2 widgets.", insights=None, caveats=None, follow_ups=None):
    if insights is None:
        insights = [{
            "title": "Two widgets", "finding": "Rows 1 and 2 are both widgets.",
            "evidence_row_numbers": [1, 2],
            "evidence_columns": [_evidence_column(1, "id"), _evidence_column(2, "name")],
        }]
    return {
        "answer": answer, "insights": insights,
        "caveats": [] if caveats is None else caveats,
        "follow_up_questions": [] if follow_ups is None else follow_ups,
    }


class FakeAnswerGenerationProvider(AnswerGenerationProvider):
    def __init__(self, provider_id="fake-ans", model="fake-ans-model", fixed_raw=None, on_call=None, raise_exc=None):
        self._provider_id = provider_id
        self._model = model
        self._fixed_raw = fixed_raw
        self._on_call = on_call
        self._raise_exc = raise_exc
        self.calls: list = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def generate_answer(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        if self._on_call is not None:
            self._on_call()
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        if self._raise_exc is not None:
            raise self._raise_exc
        raw = self._fixed_raw if self._fixed_raw is not None else _answer_raw()
        return ProviderAnswerGenerationResponse(raw=raw, model=self._model)


class FakeCursor:
    def __init__(self, columns, rows, error=None):
        self._columns = columns
        self._rows = list(rows)
        self._error = error
        self.description = None
        self.executed_sql = None
        self.execute_call_count = 0
        self.closed = False

    def execute(self, sql):
        self.execute_call_count += 1
        self.executed_sql = sql
        if self._error is not None:
            raise self._error
        self.description = [(name,) for name in self._columns]

    def fetchmany(self, n):
        batch, self._rows = self._rows[:n], self._rows[n:]
        return batch

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.timeout = None
        self.rollback_called = 0
        self.close_called = 0

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_called += 1

    def close(self):
        self.close_called += 1


def _connection_factory_for(connection, on_call=None, raise_exc=None):
    calls: list = []

    def factory(config, timeout_s):
        if on_call is not None:
            on_call()
        calls.append((config, timeout_s))
        if raise_exc is not None:
            raise raise_exc
        return connection

    factory.calls = calls
    return factory


def _counting_clock(step=1.0):
    state = {"t": 0.0}

    def clock():
        value = state["t"]
        state["t"] += step
        return value

    return clock


def _shared_clock():
    """clock() is a pure getter (never auto-advances); advance() bumps it —
    used to trip the budget check exactly between two chosen stages without
    depending on how many internal clock() reads happen along the way."""
    state = {"t": 0.0}

    def clock():
        return state["t"]

    def advance(seconds=100.0):
        state["t"] += seconds

    return clock, advance
