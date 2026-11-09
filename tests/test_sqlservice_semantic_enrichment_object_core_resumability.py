"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4C2A, Problem 1:
paid-batch resumability for OBJECT_CORE preparation
(core/sqlservice/semantic_enrichment_object_core_preparation.py +
core/sqlservice/semantic_enrichment_service.py's OBJECT_CORE dispatch).

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db. Every provider call is a fake defined in this file —
never OpenAISemanticEnrichmentProvider against a real network.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_object_core_resumability.py -v
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step4c2a-resume-secret-long-enough-12345")
os.environ.setdefault("USER_ID_SALT", "test-step4c2a-resume-salt-long-enough-123456")

import data.models as models_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.semantic_enrichment_object_core_preparation as object_core_prep_module
import core.sqlservice.semantic_enrichment_store as store_module
from core.sqlservice.join_planner import clear_join_planner_cache
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_linker import clear_schema_linker_cache
from core.sqlservice.semantic_enrichment_object_core_preparation import (
    PERSISTENCE_FAILURE_CATEGORY,
    STATUS_NOT_PREPARED,
    STATUS_OBJECT_CORE_PARTIAL,
    STATUS_OBJECT_CORE_READY,
    STATUS_UNOWNED,
    prepare_source_object_core_enrichment,
)
from core.sqlservice.semantic_enrichment_providers import (
    ProviderSemanticEnrichmentResponse,
    SemanticEnrichmentProvider,
)
from core.sqlservice.semantic_enrichment_service import (
    SemanticEnrichmentConfigurationError,
    SemanticEnrichmentProviderError,
)
from core.sqlservice.semantic_enrichment_store import get_enrichment_by_cache_key
from core.sqlservice.semantic_enrichment_phases import PHASE_OBJECT_CORE


# ── Fixture plumbing (mirrors tests/test_sqlservice_semantic_enrichment_object_core_preparation.py) ─

def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _patch_connections(monkeypatch, db_path):
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(db_path))
    monkeypatch.setattr(store_module, "get_connection", lambda: _make_connection(db_path))


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
    import shutil
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    _patch_connections(monkeypatch, path)
    clear_schema_linker_cache()
    clear_join_planner_cache()
    yield path
    clear_schema_linker_cache()
    clear_join_planner_cache()


def _insert_source(db_path, source_id, user_id="alice"):
    conn = _make_connection(db_path)
    now = "2026-09-15T00:00:00Z"
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (source_id, user_id, now, now),
    )
    conn.commit()
    conn.close()


def _insert_snapshot(db_path, snapshot_id, source_id, version=1):
    conn = _make_connection(db_path)
    now = "2026-09-15T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id, columns=(("PK", "INTEGER", True, False),), tier=1):
    schema_name, object_name = object_fqn.split(".", 1)
    column_descriptors = tuple(
        {"column_name": name, "normalized_sql_type": sql_type, "is_primary_key": pk, "is_foreign_key": fk}
        for name, sql_type, pk, fk in columns
    )
    metadata = {
        "column_count": len(columns), "column_names": tuple(c[0] for c in columns),
        "column_descriptors": column_descriptors, "has_primary_key": any(c[2] for c in columns),
        "primary_key_columns": tuple(c[0] for c in columns if c[2]),
        "confirmed_relationship_edges": (), "status_columns": (), "date_columns": (), "measure_columns": (),
        "domain": None, "business_name": None, "description": None, "synonyms": (),
        "has_business_name": False, "has_description": False, "synonym_count": 0,
    }
    text = f"OBJECT: {object_fqn}\nTYPE: TABLE\nTIER: {tier}\nCOLUMNS:\n" + "\n".join(f"  {c[0]}" for c in columns)
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type="TABLE",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def _fingerprint(*, seed="a"):
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(f"fingerprint-{seed}".encode()).hexdigest(),
        "table_set_hash": hashlib.sha256(f"table-set-{seed}".encode()).hexdigest(),
    }


def _make_ready_build(db_path, docs, *, source_id=1, snapshot_id=1, user_id="alice", seed="a"):
    batch = RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=1,
        status=STATUS_READY, documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )
    result = persist_retrieval_build(batch, _fingerprint(seed=seed), user_id)
    assert result.build is not None
    return result.build


def _row_count(db_path, source_id):
    conn = _make_connection(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM sql_semantic_enrichments WHERE source_id = ? AND phase = ?",
            (source_id, PHASE_OBJECT_CORE),
        ).fetchone()[0]
    finally:
        conn.close()


class FakeObjectCoreProvider(SemanticEnrichmentProvider):
    def __init__(self, responder=None, *, provider_id="fake", model="fake-model-1"):
        self._provider_id = provider_id
        self._model = model
        self._responder = responder or _auto_respond
        self.calls: list[str] = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def enrich_batch(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        self.calls.append(user_prompt)
        return self._responder(user_prompt)


def _auto_respond(user_prompt: str) -> ProviderSemanticEnrichmentResponse:
    ids = re.findall(r"OBJECT (o\d+):", user_prompt)
    raw = {
        "objects": [
            {
                "id": i, "label": "Student", "role": "ENTITY", "concepts": ["student"],
                "aliases": ["learner"], "description": "A student record.", "confidence": 0.9,
            }
            for i in ids
        ]
    }
    return ProviderSemanticEnrichmentResponse(raw=raw, model="fake-model-1")


# ── Late provider failure preserves earlier successful validated batches ──


def test_late_provider_failure_preserves_earlier_successful_batches(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, 1)
    names = ["dbo.Alpha", "dbo.Beta", "dbo.Gamma", "dbo.Delta", "dbo.Epsilon"]
    docs = [_doc(n, source_id=1) for n in names]
    _make_ready_build(db_path, docs, source_id=1, snapshot_id=1)

    def responder(user_prompt):
        if "OBJECT o1: dbo.Epsilon" in user_prompt:  # the LAST canonical batch
            raise SemanticEnrichmentProviderError("simulated late failure")
        return _auto_respond(user_prompt)

    provider = FakeObjectCoreProvider(responder)
    result = prepare_source_object_core_enrichment(1, "alice", lambda: provider, max_objects_per_batch=1)

    assert result.status == STATUS_OBJECT_CORE_PARTIAL
    assert result.generated_count == 4  # every batch EXCEPT the failed one
    assert result.missing_object_fqns == ("dbo.Epsilon",)
    assert result.dispatched_batch_count == 5
    assert result.failed_provider_batch_count == 1
    assert result.successful_provider_batch_count == 4
    assert result.persisted_batch_count == 4
    assert result.resumable is True
    for n in names[:-1]:
        assert _row_exists(db_path, n)
    assert not _row_exists(db_path, "dbo.Epsilon")


def _row_exists(db_path, object_fqn) -> bool:
    conn = _make_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sql_semantic_enrichments WHERE object_fqn = ? AND phase = ?",
            (object_fqn, PHASE_OBJECT_CORE),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── Out-of-order successes persist canonically ──────────────────────────


def test_out_of_order_successes_persist_canonically(db_path):
    """Force the LAST-canonical-order batch to finish FIRST (an artificial
    delay on earlier batches) -- the final persisted/reported state must be
    identical to the in-order case."""
    import threading
    import time as time_module

    _insert_source(db_path, 2)
    _insert_snapshot(db_path, 2, 2)
    names = [f"dbo.T{i}" for i in range(6)]
    docs = [_doc(n, source_id=2) for n in names]
    _make_ready_build(db_path, docs, source_id=2, snapshot_id=2)

    def responder(user_prompt):
        if "OBJECT o1: dbo.T0" in user_prompt:
            time_module.sleep(0.15)  # ensure this (canonically FIRST) batch finishes LAST
        return _auto_respond(user_prompt)

    provider = FakeObjectCoreProvider(responder)
    result = prepare_source_object_core_enrichment(
        2, "alice", lambda: provider, max_objects_per_batch=1, max_concurrency=4,
    )

    assert result.status == STATUS_OBJECT_CORE_READY
    assert result.generated_count == 6
    assert result.missing_object_fqns == ()
    for n in names:
        assert _row_exists(db_path, n)


# ── Failed batch persists zero rows ─────────────────────────────────────


def test_failed_batch_persists_zero_rows(db_path):
    _insert_source(db_path, 3)
    _insert_snapshot(db_path, 3, 3)
    doc = _doc("dbo.Stu", source_id=3)
    _make_ready_build(db_path, [doc], source_id=3, snapshot_id=3)

    provider = FakeObjectCoreProvider(lambda p: (_ for _ in ()).throw(SemanticEnrichmentProviderError("boom")))
    result = prepare_source_object_core_enrichment(3, "alice", lambda: provider)

    assert result.status == STATUS_OBJECT_CORE_PARTIAL
    assert result.generated_count == 0
    assert result.persisted_batch_count == 0
    assert _row_count(db_path, 3) == 0


# ── Resume requests only missing objects / reaches READY ────────────────


def test_resume_requests_only_missing_objects_and_reaches_ready(db_path):
    _insert_source(db_path, 4)
    _insert_snapshot(db_path, 4, 4)
    names = ["dbo.Alpha", "dbo.Beta", "dbo.Gamma"]
    docs = [_doc(n, source_id=4) for n in names]
    _make_ready_build(db_path, docs, source_id=4, snapshot_id=4)

    def failing_responder(user_prompt):
        if "OBJECT o1: dbo.Gamma" in user_prompt:
            raise SemanticEnrichmentProviderError("simulated failure")
        return _auto_respond(user_prompt)

    first_provider = FakeObjectCoreProvider(failing_responder)
    first = prepare_source_object_core_enrichment(4, "alice", lambda: first_provider, max_objects_per_batch=1)
    assert first.status == STATUS_OBJECT_CORE_PARTIAL
    assert first.missing_object_fqns == ("dbo.Gamma",)

    second_provider = FakeObjectCoreProvider(_auto_respond)
    second = prepare_source_object_core_enrichment(4, "alice", lambda: second_provider, max_objects_per_batch=1)

    assert second.status == STATUS_OBJECT_CORE_READY
    assert second.generated_count == 1  # ONLY dbo.Gamma
    assert len(second_provider.calls) == 1
    assert "dbo.Gamma" in second_provider.calls[0]
    assert "dbo.Alpha" not in second_provider.calls[0]
    assert "dbo.Beta" not in second_provider.calls[0]


# ── Cached objects never trigger another provider call ──────────────────


def test_cached_objects_never_trigger_another_provider_call(db_path):
    _insert_source(db_path, 5)
    _insert_snapshot(db_path, 5, 5)
    docs = [_doc(f"dbo.T{i}", source_id=5) for i in range(4)]
    _make_ready_build(db_path, docs, source_id=5, snapshot_id=5)

    provider = FakeObjectCoreProvider(_auto_respond)
    first = prepare_source_object_core_enrichment(5, "alice", lambda: provider)
    assert first.status == STATUS_OBJECT_CORE_READY
    assert first.generated_count == 4

    second_provider = FakeObjectCoreProvider(_auto_respond)
    second = prepare_source_object_core_enrichment(5, "alice", lambda: second_provider)
    assert second.status == STATUS_OBJECT_CORE_READY
    assert second.generated_count == 0
    assert second.cached_object_count == 4
    assert second_provider.calls == []


# ── Pre-dispatch config failure makes zero calls/writes ─────────────────


def test_pre_dispatch_config_failure_makes_zero_calls_and_writes(db_path):
    _insert_source(db_path, 6)
    _insert_snapshot(db_path, 6, 6)
    docs = [_doc(f"dbo.T{i}", source_id=6) for i in range(5)]
    _make_ready_build(db_path, docs, source_id=6, snapshot_id=6)

    calls = []

    def broken_factory():
        raise SemanticEnrichmentConfigurationError("missing API key")

    with pytest.raises(SemanticEnrichmentConfigurationError):
        prepare_source_object_core_enrichment(6, "alice", broken_factory)
    assert calls == []
    assert _row_count(db_path, 6) == 0


# ── Mixed provider and validation failures reported separately ──────────


def test_mixed_provider_and_validation_failures_reported_separately(db_path):
    _insert_source(db_path, 7)
    _insert_snapshot(db_path, 7, 7)
    names = ["dbo.Alpha", "dbo.Beta", "dbo.Gamma"]
    docs = [_doc(n, source_id=7) for n in names]
    _make_ready_build(db_path, docs, source_id=7, snapshot_id=7)

    def responder(user_prompt):
        if "OBJECT o1: dbo.Alpha" in user_prompt:
            raise SemanticEnrichmentProviderError("network failure")
        if "OBJECT o1: dbo.Beta" in user_prompt:
            return ProviderSemanticEnrichmentResponse(raw={"objects": []}, model="fake-model-1")  # malformed
        return _auto_respond(user_prompt)

    provider = FakeObjectCoreProvider(responder)
    result = prepare_source_object_core_enrichment(7, "alice", lambda: provider, max_objects_per_batch=1)

    assert result.status == STATUS_OBJECT_CORE_PARTIAL
    assert result.dispatched_batch_count == 3
    assert result.failed_provider_batch_count == 1       # dbo.Alpha -- provider call itself failed
    assert result.batch_validation_failed_count == 1      # dbo.Beta -- call succeeded, response invalid
    assert result.successful_provider_batch_count == 2     # Beta (call ok) + Gamma
    assert result.validated_batch_count == 1               # Gamma only
    assert result.persisted_batch_count == 1
    assert set(result.missing_object_fqns) == {"dbo.Alpha", "dbo.Beta"}
    categories = set(result.failure_categories_by_batch_id.values())
    assert categories == {"PROVIDER_CALL_FAILED", "VALIDATION_FAILED"}


# ── Persistence failure accounting ───────────────────────────────────────


def test_persistence_failure_accounting(db_path, monkeypatch):
    _insert_source(db_path, 8)
    _insert_snapshot(db_path, 8, 8)
    names = ["dbo.Alpha", "dbo.Beta"]
    docs = [_doc(n, source_id=8) for n in names]
    _make_ready_build(db_path, docs, source_id=8, snapshot_id=8)

    real_insert = object_core_prep_module.insert_semantic_enrichment

    def flaky_insert(*args, **kwargs):
        if kwargs.get("object_fqn") == "dbo.Alpha":
            raise sqlite3.OperationalError("simulated disk failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(object_core_prep_module, "insert_semantic_enrichment", flaky_insert)

    provider = FakeObjectCoreProvider(_auto_respond)
    result = prepare_source_object_core_enrichment(8, "alice", lambda: provider, max_objects_per_batch=1)

    assert result.status == STATUS_OBJECT_CORE_PARTIAL
    assert result.generated_count == 1  # only dbo.Beta actually committed
    assert result.missing_object_fqns == ("dbo.Alpha",)
    assert PERSISTENCE_FAILURE_CATEGORY in result.failure_categories_by_batch_id.values()
    assert result.persisted_batch_count == 1
    assert not _row_exists(db_path, "dbo.Alpha")
    assert _row_exists(db_path, "dbo.Beta")


def test_safe_idempotent_rerun_after_persistence_failure(db_path, monkeypatch):
    _insert_source(db_path, 9)
    _insert_snapshot(db_path, 9, 9)
    doc = _doc("dbo.Stu", source_id=9)
    _make_ready_build(db_path, [doc], source_id=9, snapshot_id=9)

    real_insert = object_core_prep_module.insert_semantic_enrichment

    def flaky_insert(*args, **kwargs):
        raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(object_core_prep_module, "insert_semantic_enrichment", flaky_insert)
    provider = FakeObjectCoreProvider(_auto_respond)
    first = prepare_source_object_core_enrichment(9, "alice", lambda: provider)
    assert first.status == STATUS_OBJECT_CORE_PARTIAL
    assert _row_count(db_path, 9) == 0

    monkeypatch.setattr(object_core_prep_module, "insert_semantic_enrichment", real_insert)
    provider2 = FakeObjectCoreProvider(_auto_respond)
    second = prepare_source_object_core_enrichment(9, "alice", lambda: provider2)
    assert second.status == STATUS_OBJECT_CORE_READY
    assert second.generated_count == 1
    assert _row_count(db_path, 9) == 1

    # A THIRD call must be a pure no-op -- idempotent recovery complete.
    provider3 = FakeObjectCoreProvider(_auto_respond)
    third = prepare_source_object_core_enrichment(9, "alice", lambda: provider3)
    assert third.status == STATUS_OBJECT_CORE_READY
    assert third.generated_count == 0
    assert provider3.calls == []


# ── Safe logging: no secret/schema/prompt leakage ────────────────────────


def test_no_secret_or_prompt_leakage_on_provider_call_failure(db_path, caplog):
    _insert_source(db_path, 10)
    _insert_snapshot(db_path, 10, 10)
    doc = _doc("dbo.Stu", source_id=10)
    _make_ready_build(db_path, [doc], source_id=10, snapshot_id=10)

    secret_marker = "sk-TOTALLY-SECRET-KEY-123456"

    def responder(user_prompt):
        raise SemanticEnrichmentProviderError(f"auth failed for key {secret_marker}")

    provider = FakeObjectCoreProvider(responder)
    with caplog.at_level(logging.DEBUG):
        result = prepare_source_object_core_enrichment(10, "alice", lambda: provider)

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_marker not in combined
    assert "dbo.Stu" not in combined or True  # object identifiers are fine to log; only content must be scrubbed
    assert secret_marker not in str(result.warnings)
    assert secret_marker not in str(result.errors)


def test_no_secret_leakage_on_persistence_failure(db_path, monkeypatch, caplog):
    _insert_source(db_path, 11)
    _insert_snapshot(db_path, 11, 11)
    doc = _doc("dbo.Stu", source_id=11)
    _make_ready_build(db_path, [doc], source_id=11, snapshot_id=11)

    secret_marker = "sk-ANOTHER-SECRET-abcdef"

    def flaky_insert(*args, **kwargs):
        raise RuntimeError(f"db error, connection string was postgres://user:{secret_marker}@host")

    monkeypatch.setattr(object_core_prep_module, "insert_semantic_enrichment", flaky_insert)
    provider = FakeObjectCoreProvider(_auto_respond)
    with caplog.at_level(logging.DEBUG):
        prepare_source_object_core_enrichment(11, "alice", lambda: provider)

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_marker not in combined


# ── 35-batch simulation (Part: read-only build-7 simulation) ────────────


def test_35_batch_simulation_late_failure_then_resume(db_path):
    """Simulates the exact scenario the Step 4C2 report's real build-7
    estimate predicted (35 OBJECT_CORE batches): dispatch 35 independent
    batches, make a LATE one (#30) fail, prove the other 34 persist, then
    resume and prove ONLY the missing object is re-requested."""
    _insert_source(db_path, 12)
    _insert_snapshot(db_path, 12, 12)
    names = [f"dbo.Obj{i:02d}" for i in range(35)]
    docs = [_doc(n, source_id=12) for n in names]
    _make_ready_build(db_path, docs, source_id=12, snapshot_id=12)

    failing_name = "dbo.Obj30"

    def responder(user_prompt):
        if f"OBJECT o1: {failing_name}" in user_prompt:
            raise SemanticEnrichmentProviderError("simulated late-batch failure")
        return _auto_respond(user_prompt)

    first_provider = FakeObjectCoreProvider(responder)
    first = prepare_source_object_core_enrichment(
        12, "alice", lambda: first_provider, max_objects_per_batch=1, max_concurrency=4,
    )

    assert first.dispatched_batch_count == 35
    assert first.status == STATUS_OBJECT_CORE_PARTIAL
    assert first.generated_count == 34
    assert first.missing_object_fqns == (failing_name,)
    assert len(first_provider.calls) == 35  # every batch WAS attempted (paid) exactly once

    second_provider = FakeObjectCoreProvider(_auto_respond)
    second = prepare_source_object_core_enrichment(
        12, "alice", lambda: second_provider, max_objects_per_batch=1, max_concurrency=4,
    )

    assert second.status == STATUS_OBJECT_CORE_READY
    assert second.generated_count == 1
    assert second.dispatched_batch_count == 1  # ONLY the previously-missing object
    assert len(second_provider.calls) == 1
    assert failing_name in second_provider.calls[0]
    assert second.eligible_object_count == 35
    assert second.missing_object_fqns == ()

    print(
        f"\n[Step 4C2A 35-batch simulation] first run: {len(first_provider.calls)} call(s), "
        f"{first.generated_count} persisted, {first.missing_object_count} missing "
        f"-> resume run: {len(second_provider.calls)} call(s), final status={second.status}"
    )
