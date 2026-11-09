"""
SQL rebuild Phase 4, Step 2 — automatic connection-time retrieval
preparation, wired into the existing background bootstrap job.

Covers:
  - core.sqlservice package-level exports (answer_question, generate_answer,
    execute_validated_sql, answer-generation provider symbols, and their
    result/status types) actually work via `from core.sqlservice import ...`.
  - data.datasource_service._prepare_retrieval_for_source: the new
    best-effort helper, in isolation (flag gating, success/reuse,
    missing-configuration, failure, credential safety, argument
    propagation).
  - data.datasource_service.run_connect_bootstrap_job: the wiring itself
    (called after bootstrap steps succeed, before the job is marked
    COMPLETE; failure never fails the job; repeated runs are idempotent).
  - data.datasource_service.create_data_source: retrieval preparation is
    never invoked synchronously from the request path.

Uses only temporary on-disk SQLite databases (never data/toolsmith.db),
never a real OpenAI or SQL Server call — every embedding provider here is
a fake defined in this file, and schema discovery/structural profiling/PII
classification are monkeypatched to no-ops exactly like
tests/test_connect_bootstrap_scope.py already does.

Run:
    venv/Scripts/pytest tests/test_connect_time_retrieval_preparation.py -v
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry

import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.metadata_loader as metadata_loader_module
import core.sqlservice.retrieval_preparation as retrieval_preparation_module
import core.sqlservice.retrieval_store as retrieval_store_module
import data.datasource_service as datasource_service_module
import data.schema_service as schema_service_module
from core.connectors.schema import ColumnInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.sqlservice.embedding_providers import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderError,
    ProviderEmbeddingBatchResponse,
    ProviderEmbeddingItem,
)
from core.sqlservice.retrieval_preparation import BUILD_OUTCOME_CREATED, STATUS_OK as PREPARATION_STATUS_OK
from data.datasource_service import _prepare_retrieval_for_source, create_data_source, run_connect_bootstrap_job

_NOW = "2026-09-06T00:00:00Z"


# ── 0. Package-level export smoke test ──────────────────────────────────


def test_package_level_exports_are_importable():
    from core.sqlservice import (
        AnswerGenerationConfigurationError,
        AnswerGenerationProvider,
        AnswerGenerationProviderError,
        AnswerGenerationResult,
        AnswerInsight,
        AnswerValidationError,
        ANSWER_GENERATION_STATUS_OK,
        ConnectionFactory,
        EvidenceColumnReference,
        OpenAIAnswerGenerationProvider,
        ProviderAnswerGenerationResponse,
        SqlExecutionResult,
        SQL_EXECUTION_STATUS_OK,
        execute_validated_sql,
        generate_answer,
    )

    assert callable(generate_answer)
    assert callable(execute_validated_sql)
    assert SQL_EXECUTION_STATUS_OK == "OK"
    assert ANSWER_GENERATION_STATUS_OK == "OK"
    assert issubclass(AnswerGenerationConfigurationError, Exception)
    assert issubclass(AnswerGenerationProviderError, Exception)


def test_package_level_status_aliases_do_not_collide():
    """The three modules' STATUS_OK constants are distinct Python names at
    package level (aliased, per the established core.sqlservice.__init__
    convention) even though their string VALUES are all "OK"."""
    import core.sqlservice as sqlservice

    assert sqlservice.SQL_EXECUTION_STATUS_OK == "OK"
    assert sqlservice.ANSWER_GENERATION_STATUS_OK == "OK"
    assert sqlservice.SQL_VALIDATION_STATUS_OK == "OK"


# ── Fixture plumbing (mirrors tests/test_sqlservice_retrieval_preparation.py
#    and tests/test_connect_bootstrap_scope.py, combined: real temp SQLite
#    backs both the metadata_jobs bookkeeping AND the real retrieval/
#    embedding/vector-index pipeline) ────────────────────────────────────


def _make_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
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
    monkeypatch.setattr(metadata_loader_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(schema_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(datasource_service_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_vector_index_cache():
    from core.sqlservice.vector_index import clear_vector_index_cache
    clear_vector_index_cache()
    yield
    clear_vector_index_cache()


def _insert_source(db_path, source_id=1, user_id="alice"):
    conn = _make_connection(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, "
        " created_at, updated_at) VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _col(name, data_type, *, pk=False, ordinal=1):
    return ColumnInfo(
        column_name=name, ordinal_position=ordinal, data_type=data_type,
        raw_type=data_type.lower(), is_nullable=True, is_primary_key=pk, is_identity=pk,
    )


def _table():
    return TableInfo(
        table_name="widgets", schema_name="dbo", table_fqn="dbo.widgets", table_type="TABLE",
        columns=[_col("id", "INTEGER", pk=True)],
        primary_keys=[PrimaryKeyInfo("id", 1)], foreign_keys=[],
    )


def _insert_snapshot(db_path, source_id=1, snapshot_id=1, version=1):
    import dataclasses
    import json

    snapshot = SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at=_NOW,
        schemas=[SchemaInfo(schema_name="dbo", tables=[_table()])],
    )
    conn = _make_connection(db_path)
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', ?, ?, ?)",
        (snapshot_id, source_id, version, json.dumps(dataclasses.asdict(snapshot)), _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _prepared_source(db_path, source_id=1, user_id="alice"):
    """An owned source with a real schema snapshot — everything
    prepare_source_for_retrieval() needs to build a genuine retrieval build."""
    _insert_source(db_path, source_id, user_id)
    _insert_snapshot(db_path, source_id=source_id)


def _insert_job(db_path, source_id=1, user_id="alice"):
    conn = _make_connection(db_path)
    cursor = conn.execute(
        "INSERT INTO metadata_jobs (source_id, user_id, job_type, status, created_at, updated_at) "
        "VALUES (?, ?, 'initial_metadata', 'QUEUED', ?, ?)",
        (source_id, user_id, _NOW, _NOW),
    )
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return job_id


def _job_row(db_path, job_id):
    conn = _make_connection(db_path)
    row = conn.execute("SELECT * FROM metadata_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row)


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimensions=4, provider_id="fake", model="fake-model"):
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model
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
        texts = list(texts)
        self.calls.append(texts)
        items = tuple(
            ProviderEmbeddingItem(
                index=i, vector=tuple((1.0 if j == (len(t) % self._dimensions) else 0.1) for j in range(self._dimensions)),
            )
            for i, t in enumerate(texts)
        )
        return ProviderEmbeddingBatchResponse(items=items, model=self._model)


class RaisingEmbeddingProvider(EmbeddingProvider):
    def __init__(self, exc, dimensions=4, provider_id="fake", model="fake-model"):
        self._exc = exc
        self._dimensions = dimensions
        self._provider_id = provider_id
        self._model = model

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
        raise self._exc


# ── Part A: _prepare_retrieval_for_source in isolation ─────────────────


def test_flag_disabled_preparation_not_called(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", False)
    monkeypatch.setattr(
        retrieval_preparation_module, "prepare_source_for_retrieval",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called when disabled")),
    )

    summary = _prepare_retrieval_for_source(1, "alice", provider=FakeEmbeddingProvider())

    assert summary == "Retrieval preparation is disabled."


def test_successful_created_preparation(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = FakeEmbeddingProvider()

    summary = _prepare_retrieval_for_source(1, "alice", provider=provider)

    assert "new retrieval index was built" in summary
    assert len(provider.calls) == 1


def test_successful_reused_preparation(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = FakeEmbeddingProvider()

    first = _prepare_retrieval_for_source(1, "alice", provider=provider)
    second = _prepare_retrieval_for_source(1, "alice", provider=provider)

    assert "new retrieval index was built" in first
    assert "existing retrieval index was reused" in second
    # No duplicate embedding calls when everything is already cached.
    assert len(provider.calls) == 1


def test_missing_embedding_configuration(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = RaisingEmbeddingProvider(EmbeddingConfigurationError("OPENAI_API_KEY is not configured."))

    summary = _prepare_retrieval_for_source(1, "alice", provider=provider)

    assert summary == "Retrieval preparation skipped: embedding provider is not configured."


def test_preparation_failure_is_best_effort(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = RaisingEmbeddingProvider(EmbeddingProviderError("boom"))

    summary = _prepare_retrieval_for_source(1, "alice", provider=provider)  # must not raise

    assert summary == "Retrieval preparation failed; retry preparation before asking questions."


def test_document_build_failure_is_best_effort(db_path, monkeypatch):
    """No snapshot at all -> the document-build stage itself fails (not the
    embedding stage) — still a safe, non-raising, generic outcome."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _insert_source(db_path)  # no _insert_snapshot()
    provider = FakeEmbeddingProvider()

    summary = _prepare_retrieval_for_source(1, "alice", provider=provider)

    assert summary == "Retrieval preparation failed; retry preparation before asking questions."
    assert len(provider.calls) == 0  # embedding stage never reached


def test_no_credential_leakage_in_summary(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    secret_bearing = EmbeddingProviderError(
        "AuthenticationError: Incorrect API key provided: sk-SECRET12345abcdef"
    )
    provider = RaisingEmbeddingProvider(secret_bearing)

    summary = _prepare_retrieval_for_source(1, "alice", provider=provider)

    assert "sk-SECRET12345abcdef" not in summary
    assert "SECRET" not in summary


# ── Step 2A: no raw exception content in the summary OR the logs ───────


_SECRET_BEARING_EXCEPTIONS = [
    EmbeddingProviderError("AuthenticationError: Incorrect API key provided: sk-SECRET12345abcdef"),
    EmbeddingProviderError("Login failed for user 'svc'. Connection: Server=db.internal;UID=svc;PWD={S3cr3tPWD!}"),
    EmbeddingConfigurationError("api_key='sk-CONFIGSECRET999' is invalid"),
    RuntimeError("connection string: mssql://svc:hunter2@db.internal:1433/AppDb"),
]


@pytest.mark.parametrize("exc", _SECRET_BEARING_EXCEPTIONS, ids=[type(e).__name__ + "_" + str(i) for i, e in enumerate(_SECRET_BEARING_EXCEPTIONS)])
def test_no_credential_leakage_in_summary_or_logs(db_path, monkeypatch, caplog, exc):
    """API keys, passwords, and connection strings embedded in a raw
    provider/driver exception message must never reach the returned
    summary NOR the application logs produced by this helper."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = RaisingEmbeddingProvider(exc)
    secret_fragments = ["sk-SECRET12345abcdef", "S3cr3tPWD", "PWD=", "UID=", "sk-CONFIGSECRET999", "hunter2"]

    with caplog.at_level("DEBUG"):
        summary = _prepare_retrieval_for_source(1, "alice", provider=provider)

    all_logged_text = "\n".join(record.getMessage() for record in caplog.records)
    for fragment in secret_fragments:
        assert fragment not in summary
        assert fragment not in all_logged_text
    # The exception's own message/str must never appear verbatim either.
    assert str(exc) not in summary
    assert str(exc) not in all_logged_text


def test_no_traceback_or_exc_info_logged_on_failure(db_path, monkeypatch, caplog):
    """No logger.exception()/exc_info=True — a captured record from this
    module must never carry exception info (which would include a full
    traceback)."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = RaisingEmbeddingProvider(EmbeddingProviderError("boom with a secret sk-ABC123"))

    with caplog.at_level("DEBUG"):
        _prepare_retrieval_for_source(1, "alice", provider=provider)

    own_records = [r for r in caplog.records if r.name == "data.datasource_service"]
    assert len(own_records) >= 1
    for record in own_records:
        assert record.exc_info is None
        assert record.exc_text is None


def test_no_automatic_retry_claim_anywhere(db_path, monkeypatch, caplog):
    """The system must never claim it will retry preparation on its own —
    no such mechanism exists yet."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = RaisingEmbeddingProvider(EmbeddingProviderError("boom"))

    with caplog.at_level("DEBUG"):
        summary = _prepare_retrieval_for_source(1, "alice", provider=provider)

    assert "retried automatically" not in summary
    assert "will be retried" not in summary
    assert summary == "Retrieval preparation failed; retry preparation before asking questions."
    all_logged_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "retried automatically" not in all_logged_text
    assert "will be retried" not in all_logged_text


def test_failure_log_contains_only_safe_structured_fields(db_path, monkeypatch, caplog):
    """The logged record for a failure must carry source_id, a failure
    stage label, and the exception's class name — nothing else exception-
    derived."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    _prepared_source(db_path)
    provider = RaisingEmbeddingProvider(EmbeddingProviderError("some raw driver text with sk-XYZ"))

    with caplog.at_level("DEBUG"):
        _prepare_retrieval_for_source(1, "alice", provider=provider)

    own_records = [r for r in caplog.records if r.name == "data.datasource_service"]
    assert len(own_records) == 1
    message = own_records[0].getMessage()
    assert "source_id=1" in message
    assert "EmbeddingProviderError" in message  # type(exc).__name__
    assert "stage=embedding" in message
    assert "sk-XYZ" not in message


def test_correct_source_user_provider_propagation(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    captured = {}
    provider = FakeEmbeddingProvider()

    def _fake_prepare(source_id, user_id, given_provider, **kwargs):
        captured["args"] = (source_id, user_id, given_provider)
        return SimpleNamespace(status=PREPARATION_STATUS_OK, build_outcome=BUILD_OUTCOME_CREATED)

    monkeypatch.setattr(retrieval_preparation_module, "prepare_source_for_retrieval", _fake_prepare)

    _prepare_retrieval_for_source(42, "bob", provider=provider)

    assert captured["args"] == (42, "bob", provider)


def test_default_provider_is_openai_embedding_provider(db_path, monkeypatch):
    """When no provider is injected (the real production call site), the
    helper must construct the project's real OpenAIEmbeddingProvider —
    verified by substituting a zero-arg-constructible fake in its place at
    the exact import site the helper resolves lazily."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    import core.sqlservice.embedding_providers as embedding_providers_module

    used_providers = []

    class _SwappedProvider(FakeEmbeddingProvider):
        def __init__(self):
            super().__init__()
            used_providers.append(self)

    monkeypatch.setattr(embedding_providers_module, "OpenAIEmbeddingProvider", _SwappedProvider)
    _prepared_source(db_path)

    summary = _prepare_retrieval_for_source(1, "alice")  # no provider= override

    assert "new retrieval index was built" in summary
    assert len(used_providers) == 1
    assert len(used_providers[0].calls) == 1


# ── Part B: run_connect_bootstrap_job wiring ────────────────────────────


def _run_bootstrap_with_stubs(db_path, job_id, retrieval_stub):
    with patch("data.schema_service.run_discovery"), \
         patch("data.profiling_service.run_structural_profiling"), \
         patch("data.pii_classification_service.run_pii_classification"), \
         patch.object(datasource_service_module, "_prepare_retrieval_for_source", retrieval_stub):
        run_connect_bootstrap_job(job_id)


def test_retrieval_preparation_called_with_correct_args_after_bootstrap(db_path, monkeypatch):
    _insert_source(db_path)
    job_id = _insert_job(db_path)
    captured = {}

    def _stub(source_id, user_id, **kwargs):
        captured["args"] = (source_id, user_id)
        return "Retrieval preparation complete: a new retrieval index was built."

    _run_bootstrap_with_stubs(db_path, job_id, _stub)

    assert captured["args"] == (1, "alice")
    row = _job_row(db_path, job_id)
    assert row["status"] == "COMPLETE"
    assert row["current_step"] == "READY"
    assert "a new retrieval index was built" in row["progress_message"]


def test_job_completes_only_after_preparation_handled(db_path, monkeypatch):
    """The helper must run while the job is still RUNNING/RETRIEVAL_PREPARATION
    — never after the job has already been marked COMPLETE."""
    _insert_source(db_path)
    job_id = _insert_job(db_path)
    observed = {}

    def _stub(source_id, user_id, **kwargs):
        row = _job_row(db_path, job_id)
        observed["status"] = row["status"]
        observed["current_step"] = row["current_step"]
        return "Retrieval preparation complete: a new retrieval index was built."

    _run_bootstrap_with_stubs(db_path, job_id, _stub)

    assert observed["status"] == "RUNNING"
    assert observed["current_step"] == "RETRIEVAL_PREPARATION"
    row = _job_row(db_path, job_id)
    assert row["status"] == "COMPLETE"


def test_preparation_failure_does_not_fail_the_job(db_path, monkeypatch):
    _insert_source(db_path)
    job_id = _insert_job(db_path)

    _run_bootstrap_with_stubs(
        db_path, job_id, lambda source_id, user_id, **k: "Retrieval preparation failed; retry preparation before asking questions.",
    )

    row = _job_row(db_path, job_id)
    assert row["status"] == "COMPLETE"
    assert row["current_step"] == "READY"
    assert "failed" in row["progress_message"]


def test_flag_disabled_end_to_end_through_bootstrap_job(db_path, monkeypatch):
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", False)
    _insert_source(db_path)
    job_id = _insert_job(db_path)

    with patch("data.schema_service.run_discovery"), \
         patch("data.profiling_service.run_structural_profiling"), \
         patch("data.pii_classification_service.run_pii_classification"):
        run_connect_bootstrap_job(job_id)

    row = _job_row(db_path, job_id)
    assert row["status"] == "COMPLETE"
    assert "disabled" in row["progress_message"]


def test_repeated_job_run_is_idempotent(db_path, monkeypatch):
    """Running the background job twice against an unchanged schema must
    reuse the retrieval build/embeddings, never duplicate them."""
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)
    import core.sqlservice.embedding_providers as embedding_providers_module

    shared_provider = FakeEmbeddingProvider()
    monkeypatch.setattr(embedding_providers_module, "OpenAIEmbeddingProvider", lambda: shared_provider)

    _prepared_source(db_path)
    job_id_1 = _insert_job(db_path)
    job_id_2 = _insert_job(db_path)

    with patch("data.schema_service.run_discovery"), \
         patch("data.profiling_service.run_structural_profiling"), \
         patch("data.pii_classification_service.run_pii_classification"):
        run_connect_bootstrap_job(job_id_1)
        run_connect_bootstrap_job(job_id_2)

    row1 = _job_row(db_path, job_id_1)
    row2 = _job_row(db_path, job_id_2)
    assert "a new retrieval index was built" in row1["progress_message"]
    assert "the existing retrieval index was reused" in row2["progress_message"]
    # Exactly one embedding call total across both runs — no duplicate call
    # when the second run finds everything already cached.
    assert len(shared_provider.calls) == 1


# ── Part C: create_data_source never prepares retrieval synchronously ──


def test_no_synchronous_preparation_during_source_creation(db_path, monkeypatch):
    monkeypatch.setattr("core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED", False)
    monkeypatch.setattr("core.config.SQL_RETRIEVAL_PREPARATION_ON_CONNECT_ENABLED", True)

    calls = []
    monkeypatch.setattr(
        datasource_service_module, "_prepare_retrieval_for_source",
        lambda *a, **k: calls.append(a) or "unused",
    )

    record = create_data_source(
        "alice",
        {
            "display_name": "Prod SQL Server", "source_type": "mssql",
            "config": {"host": "db.internal", "database": "AppDb", "auth_type": "sql",
                       "username": "svc", "password": "secret"},
            "metadata": {},
        },
    )

    assert record["metadata_job_id"] is not None
    assert calls == []  # never invoked synchronously — and no background thread was started either
